"""
plan 模式工具：enter_plan_mode 让模型主动进入 plan 模式，exit_plan_mode 提交计划请用户审批。
"""
from pydantic_ai import RunContext

import permissions

from ..deps import AgentDeps


def enter_plan_mode() -> str:
    """
    进入 plan 模式：先只读地调研代码、设计方案，把计划写好交给用户审批，批准后再改代码。
    适合比较复杂的实现任务，比如新增功能、有多种可行方案需要取舍、要改动多个文件，或者需求不明确、要先调研才能动手。
    简单的任务不要用，比如改错别字、改几行代码、用户已经给出了具体步骤，或者只是查询信息。
    """
    if permissions.state.mode == permissions.PLAN:
        return "当前已经处于 plan 模式。"
    permissions.set_mode(permissions.PLAN)
    return "已进入 plan 模式。接下来专注于探索代码、设计方案，在用户批准计划之前不要修改项目文件。"


async def exit_plan_mode(ctx: RunContext[AgentDeps]) -> str:
    """
    计划写进 plan 文件后，调用它请用户审批。
    它不接收计划内容，会直接读取 plan 文件展示给用户。只在规划需要写代码的任务时使用，纯调研任务不要用。
    """
    if permissions.state.mode != permissions.PLAN:
        return "当前不在 plan 模式。如果计划已经批准，直接继续实施。"
    plan_file = ctx.deps.plan_file
    plan = plan_file.read_text(encoding="utf-8").strip() if plan_file.exists() else ""
    if not plan:
        return f"plan 文件 {plan_file} 还是空的，先把计划写进去，再调用 exit_plan_mode。"

    choice, feedback = await permissions.prompt_plan_approval(plan, plan_file)
    if choice == "approve":
        # 批准：回到进入 plan 之前的模式，计划全文作为工具结果交回模型
        permissions.set_mode(permissions.state.pre_plan_mode)
        return (
            "用户批准了你的计划，现在可以开始写代码了。步骤较多的话，先用 task_create 把计划拆成 task。\n\n"
            f"计划保存在 {plan_file}，实施过程中可以随时回看。\n\n"
            f"## 批准的计划\n{plan}"
        )
    if feedback:
        return f"用户没有批准这份计划，仍处于 plan 模式。用户的修改意见：\n{feedback}\n请据此修改 plan 文件，再调用 exit_plan_mode 重新提交。"
    return "用户没有批准这份计划，仍处于 plan 模式。请停下来，等用户告诉你接下来怎么做。"
