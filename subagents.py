"""
sub agent 模块：agent 类型定义（内置 + 自定义）、sub agent 专用 hooks，以及在 agent 型 job 里运行 sub agent 的执行器。

sub agent 就是第二个 Agent 实例：全新上下文（不传 message_history）、独立的 system prompt、裁剪过的工具集——工具集里没有 run_agent，天然不能再套娃。
"""

import asyncio
import dataclasses
import os
import platform
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.usage import UsageLimits
from pydantic_graph import End

import classifier
import permissions
from agent.deps import AgentDeps
from agent.file_state import ReadFileState
from agent.hooks import _handle_tool_error, _retry_on_error
from agent.model import model
from agent.reminders import build_job_reminder_text
from agent.tools.file import edit_file, read_file, write_file
from agent.tools.shell import run_command
from background_jobs import Job, JobRegistry
from memory.store import parse_frontmatter

# 单个 sub agent 最多允许的模型请求次数，防止跑飞空转
MAX_SUBAGENT_REQUESTS = 40


# ---------- sub agent 专用 hooks ----------

sub_hooks = Hooks()

# 调用工具的前置检查，给subagent实例的
@sub_hooks.on.tool_execute
async def _check_sub_permission(ctx, *, call, tool_def, args, handler):
    """
    compute_decision检查 + AUTO模式下的独立模型实例单独提问判断是否放行
    """
    if permissions.compute_decision(call.tool_name, args) == "allow":
        return await handler(args)
    # auto 模式的 classifier 本来就是无人值守的审批官，sub agent 照常用它；classifier 自己出错则回退到下面的人工审批
    if permissions.state.mode == permissions.AUTO:
        verdict = await classifier.classify(ctx.messages, call.tool_name, args)
        if not verdict.get("error"):
            if not verdict["should_block"]:
                return await handler(args)
            return (
                f"安全检查拦截了这次 {call.tool_name} 调用，没有执行。拦截理由：{verdict['reason']}。"
                "不要尝试绕过拦截，请换别的方式完成任务，或在最终报告里说明这一步没有做。"
            )
    choice = await _request_user_approval(ctx.deps.subagent_job, call.tool_name, args)
    if choice == "always":
        # 记进会话白名单，和主 agent 的「不再询问」共享同一份
        permissions.state.session_allowed.add(call.tool_name)
    if choice in ("once", "always"):
        return await handler(args)
    return (
        f"用户拒绝了这次 {call.tool_name} 调用。"
        "请换不需要审批的方式完成任务，或在最终报告里说明这一步没有做。"
    )


@dataclass
class PendingApproval:
    """
    一条从 sub agent 冒泡上来的审批请求，等用户给答复。
    """
    job: Job
    tool_name: str
    args: dict
    # 答复通过 future 回填，挂起等待的 sub agent 由此恢复
    future: asyncio.Future


PENDING_APPROVALS: list[PendingApproval] = []


async def _request_user_approval(job: Job, tool_name: str, args: dict) -> str:
    # get_running_loop() 抓取当前正在运行的事件循环
    # create_future() 创建一个新的 Future 对象，表示一个尚未完成的异步操作
    fut = asyncio.get_running_loop().create_future()
    PENDING_APPROVALS.append(PendingApproval(job, tool_name, args, fut))
    return await fut


def pop_pending_approval() -> PendingApproval | None:
    """
    取出下一条仍然有效的审批请求；对应 job 已经不在跑的直接丢弃。
    """
    while PENDING_APPROVALS:
        req = PENDING_APPROVALS.pop(0)
        if req.job.status == "running" and not req.future.done():
            return req
    return None


# 网络重试、工具异常兜底和主 agent 相同，复用主 hooks 的函数
sub_hooks.on.model_request(_retry_on_error)
sub_hooks.on.tool_execute_error(_handle_tool_error)


@sub_hooks.on.before_model_request
async def _sub_job_notifications(ctx, request_context):
    """
    sub agent 自己也能把耗时命令放后台，完成通知同样搭它下一次 model 请求的车。
    """
    text = build_job_reminder_text(ctx.deps.job_registry)
    if text is None:
        return request_context
    messages = list(request_context.messages)
    messages.append(ModelRequest(parts=[UserPromptPart(content=text)]))
    return dataclasses.replace(request_context, messages=messages)


# ---------- agent 类型定义 ----------

# sub agent 可用的全部工具；run_agent、ask_user_question、task_*、job_kill、monitor 都被排除在外
_TOOL_FUNCS = {
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "run_command": run_command,
}


def _build_tools(tool_names: list[str]) -> list:
    # 每个 Agent 实例配一份新的 Tool 包装，改文件的工具保持 sequential=True 串行约束
    return [
        Tool(_TOOL_FUNCS[name], sequential=True) if name in ("edit_file", "write_file") else _TOOL_FUNCS[name]
        for name in tool_names
    ]


@dataclass
class AgentType:
    name: str
    # 给主 agent 看的「什么时候用这个类型」，会注入进主 agent 的动态 instructions
    description: str
    tool_names: list[str]
    # sub agent 的 system prompt，完全替换主 agent 的那份，不是追加
    instructions: str
    # "built-in" 或自定义 agent 的定义文件路径
    source: str
    # 对应的 Agent 实例，_register 时装配
    agent: Agent = field(init=False, repr=False)


def _sub_env_context() -> str:
    # sub agent 的环境信息：只给工作目录 / 系统 / 日期和项目 AGENTS.md，不注入记忆索引和 task 引导
    cwd = os.getcwd()
    parts = [
        "下面是一些环境信息：",
        f"- 工作目录：{cwd}",
        f"- 操作系统：{platform.system()}",
        f"- 今天的日期：{date.today().isoformat()}",
    ]
    agents_md = os.path.join(cwd, "AGENTS.md")
    if os.path.isfile(agents_md):
        try:
            content = open(agents_md, encoding="utf-8").read()
        except OSError:
            content = ""
        if content.strip():
            parts.append("")
            parts.append("以下是项目的 AGENTS.md，请遵循其中的约定：")
            parts.append(content)
    return "\n".join(parts)


_TYPES: dict[str, AgentType] = {}


def _register(atype: AgentType) -> None:
    atype.agent = Agent(
        model,
        instructions=[atype.instructions, _sub_env_context],
        tools=_build_tools(atype.tool_names),
        deps_type=AgentDeps,
        capabilities=[sub_hooks],
    )
    _TYPES[atype.name] = atype


EXPLORE_INSTRUCTIONS = (
    "你是一个只读的代码调查 agent，负责在项目里搜索、阅读代码，回答交给你的问题。\n"
    "严禁做任何修改：不要创建或改动文件，run_command 只用于查看和搜索（ls、grep、find、cat 这类），不要执行有副作用的命令。\n"
    "调查完成后输出一份简洁的报告：结论放前面，附上关键文件路径作为依据。调用方只能看到你最后输出的报告，看不到中间过程，所以报告要自包含。"
)

GENERAL_INSTRUCTIONS = (
    "你是主 agent 派出的 sub agent，独立完成交给你的任务，可以读写文件和执行命令。\n"
    "修改已有文件前必须先用 read_file 读取它。改动局部内容时优先用 edit_file，新建文件或整体重写才用 write_file。\n"
    "没有人会回答你的提问，拿不准时按最合理的方式处理，并在报告里说明你的取舍。\n"
    "完成后输出一份简洁的最终报告：做了什么、改了哪些文件、关键发现。调用方只能看到这份报告，看不到中间过程。"
)

_register(AgentType(
    name="explore",
    description="只读的代码调查 agent：搜索、阅读代码来回答「X 在哪」「Y 是怎么实现的」这类问题，不做任何修改",
    tool_names=["read_file", "run_command"],
    instructions=EXPLORE_INSTRUCTIONS,
    source="built-in",
))

_register(AgentType(
    name="general",
    description="可读写文件、执行命令的通用 agent，适合写测试、修独立 bug 这类多步骤的独立子任务",
    tool_names=list(_TOOL_FUNCS),
    instructions=GENERAL_INSTRUCTIONS,
    source="built-in",
))


# ---------- 自定义 agent 加载 ----------

def load_agent_types() -> list[AgentType]:
    """
    从工作目录的 .my-code-agent/agents/*.md 加载自定义 agent，
    并在加载完成后立即注册/实例化 `AgentType.agent`。

    也就是说，返回值不仅是“类型清单”，而且每个 `AgentType` 都已绑定好对应的 `Agent` 实例，
    供后续直接调用 `atype.agent.iter(...)` 或通过 `get_agent_type(name)` 取出使用。

    定义：frontmatter 提供 name / description / tools，markdown 正文就是 system prompt。
    举例：
        ---
        name: bugfix
        description: 修复 bug、写测试、验证结果的 sub agent
        tools: read_file, edit_file, write_file, run_command
        ---
        你是一个负责修 bug 的子代理。
        请先读代码，再写最小修复，最后给出简洁结论。
        不要擅自改动无关逻辑。
    """
    loaded = []
    agents_dir = Path.cwd() / ".my-code-agent" / "agents"
    if not agents_dir.is_dir():
        return loaded
    for path in sorted(agents_dir.glob("*.md")):
        fields = parse_frontmatter(path)
        # 没有 frontmatter 的 md 不当作 agent 定义（可能是目录里的说明文档），静默跳过
        if not fields:
            continue
        name = fields.get("name", "")
        description = fields.get("description", "")
        if not name or not description:
            raise RuntimeError(f"自定义 agent {path} 的 frontmatter 缺少必填的 name 或 description 字段")
        if name in _TYPES:
            raise RuntimeError(f"自定义 agent {path} 的 name「{name}」与已有 agent 类型重名")
        body = _read_body(path)
        if not body:
            raise RuntimeError(f"自定义 agent {path} 的正文为空，正文就是这个 agent 的 system prompt")
        atype = AgentType(
            name=name,
            description=description,
            tool_names=_parse_tools_field(fields.get("tools"), path),
            instructions=body,
            source=str(path),
        )
        _register(atype)
        loaded.append(atype)
    return loaded


def _read_body(path: Path) -> str:
    # 去掉头部 frontmatter，剩下的正文就是 system prompt
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        pieces = text.split("---", 2)
        if len(pieces) == 3:
            return pieces[2].strip()
    return text.strip()


def _parse_tools_field(value: str | None, path: Path) -> list[str]:
    names = [n.strip() for n in (value or "").replace("，", ",").split(",") if n.strip()]
    # tools 字段缺省或留空 = 全集，写了就按逗号分隔的白名单裁剪
    if not names:
        return list(_TOOL_FUNCS)
    unknown = [n for n in names if n not in _TOOL_FUNCS]
    if unknown:
        raise RuntimeError(
            f"自定义 agent {path} 的 tools 字段包含未知工具：{', '.join(unknown)}，可用工具：{', '.join(_TOOL_FUNCS)}"
        )
    return names


def get_agent_type(name: str) -> AgentType | None:
    return _TYPES.get(name)


def list_agent_types() -> list[AgentType]:
    return list(_TYPES.values())


def format_agent_listing() -> str:
    """
    每个 agent 类型一行，注入进主 agent 的动态 instructions。
    """
    return "\n".join(
        f"- {atype.name}：{atype.description}（可用工具：{', '.join(atype.tool_names)}）"
        for atype in _TYPES.values()
    )


# ---------- 执行器 ----------

async def run_subagent(atype: AgentType, prompt: str, job: Job, parent_deps: AgentDeps) -> None:
    """
    在 agent 型 job 里运行一个 sub agent：全新上下文，过程实时写进 job 的日志文件，最终报告存到 job.result。
    """
    # sub agent 自带一份 job 注册表，它起的进程随它的生命周期清理，日志嵌在主会话的 jobs 目录下
    sub_registry = JobRegistry(session_id=f"{parent_deps.job_registry.session_id}/{job.id}")
    # 从主会话 deps 派生：读文件状态隔离、job 注册表换成自己的，其余字段原样继承
    # （file_history 就在继承之列，sub agent 改的文件也能被 /rewind 回退）
    deps = dataclasses.replace(
        parent_deps,
        read_file_state=ReadFileState(),
        tasks_store=None,
        job_registry=sub_registry,
        subagent_job=job,
    )
    log = open(job.log_path, "a", encoding="utf-8")
    log.write(f"=== sub agent {job.id}（{atype.name}）===\nprompt: {prompt}\n\n")
    log.flush()
    try:
        async with atype.agent.iter(
            prompt, deps=deps,
            usage_limits=UsageLimits(request_limit=MAX_SUBAGENT_REQUESTS),
        ) as run:
            node = run.next_node
            while not isinstance(node, End):
                node = await run.next(node)
                if Agent.is_call_tools_node(node):
                    for part in node.model_response.parts:
                        _log_part(log, part)
                elif Agent.is_model_request_node(node):
                    for part in node.request.parts:
                        if part.part_kind == "tool-return":
                            _log_part(log, part)
        job.result = run.result.output or "(sub agent 没有输出报告)"
        log.write(f"=== 最终报告 ===\n{job.result}\n")
    finally:
        if job.status == "killed":
            log.write("=== 已被终止 ===\n")
        log.close()
        sub_registry.shutdown()


def _squash(text, limit: int) -> str:
    # 先切片再压行：工具输出可能有几 MB，不值得为留 500 字符做全量 split/join
    text = " ".join(str(text)[: limit * 4].split())
    return text if len(text) <= limit else text[:limit] + "..."


def _format_part(part) -> str | None:
    kind = part.part_kind
    if kind == "text":
        return f"● {part.content}"
    if kind == "thinking":
        return f"✻ {part.content}"
    if kind == "tool-call":
        return f"⏺ {part.tool_name}({_squash(part.args, 200)})"
    if kind == "tool-return" and part.outcome == "retried":
        return f"✘ {part.tool_name}: {_squash(part.content, 200)}"
    if kind == "tool-return":
        return f"✔ {part.tool_name} -> {_squash(part.content, 500)}"
    return None


def _log_part(log, part) -> None:
    line = _format_part(part)
    if line is None:
        return
    log.write(line + "\n\n")
    log.flush()
