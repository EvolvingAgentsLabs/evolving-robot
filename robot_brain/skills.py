"""Skill loading + progressive disclosure.

Skills live in the Claude Code layout (``<skills_dir>/.claude/skills/<name>/SKILL.md``) so
skill-map detects them natively. This module parses their frontmatter, lists metadata for
the system prompt (level 1 disclosure), and loads a full body on demand (level 2), the same
metadata-table-plus-``load_skill`` pattern skillos_x_robot uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_FIELD = re.compile(r"^(\w[\w-]*):\s*(.*)$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path

    @property
    def rel_path(self) -> str:
        """Root-relative skill path for ``sm check -n`` (posix separators)."""
        parts = self.path.parts
        i = parts.index(".claude")
        return "/".join(parts[i:])


def parse_skill(path: Path) -> Skill:
    text = path.read_text()
    m = _FRONTMATTER.match(text)
    meta: dict[str, str] = {}
    body = text
    if m:
        for line in m.group(1).splitlines():
            fm = _FIELD.match(line.strip())
            if fm:
                meta[fm.group(1)] = fm.group(2).strip()
        body = m.group(2).strip()
    name = meta.get("name") or path.parent.name
    return Skill(name=name, description=meta.get("description", ""), body=body, path=path)


def load_skills(skills_dir: Path | str) -> list[Skill]:
    root = Path(skills_dir) / ".claude" / "skills"
    return sorted(
        (parse_skill(p) for p in root.glob("*/SKILL.md")), key=lambda s: s.name
    )


def skill_metadata_table(skills: list[Skill]) -> str:
    """Level-1 disclosure: a compact name/description table for the system prompt."""
    rows = "\n".join(f"| {s.name} | {s.description} |" for s in skills)
    return "| skill | description |\n| --- | --- |\n" + rows


def get_skill(skills: list[Skill], name: str) -> Skill | None:
    """Level-2 disclosure: fetch one skill's full body by name."""
    return next((s for s in skills if s.name == name), None)


def write_skill_body(path: Path, new_body: str) -> None:
    """Replace a skill's body, preserving its frontmatter verbatim."""
    text = path.read_text()
    m = _FRONTMATTER.match(text)
    prefix = text[: m.start(2)] if m else ""
    path.write_text(prefix + new_body.strip() + "\n")
