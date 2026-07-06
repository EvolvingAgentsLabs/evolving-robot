"""Sim2DRunner — an odyssey evaluation Runner that drives the 2D sim over WebSocket.

Modeled on odyssey's ``RobosuiteRunner``: it composes a planner (Gemma SPECIALIST) and a
pilot (``Pilot2D``) via odyssey's ``PlannedEvalRuntime``, runs episodes, and returns the
standard ``build_eval_summary`` dict (``success_rate`` / ``performance_score`` / ``passed``).
Instead of a Robosuite env it talks to ``sim2d/server.py`` over the same WebSocket protocol
``scripts/drive_sim.py`` uses, so the browser viewer animates the eval live.

Scoring is clinical: an episode succeeds only if the robot reaches every checkpoint
(the ward rooms) AND correctly reports every ground-truth anomaly (a patient
``on_floor`` / ``calling`` / ``unresponsive``, injected via the sim's ``set_status``).
Partial credit lands in ``performance_score`` (mean episode return), so "completed the
round but missed the fallen patient" scores 0.8, not 1.0 — the honest number the
evolution loop keeps or rolls back on.

Registers for ``(EVALUATION, "custom")`` so a mission task with
``evaluation_type: custom`` selects it automatically (a specific (kind, type) beats the
``cpu_mock`` wildcard). No changes to odyssey's source are required.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

from odyssey.runners.agents.planned import PhaseConfig, PhaseStrategy, PlannedEvalRuntime
from odyssey.runners.agents.planner import LLMPlanner
from odyssey.runners.base import Runner, TaskContext
from odyssey.runners.evals._common import build_eval_summary
from odyssey.spec.tasks import TaskKind

from robot_brain.gemma import GemmaError, make_brain
from robot_brain.pilot import ANOMALY_STATUSES, Pilot2D
from robot_brain.skills import load_skills
from odyssey_ext.gemma_rest import GemmaPlannerGenerator

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", _PROJECT_ROOT / "robot_brain" / "skills"))
_TRACES_DIR = Path(os.environ.get("TRACES_DIR", _PROJECT_ROOT / "traces"))


def _fmt_args(action: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in action.items() if k != "cmd")


class Sim2DRunner(Runner):
    @property
    def name(self) -> str:
        return "sim2d"

    @property
    def supported_kinds(self) -> set[TaskKind]:
        return {TaskKind.EVALUATION}

    @property
    def supported_types(self) -> set[str]:
        return {"custom"}

    async def run(self, context: TaskContext) -> dict[str, Any]:
        spec = context.task.spec
        cfg = dict(getattr(spec, "config", {}) or {})
        ws_url = cfg.get("ws_url") or os.environ.get("SIM_WS", "ws://localhost:9091")
        num_episodes = int(getattr(spec, "num_episodes", 1) or 1)
        max_steps = int(cfg.get("max_steps", 30))
        arrive_radius = float(cfg.get("arrive_radius_m", 0.6))
        objective = context.mission.spec.objective.strip()
        benchmark = getattr(spec, "benchmark_name", None) or "night-rounds"
        target_skill = cfg.get("skill", "patient-check")

        # Brain: planner + pilot both Gemma when a key is present; otherwise a
        # deterministic scripted pilot with no planner (single phase). Either way
        # the runner scores by whether the robot reaches every checkpoint.
        try:
            brain = make_brain()
        except GemmaError:
            brain = None
        pilot_mode = cfg.get("pilot", "gemma" if brain else "scripted")

        await context.emit_progress(
            "model_loading",
            step="brain",
            step_label=f"pilot={pilot_mode} planner={'gemma' if brain else 'none'}",
        )

        async with websockets.connect(ws_url) as ws:
            arena = json.loads(await ws.recv())  # first frame = arena snapshot
            checkpoints = list(cfg.get("checkpoints") or []) or [
                l["id"] for l in arena.get("landmarks", []) if l["type"] == "door"
            ]
            # Ground truth for scoring: persons whose injected status is an anomaly.
            anomalies = {
                l["id"]: l["status"]
                for l in arena.get("landmarks", [])
                if l["type"] == "person" and l.get("status") in ANOMALY_STATUSES
            }

            # Inject every skill body into the pilot (the ward protocol is the three
            # skills together), so evolving any of them actually changes behavior.
            skills = load_skills(_SKILLS_DIR)
            skill_body = "\n\n".join(f"## Skill: {s.name}\n{s.body}" for s in skills)

            planner = (
                LLMPlanner(GemmaPlannerGenerator(brain, skill_context=skill_body))
                if brain
                else None
            )
            pilot = Pilot2D(
                mode=pilot_mode,
                brain=brain,
                checkpoints=checkpoints,
                arrive_radius_m=arrive_radius,
                skill_context=skill_body,
            )
            runtime = PlannedEvalRuntime(
                pilot,
                planner,
                phase_config=PhaseConfig(strategy=PhaseStrategy.FIXED_STEPS, steps_per_phase=4),
                fallback_instruction=objective,
            )
            self._pilot = pilot  # for per-episode reset

            await self._cmd(ws, {"cmd": "run_started", "task": benchmark})

            successes = 0
            episode_returns: list[float] = []
            episodes: list[dict] = []
            for ep in range(1, num_episodes + 1):
                if context.cancelled():
                    break
                try:
                    visited, reports, steps = await self._run_episode(
                        ws, runtime, objective, checkpoints, max_steps, arrive_radius, anomalies
                    )
                except GemmaError as err:
                    # The planner call died (free-tier throttling / read timeout).
                    # Degrade to planner-less mode: the objective is the instruction.
                    print(f"  (planner failed, degrading to planner-less episode: {str(err)[:80]})")
                    runtime = PlannedEvalRuntime(
                        pilot,
                        None,
                        phase_config=PhaseConfig(
                            strategy=PhaseStrategy.FIXED_STEPS, steps_per_phase=4
                        ),
                        fallback_instruction=objective,
                    )
                    visited, reports, steps = await self._run_episode(
                        ws, runtime, objective, checkpoints, max_steps, arrive_radius, anomalies
                    )
                reached = len(visited)
                # A report is correct when both the person and the observed status match.
                correct = {t for t, s in reports if anomalies.get(t) == s}
                missed_anoms = [t for t in anomalies if t not in correct]
                total = (len(checkpoints) + len(anomalies)) or 1
                episode_returns.append((reached + len(correct)) / total)
                if reached == len(checkpoints) and not missed_anoms:
                    successes += 1
                episodes.append(
                    {
                        "episode": ep,
                        "reached": sorted(visited),
                        "missed": [c for c in checkpoints if c not in visited],
                        "anomalies": dict(anomalies),
                        "reported": sorted(correct),
                        "missed_anomalies": missed_anoms,
                        "false_reports": sorted(
                            {t for t, s in reports if anomalies.get(t) != s}
                        ),
                        "steps": steps,
                    }
                )
                await context.emit_progress(
                    "executing",
                    step="episode_complete",
                    step_index=ep,
                    step_total=num_episodes,
                    step_label=(
                        f"ep {ep}: {reached}/{len(checkpoints)} checkpoints, "
                        f"{len(correct)}/{len(anomalies)} anomalies reported"
                    ),
                )

            outcome = "success" if successes == num_episodes else "partial"
            await self._cmd(ws, {"cmd": "run_complete", "outcome": outcome, "turns": num_episodes})

        success_rate = successes / num_episodes if num_episodes else 0.0
        trace_path = self._write_trace(
            objective, benchmark, target_skill, pilot_mode, checkpoints, success_rate, episodes
        )

        return build_eval_summary(
            num_episodes=num_episodes,
            successes=successes,
            episode_returns=episode_returns,
            benchmark_name=benchmark,
            checkpoint_path=(brain.model if brain else "scripted-pilot"),
            metrics={
                "checkpoints": checkpoints,
                "anomalies": anomalies,
                "anomalies_reported": episodes[-1]["reported"] if episodes else [],
                "pilot": pilot_mode,
                "skill": target_skill,
                "trace": str(trace_path),
            },
        )

    # -- trace ---------------------------------------------------------------

    @staticmethod
    def _write_trace(objective, benchmark, skill, pilot_mode, checkpoints, success_rate, episodes) -> Path:
        _TRACES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        path = _TRACES_DIR / f"{ts}_{benchmark}.md"
        lines = [
            "---",
            f"timestamp: {datetime.now(timezone.utc).isoformat()}",
            f"benchmark: {benchmark}",
            f"skill: {skill}",
            f"pilot: {pilot_mode}",
            f"success_rate: {success_rate:.3f}",
            f"checkpoints: {checkpoints}",
            "---",
            f"\n# Night-rounds trace: {benchmark}\n",
            f"Objective: {objective}\n",
        ]
        for ep in episodes:
            miss = ", ".join(ep["missed"]) or "none"
            lines.append(
                f"## Episode {ep['episode']} - reached {len(ep['reached'])}/{len(checkpoints)} "
                f"(missed: {miss})"
            )
            anomalies = ep.get("anomalies") or {}
            if anomalies:
                reported = ", ".join(ep.get("reported") or []) or "none"
                lines.append(f"Anomalies present: {anomalies}. Correctly reported: {reported}.")
            for t in ep.get("missed_anomalies") or []:
                lines.append(
                    f"INCIDENT: {t} was '{anomalies.get(t)}' and was NEVER reported. "
                    f"Its status read 'unknown' from the route - a person's status is only "
                    f"visible within ~0.8 m, so the robot must approach the patient to check them."
                )
            for t in ep.get("false_reports") or []:
                lines.append(f"False report filed on {t} (did not match the ground truth).")
            for s in ep["steps"]:
                pos = s["position"]
                pos_str = (
                    f"({pos.get('x', 0):.2f}, {pos.get('y', 0):.2f}, {pos.get('heading', 0):.0f}deg)"
                    if pos
                    else "(?)"
                )
                lines.append(
                    f"- step {s['step']:>2} [{s['instruction'][:48]}] "
                    f"{s['action'].get('cmd')}({{{_fmt_args(s['action'])}}}) -> {pos_str}"
                )
            lines.append("")
        path.write_text("\n".join(lines))
        return path

    # -- episode -------------------------------------------------------------

    async def _run_episode(self, ws, runtime, objective, checkpoints, max_steps, radius, anomalies):
        await self._cmd(ws, {"cmd": "reset"})
        if hasattr(self, "_pilot"):
            self._pilot.reset()
        obs = await self._observe(ws)
        # begin_episode / get_action call the sync Gemma client; run them off the
        # event loop so websocket keepalive pings still get answered.
        await asyncio.to_thread(runtime.begin_episode, objective, None)

        visited: set[str] = set()
        reports: list[tuple[str, str]] = []  # (target, status) the robot filed
        steps: list[dict] = []
        grace = 6  # extra steps after the route is done, to finish a pending report
        self._mark(obs, checkpoints, radius, visited)
        for step in range(max_steps):
            if len(visited) == len(checkpoints):
                correct = {t for t, s in reports if anomalies.get(t) == s}
                if len(correct) == len(anomalies) or grace <= 0:
                    break
                grace -= 1
            instruction = getattr(runtime, "current_instruction", "") or objective
            action = await asyncio.to_thread(runtime.get_action, obs)  # -> Pilot2D.act(obs, instruction)
            action = dict(action)
            action["step"] = step
            await self._cmd(ws, action)
            if action.get("cmd") == "report_status":
                reports.append((action.get("target", ""), action.get("status", "")))
            obs = await self._observe(ws)
            self._mark(obs, checkpoints, radius, visited)
            steps.append(
                {
                    "step": step,
                    "instruction": instruction,
                    "action": {k: v for k, v in action.items() if k != "step"},
                    "position": obs.get("position", {}),
                    "reached": sorted(visited),
                }
            )
        return visited, reports, steps

    @staticmethod
    def _mark(obs: dict, checkpoints: list[str], radius: float, visited: set[str]) -> None:
        for lm in obs.get("nearby_landmarks", []):
            if lm["id"] in checkpoints and lm["distance_m"] <= radius:
                visited.add(lm["id"])

    # -- websocket -----------------------------------------------------------

    async def _observe(self, ws) -> dict:
        reply = await self._cmd(ws, {"cmd": "observe"})
        return {
            "position": reply.get("position", {}),
            "nearby_landmarks": reply.get("nearby_landmarks", []),
            "nearest_person": reply.get("nearest_person"),
        }

    @staticmethod
    async def _cmd(ws, cmd: dict) -> dict:
        """Send a command, skipping broadcast events until the matching reply."""
        await ws.send(json.dumps(cmd))
        while True:
            msg = json.loads(await ws.recv())
            if "reply" in msg:
                return msg
