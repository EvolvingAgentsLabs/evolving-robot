"""Dream engine: the robot rewrites one of its own skills from mission failures.

Flow (per the plan):
  1. Read the latest mission trace (missed patient anomalies, missed checkpoints,
     inefficient steps).
  2. Ask Gemma to rewrite the target ``SKILL.md`` body to fix them.
  3. Gate the rewrite with skill-map (``gate_skill``). If it fails, feed the exact
     skill-map error back to Gemma and retry. If it still fails, revert and discard.

The gate is what makes self-editing safe: a rewrite that hallucinates a broken
``@cross-reference`` or a name collision never reaches disk permanently. Whether a *valid*
rewrite actually improves the score is decided later by agentvcs (Phase 5): keep if the next
mission scores better, roll back if worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from robot_brain.gemma import GemmaBrain
from robot_brain.skill_gate import GateResult, gate_skill
from robot_brain.skills import Skill, get_skill, load_skills, write_skill_body
from odyssey_ext.gemma_rest import strip_thinking

_SKILL_BLOCK = re.compile(r"---\s*SKILL\s*---\s*\n(.*?)\n---\s*END SKILL\s*---", re.DOTALL | re.IGNORECASE)

_SYSTEM = (
    "You improve a single skill of a night-shift ward robot based on a mission trace. "
    "Rewrite the skill body to fix the failures you see in the trace (missed patient "
    "anomalies, missed checkpoints, wasted steps, poor aiming). Keep it concise and "
    "actionable for a 2D robot whose primitives are move_forward(cm), rotate_left(deg), "
    "rotate_right(deg), observe(), speak(text), report_status(target, status), stop(). "
    "A person's status reads 'unknown' until the robot is within ~0.8 m of them. "
    "IMPORTANT: keep the existing @cross-references to sibling skills and DO NOT invent new "
    "@references (only these siblings exist: {siblings}). Output ONLY the new body inside one "
    "fenced block:\n--- SKILL ---\n<new markdown body>\n--- END SKILL ---"
)


@dataclass
class DreamOutcome:
    status: str  # kept | discarded | no_trace | no_skill
    skill: str
    reason: str
    attempts: int = 0
    gate: Optional[GateResult] = None


class DreamEngine:
    def __init__(self, brain: Optional[GemmaBrain], skills_dir: Path | str, traces_dir: Path | str):
        self.brain = brain
        self.skills_dir = Path(skills_dir)
        self.traces_dir = Path(traces_dir)

    def latest_trace(self) -> Optional[str]:
        traces = sorted(self.traces_dir.glob("*.md")) if self.traces_dir.exists() else []
        return traces[-1].read_text() if traces else None

    def propose_rewrite(self, skill: Skill, trace_text: str, siblings: list[str], feedback: str = "") -> str:
        """One Gemma call -> the new skill body (empty string if unparseable)."""
        if self.brain is None:
            return ""
        system = _SYSTEM.format(siblings=", ".join(f"@{s}" for s in siblings) or "none")
        user = f"# Current skill: {skill.name}\n{skill.body}\n\n# Latest trace\n{trace_text}"
        if feedback:
            user += f"\n\n# Your previous attempt was rejected\n{feedback}\nFix it and re-output the block."
        text = strip_thinking(self.brain.generate([{"role": "system", "content": system}, {"role": "user", "content": user}]))
        m = _SKILL_BLOCK.search(text)
        return m.group(1).strip() if m else ""

    def apply_gated(self, skill: Skill, candidate_fn: Callable[[str], str], max_retries: int = 2) -> DreamOutcome:
        """Write candidate -> gate -> retry with feedback -> revert on final failure."""
        original = skill.path.read_text()
        feedback = ""
        gate: Optional[GateResult] = None
        for attempt in range(1, max_retries + 2):
            candidate = candidate_fn(feedback)
            if not candidate:
                feedback = (
                    "No fenced block found. Output exactly one "
                    "--- SKILL --- ... --- END SKILL --- block."
                )
                continue
            write_skill_body(skill.path, candidate)
            gate = gate_skill(self.skills_dir, node_path=skill.rel_path)
            if gate.ok:
                return DreamOutcome("kept", skill.name, "rewrite passed the skill-map gate", attempt, gate)
            feedback = gate.feedback()
        skill.path.write_text(original)  # revert
        return DreamOutcome("discarded", skill.name, feedback or "no valid rewrite", max_retries + 1, gate)

    def dream(self, target_skill: str = "patient-check", max_retries: int = 2) -> DreamOutcome:
        trace = self.latest_trace()
        if not trace:
            return DreamOutcome("no_trace", target_skill, f"no trace in {self.traces_dir}")
        skills = load_skills(self.skills_dir)
        skill = get_skill(skills, target_skill)
        if skill is None:
            return DreamOutcome("no_skill", target_skill, "target skill not found")
        siblings = [s.name for s in skills if s.name != target_skill]
        return self.apply_gated(
            skill, lambda fb: self.propose_rewrite(skill, trace, siblings, fb), max_retries
        )
