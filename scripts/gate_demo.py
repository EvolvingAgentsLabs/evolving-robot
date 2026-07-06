#!/usr/bin/env python3
"""Phase 3 acceptance: skill-map gates a skill edit.

Shows the gate accepting the clean skill set, rejecting an edit that introduces a broken
cross-reference (with the reason skill-map gives), and accepting again after revert.

Requires `sm` on Node >= 24 (auto-resolved from an nvm v24+ install, or set SM_CMD).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_brain.skill_gate import gate_skill  # noqa: E402

SKILLS = ROOT / "robot_brain" / "skills"
TARGET = ".claude/skills/patient-check/SKILL.md"


def main() -> int:
    skill_file = SKILLS / TARGET

    print("1) clean skill set")
    r = gate_skill(SKILLS, node_path=TARGET)
    print(f"   ok={r.ok}  errors={len(r.errors)}  warnings={len(r.warnings)}")
    if not r.ok:
        print("   unexpected: clean set should pass"); return 1

    print("\n2) introduce a broken cross-reference (@ghost-skill)")
    original = skill_file.read_text()
    try:
        skill_file.write_text(original + "\n6. If lost, escalate via @ghost-skill.\n")
        r = gate_skill(SKILLS, node_path=TARGET)
        print(f"   ok={r.ok}  errors={len(r.errors)}")
        if r.ok:
            print("   unexpected: broken ref should be rejected"); return 1
        print("   " + r.feedback().replace("\n", "\n   "))
    finally:
        skill_file.write_text(original)

    print("\n3) after revert")
    r = gate_skill(SKILLS, node_path=TARGET)
    print(f"   ok={r.ok}  errors={len(r.errors)}")

    print("\nPhase 3 OK" if r.ok else "\nPhase 3 FAILED")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
