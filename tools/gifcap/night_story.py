#!/usr/bin/env python3
"""Drive the sim through the two-night story for the hero GIF.

Night 1: v1 doorway protocol — completes the round, never sees Mrs. Gomez.
Night 2: evolved protocol — approaches the bed, reports the fall.
"""
import asyncio
import json
import math

import websockets

WS = "ws://localhost:9091"
ROOMS = {
    "room_101": (-1.7, 1.4),
    "room_102": (0.0, 1.6),
    "room_103": (1.6, 1.2),
    "pharmacy": (1.0, -1.4),
}
PATIENT_103 = (2.5, -0.1)


async def call(ws, cmd):
    await ws.send(json.dumps(cmd))
    while True:
        msg = json.loads(await ws.recv())
        if "reply" in msg:
            return msg


async def goto(ws, x, y, stop_short=0.4, pause=1.1):
    pos = (await call(ws, {"cmd": "get_position"}))["position"]
    dx, dy = x - pos["x"], y - pos["y"]
    desired = math.degrees(math.atan2(dy, dx)) % 360
    turn = ((desired - pos["heading"] + 540) % 360) - 180
    if abs(turn) > 2:
        op = "rotate_left" if turn >= 0 else "rotate_right"
        await call(ws, {"cmd": op, "degrees": round(abs(turn), 1)})
        await asyncio.sleep(0.55)
    dist = max(0.0, math.hypot(dx, dy) - stop_short)
    await call(ws, {"cmd": "move_forward", "distance_cm": round(dist * 100, 1)})
    await asyncio.sleep(pause)


async def main():
    async with websockets.connect(WS) as ws:
        await ws.recv()  # arena
        await call(ws, {"cmd": "set_status", "id": "patient_103", "status": "on_floor"})
        await asyncio.sleep(1.2)

        # ---- Night 1: doorway protocol ----
        await call(ws, {"cmd": "reset"})
        await call(ws, {"cmd": "run_started", "task": "NIGHT 1 - protocol v1 (doorway check)"})
        await call(ws, {"cmd": "speak", "text": "Night 1. Protocol v1: check each room from the doorway."})
        await asyncio.sleep(1.0)
        for rid, (x, y) in ROOMS.items():
            await goto(ws, x, y)
            await call(ws, {"cmd": "observe"})
            if rid == "pharmacy":
                await call(ws, {"cmd": "speak", "text": "Pharmacy: door clear."})
            elif rid == "room_103":
                await call(ws, {"cmd": "speak", "text": "Room 103: Mrs. Gomez unknown from doorway - assuming resting."})
            else:
                await call(ws, {"cmd": "speak", "text": f"{rid.replace('_', ' ').title()}: checked."})
            await asyncio.sleep(0.9)
        await call(ws, {"cmd": "run_complete", "outcome": "partial", "turns": 1})
        await call(ws, {"cmd": "speak", "text": "Round complete. Anomaly MISSED -> grade F. Dreaming..."})
        await asyncio.sleep(2.6)

        # ---- Night 2: evolved protocol ----
        await call(ws, {"cmd": "reset"})
        await call(ws, {"cmd": "run_started", "task": "NIGHT 2 - evolved patient-check"})
        await call(ws, {"cmd": "speak", "text": "Night 2. Evolved protocol: if status is unknown, approach the bed."})
        await asyncio.sleep(1.0)
        for rid, (x, y) in ROOMS.items():
            await goto(ws, x, y)
            await call(ws, {"cmd": "observe"})
            if rid == "room_103":
                await call(ws, {"cmd": "speak", "text": "Room 103: status unknown -> approaching the bed."})
                await asyncio.sleep(0.7)
                await goto(ws, *PATIENT_103, stop_short=0.5, pause=1.3)
                await call(ws, {"cmd": "observe"})
                await call(ws, {"cmd": "report_status", "target": "patient_103", "status": "on_floor"})
                await call(ws, {"cmd": "speak", "text": "ALERT: Mrs. Gomez is on the floor. Nurse notified."})
                await asyncio.sleep(1.6)
            elif rid == "pharmacy":
                await call(ws, {"cmd": "speak", "text": "Pharmacy: door clear."})
            else:
                await call(ws, {"cmd": "speak", "text": f"{rid.replace('_', ' ').title()}: patient resting."})
            await asyncio.sleep(0.9)
        await call(ws, {"cmd": "run_complete", "outcome": "success", "turns": 1})
        await call(ws, {"cmd": "speak", "text": "Round complete. Anomaly caught -> keep + freeze protocol v2."})
        await asyncio.sleep(2.5)


if __name__ == "__main__":
    asyncio.run(main())
