"""
monitor 工具：盯住一条长跑的命令，把非空输出行作为事件推进对话。
"""
from pydantic_ai import RunContext

import permissions

from ..deps import AgentDeps
from .shell import run_command_self_check


async def monitor(
    ctx: RunContext[AgentDeps],
    command: str,
    description: str,
    timeout: int = 300,
    persistent: bool = False,
) -> str:
    """
    启动一个后台 monitor：非空输出行会通过 <monitor-event> 通知送达。启动后继续做你手头的事，不要轮询、也不要 sleep 等事件。

    按通知次数选择工具。只需要一次通知（如「构建完成时告诉我」），就用 run_command 的 run_in_background=True，并配一条满足条件就退出的命令，例如 `until grep -q "Ready" dev.log; do sleep 1; done`。
    每次发生都要通知时才用 monitor，例如 `tail -n 0 -F app.log | grep --line-buffered "ERROR"`，或者用 `while true` 循环轮询远端状态。

    管道的每一级都必须逐行刷新输出（flush），否则匹配到的行可能滞留在缓冲区中；grep 因此要加 --line-buffered。
    管道只传递 stdout，因此命令的 stderr 会绕过过滤器，未经筛选直接进入事件流。写成 `cmd 2>&1 | grep ...`，才能让 stderr 也经过过滤。

    沉默不等于成功：只匹配正常输出的过滤条件，进程崩掉时一样一声不吭，而这和「还在跑」看起来完全一样。失败特征也要覆盖，写 `grep -E --line-buffered "step=|Traceback|Error|FAILED"`，而不是只写 `grep --line-buffered "step="`。

    每条事件都要占用上下文。持续超出限流预算的 monitor 会被自动停掉，所以只过滤出你真会据此行动的那些行。

    Args:
        command: 要监控的 shell 命令
        description: 你在监控什么，几个词就行，它会出现在每一条通知里
        timeout: 多少秒之后停掉这个 monitor
        persistent: 让它跑满整个会话，忽略 timeout
    """
    if timeout <= 0:
        raise ValueError("timeout 必须是正整数")
    registry = ctx.deps.job_registry
    job = await registry.spawn_monitor(command, description, None if persistent else timeout)
    return (
        f"monitor 已启动，job id: {job.id}\n"
        f"输出日志：{job.log_path}（随时可用 read_file 查看已记录输出）\n"
        "非空输出行会通过 <monitor-event> 通知送达。不要原地等待；用 job_kill 可以随时停掉它。"
    )


# 复用 run_command 的高危命令自检，两个工具的参数同样叫 command
permissions.register_self_check("monitor", run_command_self_check)
