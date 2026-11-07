"""
图片输入：识别图片文件、读成模型能接收的格式，以及从系统剪贴板里取图。

本文件负责打通终端（CLI）Agent 的多模态能力，核心职责包括：
1. **格式校验与加载**：通过白名单识别本地常见图片后缀，并将其无损转化为大模型标准二进制结构 (BinaryContent)。
2. **Prompt 混排占位**：智能解析文本中的 `[Image #N]` 占位符，将图片附件精准嵌入正文对应位置。
3. **终端防乱码保护**：提供 `describe` 格式化工具，将二进制图片安全降级为文本摘要，防止控制台刷屏。
4. **跨平台剪贴板截屏取图**：通过系统原生指令（macOS/Linux/Windows）无缝实现“截图粘贴 -> 自动落盘 -> 载入内存”。
"""

import os
import platform
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from pydantic_ai import BinaryContent

# 定义支持的图片白名单及对应的 MIME 类型
IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 粘贴进来的图片统一存放在用户家目录下的 clipboard 文件夹里，一次一个文件，便于持久化追溯
CLIPBOARD_DIR = Path.home() / ".my-code-agent" / "clipboard"

# 正则表达式：用于匹配文本中形如 [Image #1], [Image #2] 的图片占位符
_IMAGE_PLACEHOLDER = re.compile(r"\[Image #(\d+)\]")

# path下的文件是否是图片（白名单校验）
def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS

# 将path下的本地图片文件读成 BinaryContent（模型层能直接接收的标准多模态对象）
def load_image(path: str) -> BinaryContent:
    # 1. 根据后缀获取对应的 MIME 类型（如 image/png）
    media_type = IMAGE_EXTENSIONS[os.path.splitext(path)[1].lower()]
    # 2. 以二进制只读模式打开并读取全量字节
    with open(path, "rb") as f:
        data = f.read()
    # 3. 返回 BinaryContent 对象，包含字节数据和 MIME 类型
    return BinaryContent(data=data, media_type=media_type)

# 把text里的 [Image #N] 占位符换成对应的图片对象，保留其在文本内的上下文位置
def build_user_content(
    text: str, attachments: list[BinaryContent]
) -> str | list[str | BinaryContent]:
    if not attachments:
        return text

    content: list[str | BinaryContent] = []
    cursor = 0
    inserted = set()

    # 遍历文本中所有的图片占位符
    for match in _IMAGE_PLACEHOLDER.finditer(text):
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(attachments):
            continue
        before = text[cursor:match.start()]
        if before:
            content.append(before)
        content.append(attachments[index])
        inserted.add(index)
        cursor = match.end()

    after = text[cursor:]
    if after:
        content.append(after)

    # 兜底逻辑：没有在文本里写对应占位符的附件，按原顺序统一放在末尾
    content.extend(image for index, image in enumerate(attachments) if index not in inserted)
    return content

# 打印到终端时，图片对象不能直接输出，否则会刷屏乱码。describe() 会把图片对象降级为一行摘要，其他内容原样返回。
def describe(content):
    if isinstance(content, BinaryContent):
        return f"[图片 {content.media_type}，{len(content.data) / 1024:.0f} KB]"
    return content


# 各系统检查剪贴板里有没有图片的命令
_CHECK_COMMANDS = {
    "Darwin": ["osascript", "-e", "clipboard info for «class PNGf»"],
    "Linux": ["sh", "-c", "xclip -selection clipboard -t TARGETS -o | grep -q image/"],
    "Windows": ["powershell", "-NoProfile", "-Command", "(Get-Clipboard -Format Image) -ne $null"],
}


def _save_command(path: str) -> list[str]:
    """
    【系统适配辅助函数】：根据当前操作系统生成“把剪贴板图片写成指定路径文件”的shell命令[cite: 8]。
    """
    return {
        "Darwin": [
            "osascript",
            "-e", "set png_data to (the clipboard as «class PNGf»)",
            "-e", f'set fp to open for access POSIX file "{path}" with write permission',
            "-e", "write png_data to fp",
            "-e", "close access fp",
        ],
        "Linux": ["sh", "-c", f'xclip -selection clipboard -t image/png -o > "{path}"'],
        "Windows": [
            "powershell", "-NoProfile", "-Command",
            f"$img = Get-Clipboard -Format Image; if ($img) {{ $img.Save('{path}') }}",
        ],
    }[platform.system()] #写法很好，这边是key，直接用 platform.system() 取 dict 的 key，返回对应的 list[str] 命令


def read_clipboard_image() -> BinaryContent | None:
    """
    【核心创新函数】：从系统剪贴板中读取图片，如果没有图则返回 None[cite: 8]。
    由于终端无法直接捕捉剪贴板事件，这里通过子进程调用系统原生工具进行“桥接转存”。
    """
    system = platform.system()
    check = _CHECK_COMMANDS.get(system)
    if check is None:
        return None

    # 每次粘贴存一个新文件，名字带时间戳，方便回头翻看贴过哪些图
    CLIPBOARD_DIR.mkdir(parents=True, exist_ok=True)
    path = str(CLIPBOARD_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.png")

    try:
        # 1. 探测剪贴板里到底有没有图
        probe = subprocess.run(check, capture_output=True, timeout=5)
        if probe.returncode != 0:
            return None
        # macOS 和 Windows 的命令对空剪贴板也返回 0，得看输出内容才知道有没有图
        if system == "Darwin" and not probe.stdout.strip():
            return None
        if system == "Windows" and b"True" not in probe.stdout:
            return None
        # 2. 将剪贴板中的图片保存为本地临时文件
        saved = subprocess.run(_save_command(path), capture_output=True, timeout=10)
        # 保存命令失败不会抛异常，得自己看退出码
        if saved.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    # 3. 校验文件是否真实有效且不为空
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    try:
        # 4. 调用 load_image 读进内存转化为 BinaryContent 供大模型使用
        return load_image(path)
    except OSError:
        return None
