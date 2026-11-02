"""
run_agent 工具：把任务委托给 sub agent，它作为 agent 型后台 job 走和后台命令同一个注册表。
"""
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

import subagents

from ..deps import AgentDeps
from .shell import background_notice


async def run_agent(
    ctx: RunContext[AgentDeps],
    description: str,
    prompt: str,
    agent_type: str = "general",
) -> str:
    """
    把任务委托给一个 sub agent，它在全新的隔离上下文里工作，只把最终报告交回给你。

    sub agent 一律在后台运行：本调用会立即返回 job id，等它完成后你会收到 <task-notification> 通知，最终报告就在通知的 <result> 字段里，不要主动轮询等待。sub agent 对当前对话一无所知，prompt 要像给新同事交代任务一样写清楚：目标、必要的背景、报告里要覆盖什么。它的报告用户看不到，需要你转述要点。可用的 agent 类型见 system prompt。

    Args:
        description: 任务的简短概括（3-5 个词），会显示在 job 列表里
        prompt: 给 sub agent 的完整任务描述
        agent_type: 使用哪种 agent 类型
    """
    atype = subagents.get_agent_type(agent_type)
    if atype is None:
        available = ", ".join(t.name for t in subagents.list_agent_types())
        raise ModelRetry(f"agent 类型 {agent_type} 不存在，可用类型：{available}")

    registry = ctx.deps.job_registry
    job = await registry.spawn_agent(
        description,
        lambda job: subagents.run_subagent(atype, prompt, job, ctx.deps),
    )
    return background_notice(job, f"sub agent 已在后台开始工作，job id: {job.id}，完成通知会在 <result> 字段附带它的最终报告")
