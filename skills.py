"""
Skill 发现与渐进式加载：启动时只注入元数据，任务匹配后再读取完整 SKILL.md。
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.exceptions import ModelRetry


USER_SKILLS_DIR = Path.home() / ".my-code-agent" / "skills"
PROJECT_SKILLS_SUBDIR = Path(".my-code-agent") / "skills"
FRONTMATTER_MAX_LINES = 100
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# 发现阶段只保存元数据和路径，正文留到命中任务后再读
@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    source: str


def _decode_scalar(value: str) -> str:
    """
    解析 name 和 description 用到的单行 YAML 字符串，不额外引入 YAML 依赖。
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _read_frontmatter(path: Path) -> dict[str, str]:
    """
    只读取文件头的 frontmatter，发现阶段不会把指令正文读进内存。
    """
    try:
        with open(path, encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return {}
            fields: dict[str, str] = {}
            for _ in range(FRONTMATTER_MAX_LINES):
                line = f.readline()
                if not line:
                    return {}
                # 遇到第二个分隔符就结束，后面的正文不参与发现
                if line.strip() == "---":
                    return fields
                if line.lstrip().startswith("#"):
                    continue
                key, sep, value = line.partition(":")
                if sep:
                    fields[key.strip()] = _decode_scalar(value)
    except (OSError, UnicodeError):
        return {}
    return {}


def _is_valid_metadata(fields: dict[str, str], directory_name: str) -> bool:
    name = fields.get("name", "")
    description = fields.get("description", "")
    return (
        name == directory_name
        and 0 < len(name) <= MAX_NAME_CHARS
        and SKILL_NAME_PATTERN.fullmatch(name) is not None
        and 0 < len(description) <= MAX_DESCRIPTION_CHARS
        and "<" not in name
        and ">" not in name
        and "<" not in description
        and ">" not in description
    )


def _scan_root(root: Path, source: str) -> list[SkillInfo]:
    """
    只发现 root/<skill-name>/SKILL.md；更深层的目录属于资源，不再当成 skill 扫描。
    """
    try:
        directories = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []

    result = []
    for directory in directories:
        path = directory / "SKILL.md"
        if not path.is_file():
            continue
        fields = _read_frontmatter(path)
        if not _is_valid_metadata(fields, directory.name):
            continue
        result.append(SkillInfo(
            name=fields["name"],
            description=fields["description"],
            path=path,
            source=source,
        ))
    return result


def discover_skills(
    cwd: Path | None = None,
    user_skills_dir: Path | None = None,
) -> list[SkillInfo]:
    """
    合并个人级和项目级 skill；同名时离当前项目更近的项目级定义覆盖个人级。
    """
    project_root = (cwd or Path.cwd()) / PROJECT_SKILLS_SUBDIR
    user_root = user_skills_dir or USER_SKILLS_DIR
    # 先收集个人级，再按名字写入项目级，自然实现同名覆盖
    by_name = {
        skill.name: skill
        for skill in _scan_root(user_root, "user")
    }
    for skill in _scan_root(project_root, "project"):
        by_name[skill.name] = skill
    return sorted(by_name.values(), key=lambda skill: skill.name)


def format_skill_listing(skills: list[SkillInfo]) -> str:
    """
    生成注入 system prompt 的轻量清单，这里不会出现任何 SKILL.md 正文。
    """
    if not skills:
        return ""
    lines = [
        "可用 skills（这里只包含名称和描述）：",
    ]
    lines.extend(f"- {skill.name}: {skill.description}" for skill in skills)
    # 清单同时说明何时调用加载工具，用户只需描述任务
    lines.extend([
        "",
        "当某个 skill 匹配用户任务时，继续处理前先调用 load_skill 读取它；不要加载无关 skill。",
    ])
    return "\n".join(lines)


def _read_body(path: Path) -> str:
    """
    第二层加载发生在这里：读取整个文件，并去掉只用于发现的 frontmatter。
    """
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content.strip()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1:]).strip()
    return content.strip()


def read_skill(
    name: str,
    cwd: Path | None = None,
    user_skills_dir: Path | None = None,
) -> str:
    # 按当前目录重新查找，沿用发现阶段的同名覆盖规则
    skills = {skill.name: skill for skill in discover_skills(cwd, user_skills_dir)}
    skill = skills.get(name.strip())
    if skill is None:
        available = ", ".join(skills) or "(none)"
        raise ModelRetry(f"未知 skill：{name}。可用 skills：{available}")
    try:
        body = _read_body(skill.path)
    except (OSError, UnicodeError) as error:
        raise ModelRetry(f"无法读取 skill {name}：{error}")
    if not body:
        raise ModelRetry(f"skill {name} 没有指令正文")
    # 附上根目录，模型才能解析正文中参考资料和脚本的相对路径
    return (
        f"skill {skill.name} 的根目录：{skill.path.parent}\n\n"
        f"{body}"
    )


def load_skill(name: str) -> str:
    """
    当可用 skill 的描述匹配当前任务后，读取它的完整指令。

    Args:
        name: 可用 skills 清单里的准确名称
    """
    return read_skill(name)


__all__ = [
    "SkillInfo",
    "discover_skills",
    "format_skill_listing",
    "load_skill",
    "read_skill",
]
