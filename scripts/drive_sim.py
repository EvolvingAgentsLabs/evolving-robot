#!/usr/bin/env python3
"""Phase 1 acceptance: drive the sim2d robot on a scripted 4-point patrol.

This is a *scripted* pilot (pure geometry) to exercise the simulator + WebSocket
transport + live viewer. The Gemma-driven pilot arrives in Phase 2.

Run the server first:   python -m sim2d.server
Open the viewer:        http://localhost:9092
Then:                   python scripts/drive_sim.py
"""

import asyncio
import json
import math
import os

import websockets

WS_URL = os.environ.get("SIM_WS", "ws://localhost:9091")

# The four patrol checkpoints (the door landmarks).
CHECKPOINTS = ["server_room", "emergency_exit", "supply_closet", "main_entrance"]


async def call(ws, cmd: dict) -> dict:
    """Send a command and return the first non-broadcast reply."""
    await ws.send(json.dumps(cmd))
    while True:
        msg = json.loads(await ws.recv())
        if "reply" in msg:
            return msg
        # else: a broadcast state event meant for viewers; ignore here.


async def patrol() -> None:
    async with websockets.connect(WS_URL) as ws:
        # First frame is the arena snapshot (landmarks + start pose).
        arena = json.loads(await ws.recv())
        landmarks = {lm["id"]: lm for lm in arena.get("landmarks", [])}

        await call(ws, {"cmd": "run_started", "task": "scripted 4-point patrol"})

        for cp_id in CHECKPOINTS:
            target = landmarks[cp_id]
            pos = (await call(ws, {"cmd": "get_position"}))["position"]

            # aim
            dx, dy = target["x"] - pos["x"], target["y"] - pos["y"]
            desired = math.degrees(math.atan2(dy, dx)) % 360
            turn = ((desired - pos["heading"] + 540) % 360) - 180  # -180..180
            if turn >= 0:
                await call(ws, {"cmd": "rotate_left", "degrees": round(abs(turn), 1)})
            else:
                await call(ws, {"cmd": "rotate_right", "degrees": round(abs(turn), 1)})

            # advance to ~0.4 m short of the checkpoint
            dist_m = max(0.0, math.hypot(dx, dy) - 0.4)
            await call(ws, {"cmd": "move_forward", "distance_cm": round(dist_m * 100, 1)})

            # inspect
            obs = await call(ws, {"cmd": "observe"})
            seen = [f"{l['label']}@{l['distance_m']}m" for l in obs["nearby_landmarks"]]
            print(f"[{target['label']:<16}] observe -> {', '.join(seen) or 'nothing in range'}")
            await call(ws, {"cmd": "speak", "text": f"Checkpoint {target['label']} clear."})
            await asyncio.sleep(0.6)  # let the viewer animate

        await call(ws, {"cmd": "run_complete", "outcome": "success", "turns": len(CHECKPOINTS)})
        print("\nPhase 1 OK: patrol complete.")


if __name__ == "__main__":
    try:
        asyncio.run(patrol())
    except (ConnectionRefusedError, OSError):
        print("Could not connect. Start the server first:  python -m sim2d.server")
