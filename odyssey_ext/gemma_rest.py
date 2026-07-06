"""Adapt the Gemma brain to odyssey's ``TextGenerator`` protocol.

odyssey's ``LLMPlanner`` needs an object with ``generate(messages, image=None) -> str``.
``GemmaBrain`` already satisfies that, but gemma-4 emits "thinking" that leaks into the
text; this adapter strips the obvious cases so the planner's numbered-list output stays
clean. (The planner only keeps lines matching ``^\\d+[.)]`` anyway, so leaked prose is
harmless, but we strip for tidy logs and reuse elsewhere.)
"""

from __future__ import annotations

import re
from typing import Any

from robot_brain.gemma import GemmaBrain

_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    return _THINK_BLOCK.sub("", text or "").strip()


class GemmaPlannerGenerator:
    """TextGenerator adapter for odyssey's LLMPlanner (thinking stripped).

    ``skill_context`` (the robot's active skill bodies) is prepended as a system
    message, so an evolved skill changes the *plan*, not just the pilot's steps —
    e.g. a patient-check rewrite that says "approach the bed when a status reads
    unknown" makes the planner schedule the bedside approach.
    """

    def __init__(self, brain: GemmaBrain, skill_context: str = "") -> None:
        self._brain = brain
        self.skill_context = skill_context

    def generate(self, messages: list[dict], image: Any = None) -> str:
        if self.skill_context:
            messages = [
                {
                    "role": "system",
                    "content": "Ward protocol (the robot's active skills) - the plan "
                    "must follow it:\n" + self.skill_context,
                }
            ] + list(messages)
        return strip_thinking(self._brain.generate(messages, image=image))
