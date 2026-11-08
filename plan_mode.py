"""
plan 模式：plan 文件的位置，以及让模型对当前模式的认知和实际模式保持一致的 system-reminder。
"""
from pathlib import Path

from pydantic_ai.messages import ModelResponse, UserPromptPart

import permissions

PLANS_DIR = Path.home() / ".my-claude-code" / "plans"

# 上一条 plan 提醒之后，每过这么多轮请求，补一条精简版
PLAN_REMINDER_TURNS_BETWEEN = 5

# 三种提醒的开头，扫描历史时靠它们认出提醒的类型
_SENTINELS = {
    "full": "plan 模式已开启。",
    "sparse": "plan 模式仍在生效",
    "exit": "已退出 plan 模式。",
}


def plan_file_path(session_id: str) -> Path:
    return PLANS_DIR / f"{session_id}.md"


def ensure_plans_dir() -> None:
    # 提醒里让模型直接用 write_file 创建 plan 文件，目录必须提前建好
    PLANS_DIR.mkdir(parents=True, exist_ok=True)


def _wrap(text: str) -> str:
    return f"<system-reminder>\n{text}\n</system-reminder>"


def full_reminder(plan_file: Path) -> str:
    if plan_file.exists():
        file_info = f"plan 文件已存在：{plan_file}。继续同一个任务就用 edit_file 增量修改；如果用户现在要做的是另一件事，就用 write_file 覆盖重写。"
    else:
        file_info = f"还没有 plan 文件，请用 write_file 在 {plan_file} 创建。"
    return _wrap(
        _SENTINELS["full"]
        + "用户希望先看到方案再动手：除了下面的 plan 文件，你不能修改任何文件，也不能执行任何会改动系统的命令（包括修改配置、git commit）。这条约束优先于你收到的其他指令。\n\n"
        + "## plan 文件\n"
        + f"{file_info}\n"
        + "这是你唯一可以写入的文件，请在里面逐步完善计划。\n\n"
        + "## 工作流程\n"
        + "1. 探索：弄清用户的需求和相关代码，优先寻找可以复用的函数和写法。用 run_agent 派 explore agent 调查，最多 3 个并行；通常 1 个就够，范围不确定或涉及多处代码时再多派。\n"
        + "2. 设计：用 run_agent 派 plan agent，把需求和探索结果（文件名、代码路径）交给它设计实现方案。只有很简单的改动才跳过这一步。\n"
        + "3. 审视：阅读 agent 指出的关键文件，确认方案符合用户的需求；还有疑问就用 ask_user_question 问用户，不要自作主张。\n"
        + "4. 写计划：把最终方案写进 plan 文件。开头说明为什么要做这个改动，只写你推荐的方案，列出要修改的关键文件和要复用的现有代码，最后写清楚怎么验证。计划要简洁到能快速看完，又详细到能直接照着执行。\n"
        + "5. 提交：计划写好后，调用 exit_plan_mode 请用户审批。\n\n"
        + "每轮对话只能以两种方式结束：调用 ask_user_question 澄清需求，或调用 exit_plan_mode 提交计划。不要用文字或 ask_user_question 询问「计划可以吗」，请求审批正是 exit_plan_mode 的职责。"
    )


def sparse_reminder(plan_file: Path) -> str:
    return _wrap(
        _SENTINELS["sparse"]
        + f"（完整规则见前文）。除 plan 文件（{plan_file}）外只读。每轮对话以 ask_user_question 或 exit_plan_mode 结束，不要用文字询问计划是否可行。"
    )


def exit_reminder(plan_file: Path) -> str:
    text = _SENTINELS["exit"] + "现在可以编辑文件、执行命令了。"
    if plan_file.exists():
        text += f"plan 文件在 {plan_file}，需要时可以回看。"
    return _wrap(text)


def _last_plan_reminder(messages) -> tuple[str | None, int]:
    """
    反向扫描历史，找到最近一条 plan 提醒，返回它的类型，以及之后过了多少轮请求。
    """
    turns = 0
    for msg in reversed(messages):
        if isinstance(msg, ModelResponse):
            turns += 1
            continue
        for part in msg.parts:
            if not (isinstance(part, UserPromptPart) and isinstance(part.content, str)):
                continue
            for kind, sentinel in _SENTINELS.items():
                if part.content.startswith(f"<system-reminder>\n{sentinel}"):
                    return kind, turns
    return None, turns


def build_plan_reminder(messages, plan_file: Path) -> str | None:
    """
    对比模型以为的模式（历史里最近一条 plan 提醒）和实际模式，挑出能消除两者差异的提醒。
    """
    # last：最近一条 plan 提醒的类型，"full" 完整版、"sparse" 精简版、"exit" 退出提醒，历史里没有则为 None
    # turns：这条提醒之后过了几轮请求
    last, turns = _last_plan_reminder(messages)
    believes_plan = last in ("full", "sparse")
    if permissions.state.mode != permissions.PLAN:
        # 已经离开 plan 模式，模型却还以为在规划：补一条退出提醒
        return exit_reminder(plan_file) if believes_plan else None
    if not believes_plan:
        # 刚进入 plan 模式，或者完整提醒被压缩掉了：发完整版
        return full_reminder(plan_file)
    if turns >= PLAN_REMINDER_TURNS_BETWEEN:
        return sparse_reminder(plan_file)
    return None
