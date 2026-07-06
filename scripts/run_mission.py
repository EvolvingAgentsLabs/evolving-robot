#!/usr/bin/env python3
"""Phase 2 acceptance: run the night-rounds mission through odyssey's engine.

Builds a MissionEngine with a RunnerRegistry of {CPUMockRunner, Sim2DRunner} and NO
ProviderRegistry (so odyssey never resolves the 2D embodiment / dummy dataset). The
warm-up training task routes to cpu_mock; the `evaluation_type: custom` task selects
Sim2DRunner, which drives the live sim2d over WebSocket.

Start the sim first:  python -m sim2d.server   (open http://localhost:9092 to watch)
Then:                 python scripts/run_mission.py [missions/night_rounds.mission.yaml]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from odyssey.engine import MissionEngine, MissionStatus  # noqa: E402
from odyssey.persistence import SqlitePersistence  # noqa: E402
from odyssey.runners import CPUMockRunner, RunnerRegistry  # noqa: E402
from odyssey.spec.loader import load_mission  # noqa: E402
from odyssey.telemetry import StdoutEventPublisher  # noqa: E402
from odyssey.telemetry.publishers.base import EventPublisher  # noqa: E402

from odyssey_ext.sim2d_runner import Sim2DRunner  # noqa: E402


async def run_rounds(mission_path: Path, quiet: bool = False):
    """Run the mission through odyssey's engine; return the final MissionRun."""
    spec = load_mission(mission_path)

    odyssey_dir = ROOT / ".odyssey"
    odyssey_dir.mkdir(exist_ok=True)

    registry = RunnerRegistry()
    registry.register(Sim2DRunner())   # (EVALUATION, "custom")
    registry.register(CPUMockRunner())  # wildcard fallback (training warm-up)

    publisher = _QuietPublisher() if quiet else StdoutEventPublisher()
    engine = MissionEngine(
        persistence=SqlitePersistence(str(odyssey_dir / "missions.db")),
        runners=registry,
        event_publisher=publisher,
        working_dir=odyssey_dir / "runs",
        providers=None,          # <- no robot/dataset resolution
        force_runner=None,
    )

    await engine.initialize()
    run = await engine.create_mission(spec)
    return await engine.start_mission(run.id)


def rounds_summary(mission_path: Path | None = None) -> dict:
    """Sync helper: run the mission and return the eval result_summary dict."""
    import asyncio as _asyncio

    mp = mission_path or (ROOT / "missions" / "night_rounds.mission.yaml")
    final = _asyncio.run(run_rounds(mp, quiet=True))
    for task in final.tasks:
        if task.spec.kind == "evaluation" and task.result_summary:
            return dict(task.result_summary)
    return {}


def rounds_score(mission_path: Path | None = None) -> float:
    """Sync helper: run the mission and return its eval score (0..1).

    Uses odyssey's ``performance_score`` (mean episode return), which grants partial
    credit — "finished the route but missed the fallen patient" scores 0.8, not 0 —
    the number the evolution loop keeps or rolls back on.
    """
    summary = rounds_summary(mission_path)
    return float(summary.get("performance_score", summary.get("success_rate", 0.0)))


class _QuietPublisher(EventPublisher):
    """An EventPublisher that swallows events (for programmatic scoring)."""

    async def publish(self, event_type: str, payload: dict) -> None:
        return None


async def _run(mission_path: Path) -> int:
    final = await run_rounds(mission_path)

    print("\n" + "=" * 60)
    print(f"mission {final.id}  status={final.status.value}")
    if final.overall_grade is not None:
        print(f"overall_grade = {final.overall_grade:.3f}")
    for task in final.tasks:
        summary = task.result_summary or {}
        if task.spec.kind == "evaluation":
            print(f"\neval task '{task.spec.name}':")
            print(json.dumps(summary, indent=2, default=str))
    print("=" * 60)

    return 0 if final.status == MissionStatus.COMPLETED else 1


def main() -> int:
    mission = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "missions" / "night_rounds.mission.yaml"
    if not mission.exists():
        print(f"mission not found: {mission}")
        return 2
    try:
        return asyncio.run(_run(mission))
    except (ConnectionRefusedError, OSError) as e:
        print(f"\nCould not reach the sim. Start it first:  python -m sim2d.server\n({e})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
