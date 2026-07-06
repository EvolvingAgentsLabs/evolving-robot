"""Pilot2D — the low-level policy that turns a sub-instruction + observation
into one discrete 2D primitive.

Two modes:

- ``gemma``    : Gemma picks a primitive via native function-calling (confirmed
                 working on AI Studio). Uses the current sub-instruction + the
                 structured observation.
- ``scripted`` : pure geometry, no API. Navigates toward the next unvisited
                 checkpoint using the bearing/distance in the observation. The
                 deterministic, no-key default so the odyssey integration runs
                 (and tests) without burning tokens.

``act(observation, instruction) -> dict`` returns a command dict the sim
understands, e.g. ``{"cmd": "move_forward", "distance_cm": 40}``. It is passed
through unchanged by odyssey's ``PlannedEvalRuntime.get_action`` (which type-hints
an ndarray but never inspects the value), so a 2D pilot composes with odyssey's
multi-agent orchestrator without pretending to emit robot-arm action vectors.
"""

from __future__ import annotations

from typing import Any, Optional

# Person statuses that count as a clinical anomaly (mirrors sim2d.server.ANOMALY_STATUSES).
ANOMALY_STATUSES = {"on_floor", "calling", "unresponsive"}

# The robot's primitives, as OpenAI-style tool definitions for the gemma pilot.
PRIMITIVES = [
    {
        "type": "function",
        "function": {
            "name": "move_forward",
            "description": "Drive forward along the current heading by a distance in cm.",
            "parameters": {
                "type": "object",
                "properties": {"distance_cm": {"type": "number"}},
                "required": ["distance_cm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rotate_left",
            "description": "Turn left (counter-clockwise) by degrees. Positive bearing_deg is to the left.",
            "parameters": {
                "type": "object",
                "properties": {"degrees": {"type": "number"}},
                "required": ["degrees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rotate_right",
            "description": "Turn right (clockwise) by degrees. Negative bearing_deg is to the right.",
            "parameters": {
                "type": "object",
                "properties": {"degrees": {"type": "number"}},
                "required": ["degrees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe",
            "description": "Scan for nearby landmarks (use when nothing useful is in view).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_status",
            "description": (
                "File a clinical finding about a person (by landmark id), e.g. "
                "report_status(target='patient_103', status='on_floor'). Use the status "
                "you actually observed; a person's status reads 'unknown' until you are "
                "close enough to see them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["target", "status"],
            },
        },
    },
]

_PILOT_SYSTEM = (
    "You pilot Florence, a 2D night-shift ward-assist robot. You are given your "
    "current sub-goal and a JSON observation of nearby landmarks, each with "
    "distance_m and bearing_deg (bearing is relative to your heading: positive = "
    "to your left, negative = to your right, 0 = straight ahead). Persons carry a "
    "status field; it reads 'unknown' until you are close enough to see them. "
    "Call exactly ONE tool to make progress toward the sub-goal's landmark: first "
    "rotate to face it (|bearing| small), then move_forward toward it (100 cm = 1 m; "
    "stop ~40 cm short). If you observe a person whose status is an anomaly "
    "(on_floor, calling, unresponsive), report it with report_status before moving "
    "on. If the target is not in view, observe. Call one tool, no prose."
)


class Pilot2D:
    def __init__(
        self,
        mode: str = "scripted",
        brain: Any = None,
        checkpoints: Optional[list[str]] = None,
        arrive_radius_m: float = 0.6,
        step_cap_cm: float = 60.0,
        skill_context: str = "",
    ):
        self.mode = mode if (mode != "gemma" or brain is not None) else "scripted"
        self.brain = brain
        self.checkpoints = checkpoints or []
        self.arrive_radius = arrive_radius_m
        self.step_cap = step_cap_cm
        # The body of the active skills (e.g. patient-check), injected into the gemma
        # pilot's prompt so that evolving the skill actually changes behavior.
        self.skill_context = skill_context
        self._reached: set[str] = set()  # scripted-pilot queue progress
        self._reported: set[str] = set()  # persons already reported this episode
        self.fallbacks = 0  # gemma steps degraded to geometry (transient API errors)

    def reset(self) -> None:
        self._reached.clear()
        self._reported.clear()

    def act(self, observation: dict, instruction: str) -> dict:
        if self.mode == "gemma":
            return self._act_gemma(observation, instruction)
        return self._act_scripted(observation, instruction)

    # -- gemma pilot ---------------------------------------------------------

    def _act_gemma(self, observation: dict, instruction: str) -> dict:
        import json

        from robot_brain.gemma import GemmaError

        system = _PILOT_SYSTEM
        if self.skill_context:
            system += "\n\nActive skill guidance:\n" + self.skill_context
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"Sub-goal: {instruction}\nObservation: {json.dumps(observation)}",
            },
        ]
        try:
            res = self.brain.generate_full(messages, tools=PRIMITIVES, tool_choice="auto")
        except GemmaError as err:
            # A transient API failure (free-tier throttling, read timeout) must not
            # kill the mission: degrade this one step to the geometric pilot.
            self.fallbacks += 1
            print(f"  (gemma step failed, geometric fallback #{self.fallbacks}: {str(err)[:80]})")
            return self._act_scripted(observation, instruction)
        if res.tool_calls:
            tc = res.tool_calls[0]
            return {"cmd": tc.name, **(tc.arguments or {})}
        return {"cmd": "observe"}

    # -- scripted pilot (geometry, no API) -----------------------------------

    def _act_scripted(self, observation: dict, instruction: str) -> dict:
        landmarks = {l["id"]: l for l in observation.get("nearby_landmarks", [])}

        # Duty of care first: report any visible anomalous person exactly once.
        # (Statuses beyond the lamp radius read "unknown" and are never reported.)
        for lm in landmarks.values():
            if (
                lm.get("type") == "person"
                and lm.get("status") in ANOMALY_STATUSES
                and lm["id"] not in self._reported
            ):
                self._reported.add(lm["id"])
                return {"cmd": "report_status", "target": lm["id"], "status": lm["status"]}

        # Advance the queue: any checkpoint currently within radius is reached.
        for cp in self.checkpoints:
            lm = landmarks.get(cp)
            if lm is not None and lm["distance_m"] <= self.arrive_radius:
                self._reached.add(cp)

        target = next((cp for cp in self.checkpoints if cp not in self._reached), None)
        if target is None:
            return {"cmd": "observe"}  # all checkpoints reached

        lm = landmarks.get(target)
        if lm is None:  # target out of view: sweep to find it
            return {"cmd": "rotate_left", "degrees": 45}

        bearing, dist_m = lm["bearing_deg"], lm["distance_m"]
        if abs(bearing) > 12:  # aim first
            side = "rotate_left" if bearing > 0 else "rotate_right"
            return {"cmd": side, "degrees": round(abs(bearing), 1)}
        if dist_m > self.arrive_radius:  # then advance
            step_m = min(dist_m - 0.4, self.step_cap / 100.0)
            return {"cmd": "move_forward", "distance_cm": round(max(step_m, 0.1) * 100, 1)}
        return {"cmd": "observe"}  # arrived
