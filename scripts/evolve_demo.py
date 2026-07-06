#!/usr/bin/env python3
"""Phase 5 acceptance: the robot versions its skills with agentvcs and self-manages
commit / rollback / freeze by score.

Deterministic and offline: it runs on a throwaway copy of the skills tree, with a stubbed
"dream" (edits the skill files) and injected mission scores, so the agentvcs mechanics are
exercised and verified without the sim or an API key. The real loop wires ``dream_fn`` to
DreamEngine.dream and ``score_fn`` to a live mission run (see the note at the end).

Scenarios:
  1. BAD evolution  -> score regresses -> rollback(reason=...) restores the skill files and
     records why in the durable ledger.
  2. GOOD evolution -> score holds -> keep, then freeze (crystallize) the verified skill set.
"""

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_brain.evolve import EvolutionController  # noqa: E402
from robot_brain.skills import get_skill, load_skills, write_skill_body  # noqa: E402

SRC_SKILLS = ROOT / "robot_brain" / "skills"
TARGET = "patient-check"


def _score_seq(*scores):
    it = iter(scores)
    return lambda: next(it)


def _stub_dream(work_dir, new_body, reason):
    def dream_fn():
        skill = get_skill(load_skills(work_dir), TARGET)
        write_skill_body(skill.path, new_body)
        return SimpleNamespace(status="kept", reason=reason)
    return dream_fn


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="evolve-"))
    shutil.copytree(SRC_SKILLS / ".claude", tmp / ".claude")
    try:
        ctrl = EvolutionController(tmp)
        baseline_body = get_skill(load_skills(tmp), TARGET).body
        print(f"repo head after init: {str(ctrl.head())[:12] if ctrl.head() else None}")

        # --- Scenario 1: bad evolution -> rollback ---
        print("\n1) BAD evolution (score regresses)")
        bad_body = "## Overview\n\nGlance at each door. Skip the patients.\n"  # degraded, drops @refs
        r1 = ctrl.evolve_step(
            dream_fn=_stub_dream(tmp, bad_body, "shortened (demo-bad)"),
            score_fn=_score_seq(1.0, 0.4),  # baseline 1.0, evolved 0.4
            target_skill=TARGET,
        )
        restored = get_skill(load_skills(tmp), TARGET).body == baseline_body
        print(f"   evolved={r1.evolved} kept={r1.kept} rolled_back={r1.rolled_back}")
        print(f"   skill restored to baseline: {restored}")
        print(f"   ledger reason: {ctrl.rollbacks()[-1]['reason']}")
        if not (r1.rolled_back and restored):
            print("   FAIL: expected rollback + restore"); return 1

        # --- Scenario 2: good evolution -> keep -> freeze ---
        print("\n2) GOOD evolution (score holds) -> freeze")
        good_body = (
            baseline_body
            + "\n## Bedside check\n\nIf a patient's status reads 'unknown' from the doorway, "
            "approach within 0.8 m and read it before moving on.\n"
        )
        r2 = ctrl.evolve_step(
            dream_fn=_stub_dream(tmp, good_body, "approach the bed when status is unknown (demo-good)"),
            score_fn=_score_seq(1.0, 1.0),
            target_skill=TARGET,
        )
        print(f"   evolved={r2.evolved} kept={r2.kept} rolled_back={r2.rolled_back}")
        kept_ok = get_skill(load_skills(tmp), TARGET).body == good_body.strip()
        print(f"   evolved skill kept on disk: {kept_ok}")

        frozen_oid, artifact = ctrl.freeze(message="freeze: verified night-rounds skill set")
        state = ctrl.repo.objects.read_obj(frozen_oid).get("state")
        print(f"   frozen commit {str(frozen_oid)[:12]} state={state}")

        print("\n   history (newest first):")
        for oid, c in ctrl.history()[:5]:
            print(f"     {oid[:12]} [{c['state']:>12}] {c['message']}")

        ok = r1.rolled_back and restored and r2.kept and kept_ok and state == "crystallized"
        print("\nPhase 5 OK" if ok else "\nPhase 5 FAILED")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Live wiring (not run here): the same controller with real inputs -
#   from robot_brain.dream import DreamEngine
#   dream = DreamEngine(make_brain(), SRC_SKILLS, ROOT / "traces")
#   ctrl.evolve_step(dream_fn=lambda: dream.dream("patient-check"),
#                    score_fn=lambda: rounds_score())   # a live odyssey mission


if __name__ == "__main__":
    raise SystemExit(main())
