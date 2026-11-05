"""
本文件是后台任务执行核心层 (`background_jobs.py`)，负责管理 Agent 在本地执行的所有长周期任务后台任务执行层：
把 shell 命令或 sub-agent 异步任务作为后台 Job 运行，输出落盘到日志文件，并跟踪完成状态用于通知。

它的核心职责与功能范围包括：
1. Job 实体定义 (Job)：封装任务的唯一标识、多态类型（shell/agent）、生命周期状态、输出日志落盘路径、终止句柄及最终报告等。
2. 进程衍生与隔离 (spawn_shell)：通过异步子进程执行 shell 命令，并针对 Windows 和 POSIX 分别实现进程组隔离，确保级联杀死整个进程树。
3. Sub-Agent 衍生与管理 (spawn_agent)：姊妹方法，专门负责拉起异步 Sub-Agent 任务，将其绑定到协程取消句柄。
4. 异步状态监视 (_watch 与 _watch_agent)：
   - _watch：利用 asyncio.subprocess 非阻塞地等待系统进程退出并更新状态。
   - _watch_agent：非阻塞地等待 sub-agent 协程运行完毕，捕获异常并安全维护任务状态。
5. 进程树与协程安全终止：提供跨平台的进程树强力终止（Windows taskkill / POSIX killpg）以及协程取消机制。
6. 注册表与生命周期调度 (JobRegistry)：
   - 会话级（Session-level）的任务生命周期中心。
   - 追踪前台与后台任务的转换（如用户通过 Ctrl+B 将前台耗时任务转入后台）。
   - 维护「已完成但尚未通知模型」的任务队列 (pop_unnotified)，供后续 hook 和 reminder 注入给 Agent。
   - 会话退出时的安全清理 (shutdown)。
"""
from __future__ import annotations

import asyncio
import os
import random
import signal
import string
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

JOBS_ROOT = Path.home() / ".my-code-agent" / "jobs"

"""
因为被监控的后台程序可能会疯狂输出海量日志（比如每秒打印几百行），
如果把这些内容全部不加过滤地塞给大模型的上下文（Context），
不仅会瞬间耗尽 Token、把大模型撑爆，还会导致主循环卡死。
为了解决这个“信息洪流”问题，这里设计了一套滑动窗口限流与熔断保护机制。
"""
EVENT_WINDOW = 10.0             #以10秒为一个窗口长度，计算输出频率
MAX_EVENTS_PER_WINDOW = 20      #每个窗口最多允许 20 行事件输出给大模型，超过的就丢弃并计数
MAX_PENDING_EVENTS = 200        #如果 Agent 一直在忙别的，没时间消费监控事件，那么待推送事件的最大队列长度，新来的事件会被丢弃并计数，防止内存无限膨胀
MAX_DROPPED_EVENTS = 100        #最大允许连续/累计丢弃总数 —— 熔断阈值
MAX_EVENT_CHARS = 500           #单行事件的最大字符数

_ID_ALPHABET = string.digits + string.ascii_lowercase


def _new_job_id(prefix: str) -> str:
    # 随机 id 而非自增计数：重启进程后恢复会话也不会撞车、不会覆盖旧日志；前缀标 job 类型（b=shell，a=agent）
    return prefix + "".join(random.choices(_ID_ALPHABET, k=8))

# 定义任务可能处于的几种状态类型
JobStatus = Literal["running", "completed", "failed", "killed"]
JobKind = Literal["shell", "agent", "monitor"]

# 状态码到人类可读文本的映射，用于生成通知摘要
_STATUS_TEXT = {
    "completed": "执行成功",
    "failed": "执行失败",
    "killed": "已被终止",
}


@dataclass
class Job:
    """
    Job 数据类：
    代表一个具体的后台任务实体，记录了该任务从出生到终结的全生命周期元数据。
    """
    id: str
    # "shell"，"subagent"，"monitor"
    kind: JobKind
    description: str
    # Job 的日志输出存储到文件
    log_path: Path
    # 任务当前状态，初始为运行，另有三种：completed\failed\killed
    status: JobStatus = "running"
    # 进程退出码，进程结束前为 None
    returncode: int | None = None
    # 标记该任务的结束状态是否已经通知过 Agent（防止重复通知）
    notified: bool = False
    # background 标志位：
    # - False 表示前台命令：其结果会直接随 tool result 返回给模型，不发送异步完成通知。
    # - True 表示后台任务：用户按 ctrl+b 或模型指定时会翻转为 True。
    background: bool = True
    # 由任务创建方（如 spawn_shell）注入的闭包函数，用于后续安全终止该任务及其子进程
    kill_func: Callable[[], None] | None = None
    # agent 型 job 的最终报告，shell 型 job 不用（输出都在日志文件里）
    result: str | None = None
    # monitor 型 job 攒着还没推送的事件行；dropped_events 是留着随下次通知上报的丢弃数，dropped_total 只增不减，供熔断判断
    pending_events: list[str] = field(default_factory=list)
    dropped_events: int = 0         #当前窗口内被丢弃的事件数（因为输出太快，超出窗口预算或队列已满）
    dropped_total: int = 0          #全生命期累计被丢弃的事件数（因为输出太快，超出窗口预算或队列已满）
    window_start: float = 0.0       #滑动时间窗口的起点时间戳
    window_count: int = 0           #滑动时间窗口内已推送的事件数（用于速率限制）

    def summary(self) -> str:
        """
        一句话描述这个 job 的结局，通知用。
        """
        detail = _STATUS_TEXT[self.status]
        if self.returncode is not None:
            detail += f"，exit code {self.returncode}"
        noun = "sub agent" if self.kind == "agent" else "后台命令"
        return f"{noun}「{self.description}」{detail}"

    def push_event(self, line: str) -> None:
        """
        把一行事件放进待推送队列，受速率限制：超出窗口预算、或队列已经堆满的行只累加丢弃计数。
        """
        now = time.monotonic()
        # 1. 检查并重置滑动时间窗口 (Event Window)
        if now - self.window_start >= EVENT_WINDOW:
            self.window_start = now
            self.window_count = 0
        self.window_count += 1

        # 2. 速率限制与队列满载保护：超额则丢弃并累计计数，防止内存溢出
        if self.window_count > MAX_EVENTS_PER_WINDOW or len(self.pending_events) >= MAX_PENDING_EVENTS:
            self.dropped_events += 1
            self.dropped_total += 1
            return
        
        # 3. 截断单行最大字符数后压入待推送队列
        self.pending_events.append(line[:MAX_EVENT_CHARS])


class JobRegistry:
    """
    会话级 Job 注册表 (JobRegistry)：
    作为中央管理器，负责创建、查询、终止后台 Job，并把「已完成但还没通知」的 Job 交付给通知通道。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._jobs: dict[str, Job] = {}          # 内存中的任务字典：key 是 job_id，value 是 Job 实例
        self._jobs_dir = JOBS_ROOT / session_id  
        # 将该 Task 强引用存入 `self._watchers` 集合，防止被 Python 的 GC 机制提前误杀。
        self._watchers: set = set()
    
    # 起进程
    async def spawn_shell(self, command: str, background: bool = True) -> Job:
        """
        衍生（Spawn）一个后台 Shell 任务：
        1. 创建唯一的 Job ID 与日志文件句柄。
        2. 启动平台隔离的异步子进程，并将输出直接重定向落盘到日志文件。
        3. 注册异步监视协程（_watch），在后台静默等待子进程运行完毕。
        """
        # 1. 生成全局唯一的 Job ID，创建会话专属的日志落盘目录
        job_id = _new_job_id("b")
        # 2. 确保日志存储目录存在
        self._jobs_dir.mkdir(parents=True, exist_ok=True)      
        log_path = self._jobs_dir / f"{job_id}.log"
        # 3. 以二进制写模式打开日志文件，用于吸纳子进程的 stdout/stderr
        log_file = open(log_path, "wb")

        # 启动，初始化，异步执行 shell 命令，将输出绑定到日志文件
        # create_subprocess_shell拉起子进程和初始化，绑定给proc异步子进程对象，任务此时会在后台进行
        # 将进程的 stdout（标准输出流）和 stderr（错误输出流）合并到一起，保存到 log_file 文件中
        proc = await asyncio.create_subprocess_shell(
            command, stdout=log_file, stderr=subprocess.STDOUT, **_process_group_kwargs()
        )
        pid = proc.pid

        # 实例化 Job 对象，并注入对应的终止闭包（绑定了具体的进程 PID）
        job = Job(
            id=job_id,
            kind="shell",
            description=command,
            log_path=log_path,
            background=background,
            kill_func=lambda: _kill_process_tree(pid),
        )
        self._jobs[job_id] = job        #一对一的关系

        # watcher任务加到状态保存里一直盯着后台command运行情况
        # 调用 `asyncio.create_task()` 将 `_watch` 协程抛给事件循环，形成一个独立的后台 Task（分身任务）
        # 主流程（主光标）毫不停留，瞬间释放并返回 Job 实例
        watcher = asyncio.create_task(self._watch(job, proc, log_file))
        # 将该 Task 强引用存入 `self._watchers` 集合，防止被 Python 的 GC 机制提前误杀
        self._watchers.add(watcher)
        # 挂载 `add_done_callback(self._watchers.discard)`，预定在任务终结时由事件循环自动清理,结束的是wathcer任务
        watcher.add_done_callback(self._watchers.discard)
        return job

    async def _watch(self, job: Job, proc: asyncio.subprocess.Process, log_file) -> None:
        """
        异步监视器（Private Watcher Coroutine）：
        非阻塞地等待子进程运行结束，关闭日志文件，并根据退出码更新任务状态。
        """
        # 该协程把自己挂起并退出事件循环的活跃队列，
        # 不消耗任何 CPU 资源。它完全依赖操作系统内核的进程退出信号（如 SIGCHLD）由事件循环唤醒。
        returncode = await proc.wait()
        log_file.close()
        if job.status == "killed":
            return
        # 防御性检查：若已被外部强行终止（killed），则保留终止态，不作状态覆盖
        job.returncode = returncode
        # 终结状态写入
        job.status = "completed" if returncode == 0 else "failed"

    async def spawn_agent(self, description: str, run) -> Job:
        """
        spawn_shell 的姊妹方法，创建 agent 型 job（一律后台运行）：run 是接收 Job 的异步函数，负责干活并填好 job.result。
        """
        job_id = _new_job_id("a")
        # 落盘日志文件，确保目录存在
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._jobs_dir / f"{job_id}.log"
        log_path.touch()
        # 实例化 Job 对象（kind="agent"）
        job = Job(id=job_id, kind="agent", description=description, log_path=log_path)
        # 核心点 1：把传进来的异步干活函数 `run(job)` 包装成一个独立的 Task 丢给事件循环
        task = asyncio.create_task(run(job))
        # shell job 终止是杀进程树，agent job 终止就是取消协程
        job.kill_func = task.cancel #绑定job.kill_func为task.cancel方法，kill_func被调用时会取消task
        self._jobs[job_id] = job
        # 核心点 2：创建一个 watcher 协程，专门盯着这个 sub-agent 任务的运行情况
        watcher = asyncio.create_task(self._watch_agent(job, task))
        # 将该 Task 强引用存入 `self._watchers` 集合，防止被 Python 的 GC 机制提前误杀
        self._watchers.add(watcher)
        # 挂载 `add_done_callback(self._watchers.discard)`，预定在任务终结时由事件循环自动清理掉watcher任务
        watcher.add_done_callback(self._watchers.discard)
        return job
    
    async def _watch_agent(self, job: Job, task: asyncio.Task) -> None:
        try:
            # 挂起自己，等待 Sub-Agent 协程彻底跑完（期间不消耗 CPU）
            await task
        except asyncio.CancelledError:
            # 如果被外部强行终止（调用了 job.kill() -> task.cancel()），捕获异常直接退出
            return
        except Exception as e:
            # 如果 Sub-Agent 在运行中途崩了、抛了未捕获的异常，捕获异常并写入 job.result，状态置为 failed
            job.result = f"sub agent 运行出错：{type(e).__name__}: {e}"
            status = "failed"
        else:
            # 完成状态更新
            status = "completed"
        if job.status == "running":
            job.status = status

    async def spawn_monitor(self, command: str, description: str, timeout: float | None) -> Job:
        """
        【新增方法】：创建 monitor 型后台任务
        将命令作为监控守护进程运行。非空 stdout 行会实时转换为事件上报；
        若 timeout 传 None 则持续运行，直到被显式终止或触发熔断。
        """
        job_id = _new_job_id("m")
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._jobs_dir / f"{job_id}.log"

        # 1 先拉起一个独立的子进程，stdout/stderr 都重定向到管道（PIPE），由 _pump_monitor 负责泵送输出
        proc = await asyncio.create_subprocess_shell(
            command,                            #命令内容（比如 "tail -f app.log"）
            stdout=asyncio.subprocess.PIPE,     #command运行输出的stdout，连接管道输出缓存
            stderr=subprocess.STDOUT,           #将子进程的标准错误输出重定向到标准输出，确保最后都会经过管道处理
            limit=1024 * 1024,                  #设置管道缓冲区大小为 1MB，防止输出过大导致阻塞
            **_process_group_kwargs(),          #确保子进程在独立的进程组中运行，便于后续统一终止整个进程树
        )
        pid = proc.pid
        job = Job(
            id=job_id,
            kind="monitor",
            description=description,
            log_path=log_path,
            kill_func=lambda: _kill_process_tree(pid),
        )
        self._jobs[job_id] = job

        # 注册专门用于泵送输出和处理超时的后台监控 Task
        watcher = asyncio.create_task(self._pump_monitor(job, proc, timeout))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return job

    async def _pump_monitor(
        self, job: Job, proc: asyncio.subprocess.Process, timeout: float | None
    ) -> None:
        """
        【新增内部协程】：管道泵送器（Output Pumper）
        负责从 subprocess.PIPE 异步逐行读取标准输出，同步写日志文件，并推入事件缓冲区。
        """
        # 如果设置了超时，拉起一个并行的超时监控任务
        timer = asyncio.create_task(self._monitor_timeout(job, timeout)) if timeout is not None else None
        # 行缓冲模式打开日志文件，确保模型随时通过 read_file 都能看到最新输出
        log_file = open(job.log_path, "w", encoding="utf-8", buffering=1)
        try:
            # 每行既写日志留档，也进事件队列等着推给模型
            try:
                async for raw in proc.stdout:  #async for 循环异步迭代子进程的 stdout 流，每次迭代获取一行输出，等待子进程输出新行时自动挂起协程，避免阻塞事件循环
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    log_file.write(line + "\n")
                    if not line:
                        continue
                    job.push_event(line)
                    # 触发熔断保护：丢弃事件数达到上限时，主动截断并停掉监控
                    if job.dropped_total >= MAX_DROPPED_EVENTS:
                        self._stop_monitor(job, "monitor 已停止：输出太多，请用更精确的过滤条件重新挂载")
                        break
            except ValueError:
                # 异常处理：单行字符过长超过 buffer 限制时，安全终止监控
                self._stop_monitor(job, "monitor 已停止：单行输出过长，请先在命令中截断或过滤")
                await proc.wait()
                return
            returncode = await proc.wait()
            if job.status == "killed":
                return
            # 等待子进程正常退出并更新状态
            job.returncode = returncode
            job.status = "completed" if returncode == 0 else "failed"
            # 结束消息也走事件通道，标记已通知就不会再走一遍 job 完成通知
            job.notified = True
            job.pending_events.append(f"[monitor 已停止：被监控的命令自己退出了，exit code {returncode}]")
        finally:
            log_file.close()
            if timer:
                timer.cancel()

    async def _monitor_timeout(self, job: Job, timeout: float) -> None:
        await asyncio.sleep(timeout)
        if job.status == "running":
            self._stop_monitor(job, f"monitor 已停止：到了 {int(timeout)} 秒的超时上限，还需要监控就重新挂载")

    def _stop_monitor(self, job: Job, note: str) -> None:
        # 停止说明直接入队，不受限流影响
        job.pending_events.append(f"[{note}]")
        self.kill(job.id)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def running(self, background: bool | None = None) -> list[Job]:
        """
        查询当前仍在运行（running）的 Job。
        可通过传入 background 参数来精准筛选：
        - False: 只筛出前台命令
        - True: 只筛出后台任务
        - None: 所有正在跑的任务
        """
        return [
            j for j in self._jobs.values()
            if j.status == "running" and (background is None or j.background is background)
        ]

    def move_to_background(self) -> None:
        """
        将所有还在运行的前台命令批量转为后台任务（通常绑定到用户的 Ctrl+B 快捷键触发）。
        """
        for job in self.running(background=False):
            job.background = True

    def kill(self, job_id: str) -> bool:
        """
        安全终止指定的后台任务：
        通过先修改状态（置为 killed 并标记为已通知），再调用底层终止函数，
        从而有效防止 watcher 协程随后将其改成 completed 并引发重复通知的竞态条件。
        """
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        # 先置 killed + 已通知再动手终止，防 watcher 随后改成 completed 又通知一遍
        job.status = "killed"
        job.notified = True
        if job.kill_func:
            job.kill_func()
        return True

    def pop_unnotified(self) -> list[Job]:
        """
        获取所有「已完成、属于后台任务、但还未曾通知过 Agent」的 Job 列表，
        并在获取的同时将其标志位 `notified` 设置为 True（即消费掉这些未读通知）。
        """
        done = [j for j in self._jobs.values() if j.background and j.status != "running" and not j.notified]
        for job in done:
            job.notified = True
        return done

    def pop_events(self) -> list[tuple[Job, list[str], int]]:
        """
        取走各 monitor job 攒下的事件并清空；取出即清空，两条通知通道就不会把同一行推两遍。
        """
        out = []
        for job in self._jobs.values():
            if not job.pending_events and not job.dropped_events:
                continue
            out.append((job, job.pending_events, job.dropped_events))
            job.pending_events = []
            job.dropped_events = 0
        return out

    def shutdown(self) -> int:
        """
        会话退出清理 (Shutdown)：
        强行终止所有还在运行的 Job（确保子进程不会变成孤儿进程活过会话周期），并返回被终止的任务数量。
        """
        running = self.running()
        for job in running:
            self.kill(job.id)
        return len(running)

def _process_group_kwargs() -> dict:
    # 单独起一个进程组，之后才能把子孙进程一并终止
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}

def _kill_process_tree(pid: int) -> None:
    """
    跨平台的进程树强力终止函数 (Kill Process Tree)：
    针对 Windows 和类 Unix (Linux/macOS) 系统采取不同的系统级杀死策略，
    确保连同该进程衍生的所有子孙进程一并干净彻底地干掉。
    """
    if os.name == "nt":
        # Windows 平台：使用 taskkill 命令通过 /F (强制) /T (终止指定的进程及由它启动的任何子进程)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
