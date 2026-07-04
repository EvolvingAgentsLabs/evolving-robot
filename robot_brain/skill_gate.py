"""skill_gate: run skill-map (`sm`) as a semantic safety gate over skill edits.

After the robot rewrites a ``SKILL.md`` (Phase 4), this gate decides whether the edit is
safe to keep: it runs ``sm scan --changed`` then ``sm check --json`` and rejects the edit if
skill-map reports any ``severity: error`` issue (broken references, name collisions, schema
violations). Warnings are surfaced but do not block.

skill-map requires Node >= 24. This module resolves an `sm` that runs on Node >= 24
automatically (an nvm-installed v24+), or honors ``SM_CMD`` if you set it explicitly.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class GateResult:
    ok: bool
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    def feedback(self) -> str:
        """A compact message to hand back to the model on rejection."""
        lines = []
        for i in self.errors:
            data = i.get("data") or {}
            tgt = data.get("target")
            lines.append(
                f"- [{i['analyzerId']}] {i['message'].splitlines()[0]}"
                + (f" (target: {tgt})" if tgt else "")
            )
        return "skill-map rejected the edit:\n" + "\n".join(lines)


def _sm_cmd() -> tuple[list[str], dict]:
    """Return (base_cmd, env) for invoking `sm` on Node >= 24.

    Order: SM_CMD override -> an nvm-installed node v24+ (prepended to PATH so
    sm's `env node` shebang resolves to it) -> bare `sm`.
    """
    env = os.environ.copy()
    override = os.environ.get("SM_CMD")
    if override:
        return shlex.split(override), env

    for pat in ("v24*", "v25*", "v26*"):
        hits = sorted(glob.glob(os.path.expanduser(f"~/.nvm/versions/node/{pat}/bin")))
        if hits:
            bindir = hits[-1]
            env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
            sm = os.path.join(bindir, "sm")
            if os.path.exists(sm):
                return [sm], env
    return ["sm"], env


def _run(cmd: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120
    )


def scan(skills_dir: Path, incremental: bool = True) -> None:
    """Refresh skill-map's snapshot of ``skills_dir``."""
    base, env = _sm_cmd()
    cmd = base + ["scan"] + (["--changed"] if incremental else [])
    _run(cmd, skills_dir, env)


def check(skills_dir: Path, node_path: Optional[str] = None) -> list[dict]:
    """Return skill-map's issue list (flat array) from the current snapshot."""
    base, env = _sm_cmd()
    cmd = base + ["check", "--json"]
    if node_path:
        cmd += ["-n", node_path]
    proc = _run(cmd, skills_dir, env)
    if proc.returncode == 2:  # operational error (bad flags, no DB, wrong node)
        raise RuntimeError(f"sm check failed (exit 2): {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []


def gate_skill(
    skills_dir: Path | str, node_path: Optional[str] = None, incremental: bool = True
) -> GateResult:
    """Scan + check ``skills_dir`` (optionally scoped to one edited skill).

    ``node_path`` is the root-relative skill path to scope the check to, e.g.
    ``.claude/skills/patrol-route/SKILL.md``.
    """
    skills_dir = Path(skills_dir)
    scan(skills_dir, incremental=incremental)
    issues = check(skills_dir, node_path=node_path)
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warn"]
    return GateResult(ok=not errors, errors=errors, warnings=warnings, issues=issues)
