#!/usr/bin/env python3
"""Phase 6: the full loop, live and end to end.

Starts the sim, then runs one real evolution step:
  mission (odyssey + sim2d) -> success_rate
  -> commit baseline (agentvcs captures the odyssey trace via the new provider)
  -> dream (Gemma rewrites a skill, gated by skill-map)
  -> commit evolved -> re-run mission -> keep or rollback(reason)

With a Gemma key the pilot + dream are real; without one, the scripted pilot scores the
missions and the dream step reports "no brain" (the agentvcs + odyssey wiring still runs).

Usage:  python scripts/evolve_live.py [--keep]
  --keep leaves the evolved skills + .agentvcs in place; default restores them afterward.
"""

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_brain.dream import DreamEngine  # noqa: E402
from robot_brain.evolve import EvolutionController  # noqa: E402
from robot_brain.gemma import GemmaError, make_brain  # noqa: E402
from scripts.run_mission import rounds_score  # noqa: E402

SKILLS = ROOT / "robot_brain" / "skills"
TRACES = ROOT / "traces"
TARGET_SKILL = SKILLS / ".claude" / "skills" / "patient-check" / "SKILL.md"


def _wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> int:
    keep = "--keep" in sys.argv
    skill_backup = TARGET_SKILL.read_text()
    agentvcs_existed = (SKILLS / ".agentvcs").exists()

    sim = subprocess.Popen(
        [sys.executable, "-m", "sim2d.server"], cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_port("127.0.0.1", 9091):
            print("sim did not come up on :9091"); return 1
        print("sim2d up on :9091 (viewer at http://localhost:9092)\n")

        ctrl = EvolutionController(SKILLS)
        try:
            brain = make_brain()
            print(f"brain: {brain.model}")
        except GemmaError:
            brain = None
            print("brain: none (scripted pilot scores; dream step is a no-op)")
        dream = DreamEngine(brain, SKILLS, TRACES)

        result = ctrl.evolve_step(
            dream_fn=lambda: dream.dream("patient-check"),
            score_fn=lambda: rounds_score(),
            target_skill="patient-check",
        )

        print("\n--- evolution step ---")
        print(f"evolved={result.evolved} kept={result.kept} rolled_back={result.rolled_back}")
        print(f"baseline_score={result.baseline_score}  new_score={result.new_score}")
        print(f"reason: {result.reason}")

        print("\n--- agentvcs log (with odyssey trace captured) ---")
        for oid, c in ctrl.history()[:5]:
            trace = "trace" if c.get("trace") else "no-trace"
            print(f"  {oid[:12]} [{c['state']:>12}] [{trace}] {c['message']}")
        if ctrl.rollbacks():
            print("\n--- rollback ledger ---")
            for r in ctrl.rollbacks():
                print(f"  {r['from'][:8]} -> {r['to'][:8]}: {r['reason']}")
        return 0
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sim.kill()
        if not keep:
            TARGET_SKILL.write_text(skill_backup)
            if not agentvcs_existed:
                shutil.rmtree(SKILLS / ".agentvcs", ignore_errors=True)
                (SKILLS / "agent.json").unlink(missing_ok=True)  # created by Repository.init
                (SKILLS / "AGENTS.md").unlink(missing_ok=True)   # created by Repository.init
            print("\n(restored authored skills; pass --keep to persist the evolution)")


if __name__ == "__main__":
    raise SystemExit(main())
