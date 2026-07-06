#!/usr/bin/env python3
"""The Night Shift: Florence misses a fallen patient, dreams, and learns to find her.

The flagship demo. It plays the evolution loop as a story, over two "nights"
(episodes of the night-rounds mission) with a scenario the authored skills
cannot handle:

  Night 1  Mrs. Gomez (bed 103) is on the floor, away from the door. The v1
           patient-check protocol scans from the doorway, reads her status as
           'unknown', assumes she is resting, and completes the round. odyssey
           scores it honestly: route done, anomaly missed -> partial credit only.
  Dream    Gemma reads the incident trace and rewrites patient-check (approach
           the bed when a status reads 'unknown'). skill-map gates the rewrite;
           agentvcs commits it.
  Night 2  Same ward, same fall — but the evolved protocol approaches the bed,
           reads 'on_floor', and files report_status. If the rewrite had made
           things worse instead, the controller would rollback(reason=...) and
           the ledger would record why.
  Freeze   If night 2 is a full success, the verified skill set is frozen
           (crystallize): the ward's new night protocol, auditable in agentvcs.

With a Gemma key the pilot + dream are real. Without one, the scripted pilot runs
night 1 deterministically (and misses the fall for the same reason: it never
approaches the bed) but the dream step is a no-op — the wiring still verifies.

Usage:  python scripts/night_shift.py [--keep]
  --keep leaves the evolved skills + .agentvcs in place; default restores them.
"""

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot_brain.dream import DreamEngine  # noqa: E402
from robot_brain.evolve import EvolutionController  # noqa: E402
from robot_brain.gemma import GemmaError, make_brain  # noqa: E402
from scripts.run_mission import rounds_summary  # noqa: E402

SKILLS = ROOT / "robot_brain" / "skills"
TRACES = ROOT / "traces"
TARGET = "patient-check"
TARGET_SKILL = SKILLS / ".claude" / "skills" / TARGET / "SKILL.md"
WS_URL = "ws://localhost:9091"

# Tonight's incident: Mrs. Gomez (bed 103) has fallen, out of sight of the doorway.
SCENARIO = {"patient_103": "on_floor"}
KEEP_RATIO = 0.9


def _wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


async def _inject(statuses: dict) -> None:
    """Set ground-truth statuses on the running sim (scenario injection)."""
    async with websockets.connect(WS_URL) as ws:
        await ws.recv()  # first frame = arena snapshot
        for lm_id, status in statuses.items():
            await ws.send(json.dumps({"cmd": "set_status", "id": lm_id, "status": status}))
            while True:
                msg = json.loads(await ws.recv())
                if "reply" in msg:
                    break


def _run_night(n: int) -> dict:
    print(f"\n=== Night {n}: running the rounds ===")
    summary = rounds_summary()
    if not summary:
        raise RuntimeError(
            f"night {n}: the mission returned no eval summary (runner crashed?) - "
            "check the sim and the API, then re-run"
        )
    metrics = summary.get("metrics", {})
    anomalies = metrics.get("anomalies", {}) or {}
    reported = set(metrics.get("anomalies_reported", []) or [])
    print(
        f"  performance={summary.get('performance_score')}  "
        f"grade={summary.get('letter_grade')}  "
        f"anomalies reported {len(reported & set(anomalies))}/{len(anomalies)}"
    )
    for target, status in anomalies.items():
        verdict = "REPORTED" if target in reported else "** MISSED **"
        print(f"    - {target} ({status}): {verdict}")
    return summary


def _score(summary: dict) -> float:
    return float(summary.get("performance_score", summary.get("success_rate", 0.0)))


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
        print("sim2d up on :9091 (viewer at http://localhost:9092)")

        asyncio.run(_inject(SCENARIO))
        print(f"scenario injected: {SCENARIO}")

        ctrl = EvolutionController(SKILLS)
        try:
            brain = make_brain()
            print(f"brain: {brain.model}")
        except GemmaError:
            brain = None
            print("brain: none (scripted pilot; the dream step is a no-op)")
        dream = DreamEngine(brain, SKILLS, TRACES)

        # --- Night 1: the incident ---
        night1 = _run_night(1)
        score1 = _score(night1)
        if ctrl.head() is None:
            ctrl.commit(f"baseline: night protocol v1 at performance={score1:.2f}")

        # --- The dream: rewrite patient-check from the incident trace ---
        print("\n=== Dream: rewriting patient-check from the incident trace ===")
        outcome = dream.dream(TARGET)
        print(f"  status={outcome.status}  attempts={outcome.attempts}  reason: {outcome.reason.splitlines()[0]}")
        if outcome.status != "kept":
            if brain is None:
                print("\n(no Gemma key: night 1 + the wiring are verified; set "
                      "GEMINI_API_KEY to watch the full learn-and-recover arc)")
            return 0
        commit = ctrl.commit(f"evolve({TARGET}): {outcome.reason}")
        print(f"  committed {commit[:12]}")

        # --- Night 2: same ward, same fall, evolved protocol ---
        night2 = _run_night(2)
        score2 = _score(night2)

        # --- Keep or rollback, then freeze if verified ---
        print("\n=== Verdict ===")
        if score2 < score1 * KEEP_RATIO:
            reason = (
                f"performance {score2:.2f} < {score1:.2f} baseline "
                f"(keep_ratio {KEEP_RATIO}); revert {TARGET}"
            )
            ctrl.rollback(reason=reason)
            print(f"  ROLLBACK: {reason}")
        else:
            print(f"  KEEP: performance {score1:.2f} -> {score2:.2f}")
            if float(night2.get("success_rate", 0.0)) >= 1.0:
                frozen_oid, _ = ctrl.freeze(
                    message="freeze: night protocol v2 - anomaly caught, round intact"
                )
                print(f"  FROZEN: verified skill set crystallized at {str(frozen_oid)[:12]}")

        # --- Night 1 vs Night 2 ---
        print("\n=== Night 1 vs Night 2 ===")
        for label, s in (("night 1", night1), ("night 2", night2)):
            m = s.get("metrics", {})
            print(
                f"  {label}: performance={s.get('performance_score')} "
                f"grade={s.get('letter_grade')} passed={s.get('passed')} "
                f"reported={m.get('anomalies_reported', [])}"
            )

        print("\n--- agentvcs log (the protocol's genealogy) ---")
        for oid, c in ctrl.history()[:6]:
            trace = "trace" if c.get("trace") else "no-trace"
            print(f"  {oid[:12]} [{c['state']:>12}] [{trace}] {c['message']}")
        if ctrl.rollbacks():
            print("\n--- rollback ledger (why changes were rejected) ---")
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
