#!/usr/bin/env python3
"""Phase 0 acceptance: confirm Gemma answers over REST (no GPU) and can call a tool.

Usage:
    # AI Studio (default): set GEMINI_API_KEY (+ optional GEMMA_MODEL)
    python scripts/smoke_gemma.py
    # OpenRouter fallback: set OPENROUTER_API_KEY, then
    ROBOT_PROVIDER=openrouter python scripts/smoke_gemma.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robot_brain.gemma import make_brain, GemmaError  # noqa: E402

MOVE_TOOL = {
    "type": "function",
    "function": {
        "name": "move_forward",
        "description": "Drive the robot forward by a distance in centimeters.",
        "parameters": {
            "type": "object",
            "properties": {
                "distance_cm": {"type": "number", "description": "distance in cm"}
            },
            "required": ["distance_cm"],
        },
    },
}


def main() -> int:
    try:
        brain = make_brain()
    except GemmaError as e:
        print(f"[skip] {e}")
        print("Set GEMINI_API_KEY (AI Studio) or OPENROUTER_API_KEY, then re-run.")
        return 2

    print(f"provider={os.environ.get('ROBOT_PROVIDER', 'aistudio')} model={brain.model}\n")

    # 1) plain text
    text = brain.generate(
        [
            {"role": "system", "content": "You are a terse night-shift ward robot. Answer in one short sentence."},
            {"role": "user", "content": "You are at the nurses' station. What is your first step of the night round?"},
        ]
    )
    print("TEXT:", text.strip(), "\n")

    # 2) function-calling (the pilot path)
    res = brain.generate_full(
        [
            {"role": "system", "content": "You control a robot. Use tools to act. Move 30 cm forward now."},
            {"role": "user", "content": "Advance toward the server room."},
        ],
        tools=[MOVE_TOOL],
        tool_choice="auto",
    )
    if res.tool_calls:
        for tc in res.tool_calls:
            print(f"TOOL_CALL: {tc.name}({tc.arguments})")
    else:
        print("TOOL_CALL: (none) ->", (res.message or "").strip())

    print(f"\nusage={res.usage}")
    print("\nPhase 0 OK" if (text or res.tool_calls) else "\nPhase 0 produced nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
