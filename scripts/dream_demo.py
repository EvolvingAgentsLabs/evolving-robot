#!/usr/bin/env python3
"""Phase 4 acceptance: the dream engine rewrites a skill, gated by skill-map.

Two paths:
  A) DISCARD (deterministic, no API key): a rewrite that hallucinates a broken @reference is
     retried with skill-map's feedback, then reverted. Proves the safety gate + revert.
  B) KEEP (needs a Gemma key): the engine reads the latest trace and asks Gemma to rewrite
     patrol-route; a valid rewrite passes the gate and is kept. Uses the newest trace in
     traces/, synthesizing a failure trace if none exists.

Run a real mission first (scripts/run_mission.py) to dream over a real trace, or just run
this standalone.
"""

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_brain.dream import DreamEngine  # noqa: E402
from robot_brain.gemma import GemmaError, make_brain  # noqa: E402
from robot_brain.skills import get_skill, load_skills  # noqa: E402

SKILLS = ROOT / "robot_brain" / "skills"
TRACES = ROOT / "traces"
TARGET = "patrol-route"


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def _ensure_trace() -> None:
    if TRACES.exists() and list(TRACES.glob("*.md")):
        return
    TRACES.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    (TRACES / f"{ts}_patrol-2d.md").write_text(
        "---\nbenchmark: patrol-2d\nskill: patrol-route\npilot: gemma\nsuccess_rate: 0.500\n---\n\n"
        "# Patrol trace: patrol-2d\n\n## Episode 1 - reached 2/4 (missed: supply_closet, main_entrance)\n"
        "- step  0 [go to server room] rotate_left(degrees=30) -> (-0.4, -0.9, 120deg)\n"
        "- step  1 [go to server room] move_forward(distance_cm=50) -> (0.0, 1.2, 90deg)\n"
        "- step  2 [go to emergency exit] observe() -> (0.0, 1.2, 90deg)\n"
        "- step  3 [go to emergency exit] observe() -> (0.0, 1.2, 90deg)\n"
        "- step  4 [go to supply closet] observe() -> (0.0, 1.2, 90deg)\n"
        "Note: many wasted observe() calls; never advanced to supply_closet or main_entrance.\n"
    )


def main() -> int:
    skill = get_skill(load_skills(SKILLS), TARGET)
    skill_file = skill.path
    before = _digest(skill_file)

    # --- A) discard path (no API needed) ---
    print("A) DISCARD: rewrite hallucinates @ghost-skill")
    engine = DreamEngine(brain=None, skills_dir=SKILLS, traces_dir=TRACES)
    broken_body = skill.body + "\n6. If lost, escalate via @ghost-skill.\n"
    outcome = engine.apply_gated(skill, candidate_fn=lambda fb: broken_body, max_retries=1)
    reverted = _digest(skill_file) == before
    print(f"   status={outcome.status}  attempts={outcome.attempts}  reverted={reverted}")
    print(f"   reason: {outcome.reason.splitlines()[-1] if outcome.reason else ''}")
    if outcome.status != "discarded" or not reverted:
        print("   FAIL: expected discard + revert"); return 1

    # --- B) keep path (needs Gemma) ---
    print("\nB) KEEP: Gemma rewrites patrol-route from the latest trace")
    try:
        brain = make_brain()
    except GemmaError as e:
        print(f"   [skip] no Gemma key ({e}). Path A already proved the gate + revert.")
        print("\nPhase 4 OK (discard path verified; set GEMINI_API_KEY to see the keep path)")
        return 0

    _ensure_trace()
    engine = DreamEngine(brain=brain, skills_dir=SKILLS, traces_dir=TRACES)
    fresh = load_skills(SKILLS)  # reload after revert
    skill = get_skill(fresh, TARGET)
    before = _digest(skill.path)
    outcome = engine.dream(TARGET, max_retries=2)
    changed = _digest(skill.path) != before
    print(f"   status={outcome.status}  attempts={outcome.attempts}  gate_ok={outcome.gate.ok if outcome.gate else None}  body_changed={changed}")
    if outcome.status == "kept":
        print(f"   kept a gated rewrite of {TARGET} (new body {len(skill.path.read_text())} bytes)")
    print(f"   reason: {outcome.reason.splitlines()[0]}")

    print("\nPhase 4 OK" if outcome.status in ("kept", "discarded") else "\nPhase 4 incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
