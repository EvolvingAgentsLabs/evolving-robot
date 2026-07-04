"""sim2d: a headless 2D robot simulator with a live browser viewer.

- ``SimulatorHAL``  : a Python port of skillos_x_robot's trig simulator (no physics engine).
- WebSocket (:9091): controllers send action commands and get replies; every state change
  is broadcast to all clients so the viewer animates. Event vocabulary mirrors
  skillos_x_robot's ``WsMessage`` (pose / move / rotate / observe / speak / arrived / ...).
- HTTP (:9092)     : serves ``viewer.html``.

Run:  python -m sim2d.server        (from the evolving-robot/ root, on python>=3.10)
Then: open http://localhost:9092
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
from dataclasses import dataclass, asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import websockets

HERE = Path(__file__).resolve().parent


# --- world ------------------------------------------------------------------


@dataclass
class Position:
    x: float  # meters
    y: float  # meters
    heading: float  # degrees, 0=east, 90=north


@dataclass
class Landmark:
    id: str
    label: str
    x: float
    y: float
    type: str  # door | person | obstacle | object


ARENA_LANDMARKS: list[Landmark] = [
    Landmark("server_room", "Server Room", 0.0, 1.6, "door"),
    Landmark("emergency_exit", "Emergency Exit", -1.5, 1.5, "door"),
    Landmark("supply_closet", "Supply Closet", 1.3, -0.5, "door"),
    Landmark("main_entrance", "Main Entrance", -1.0, -1.2, "door"),
    Landmark("guard_carlos", "Carlos", 0.5, 0.3, "person"),
    Landmark("fire_extinguisher", "Fire Extinguisher", -0.8, 0.5, "object"),
]

START = Position(-0.5, -1.0, 90.0)


def _norm_angle(deg: float) -> float:
    return ((deg % 360) + 360) % 360


def _signed_bearing(deg: float) -> float:
    b = ((deg % 360) + 360) % 360
    return b - 360 if b > 180 else b


def _r2(n: float) -> float:
    return round(n, 2)


class SimulatorHAL:
    """2D trig simulator. Faithful port of skillos_x_robot/src/hal.ts."""

    def __init__(
        self,
        landmarks: Optional[list[Landmark]] = None,
        start: Optional[Position] = None,
        observe_radius: float = 3.5,
    ):
        self.landmarks = landmarks if landmarks is not None else list(ARENA_LANDMARKS)
        self._start = start or START
        self.pos = Position(self._start.x, self._start.y, self._start.heading)
        self.observe_radius = observe_radius

    def reset(self, position: Optional[Position] = None) -> None:
        p = position or self._start
        self.pos = Position(p.x, p.y, p.heading)

    def move_forward(self, distance_cm: float) -> dict:
        d = distance_cm / 100.0  # cm -> m
        rad = math.radians(self.pos.heading)
        self.pos.x += d * math.cos(rad)
        self.pos.y += d * math.sin(rad)
        return {"ok": True, "new_position": asdict(self.pos)}

    def rotate_left(self, degrees: float) -> dict:
        self.pos.heading = _norm_angle(self.pos.heading + degrees)
        return {"ok": True, "new_position": asdict(self.pos)}

    def rotate_right(self, degrees: float) -> dict:
        self.pos.heading = _norm_angle(self.pos.heading - degrees)
        return {"ok": True, "new_position": asdict(self.pos)}

    def stop(self) -> dict:
        return {"ok": True}

    def get_position(self) -> dict:
        return asdict(self.pos)

    def observe(self) -> dict:
        nearby: list[dict] = []
        nearest_person: Optional[dict] = None
        nearest_person_dist = math.inf
        for lm in self.landmarks:
            dx = lm.x - self.pos.x
            dy = lm.y - self.pos.y
            dist = math.hypot(dx, dy)
            if dist > self.observe_radius:
                continue
            bearing = _signed_bearing(math.degrees(math.atan2(dy, dx)) - self.pos.heading)
            entry = {
                "id": lm.id,
                "label": lm.label,
                "distance_m": _r2(dist),
                "bearing_deg": _r2(bearing),
                "type": lm.type,
            }
            nearby.append(entry)
            if lm.type == "person" and dist < nearest_person_dist:
                nearest_person_dist = dist
                nearest_person = {k: entry[k] for k in ("id", "label", "distance_m", "bearing_deg")}
        return {
            "position": asdict(self.pos),
            "nearby_landmarks": nearby,
            "nearest_person": nearest_person,
        }


# --- server -----------------------------------------------------------------


class SimServer:
    """Applies controller commands to one HAL and broadcasts state events."""

    def __init__(self, hal: SimulatorHAL):
        self.hal = hal
        self.clients: set[Any] = set()

    def arena(self) -> dict:
        return {
            "type": "arena",
            "landmarks": [asdict(lm) for lm in self.hal.landmarks],
            "observe_radius": self.hal.observe_radius,
            "pose": self.hal.get_position(),
        }

    async def _broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg)
        await asyncio.gather(
            *(self._safe_send(c, data) for c in list(self.clients)),
            return_exceptions=True,
        )

    @staticmethod
    async def _safe_send(client, data) -> None:
        try:
            await client.send(data)
        except Exception:
            pass

    async def _pose_event(self) -> None:
        p = self.hal.pos
        await self._broadcast({"type": "pose", "x": p.x, "y": p.y, "heading": p.heading})

    async def handle(self, websocket) -> None:
        self.clients.add(websocket)
        try:
            await websocket.send(json.dumps(self.arena()))
            async for raw in websocket:
                try:
                    cmd = json.loads(raw)
                except (ValueError, TypeError):
                    await websocket.send(json.dumps({"reply": "error", "ok": False, "error": "bad json"}))
                    continue
                reply = await self._dispatch(cmd)
                await websocket.send(json.dumps(reply))
        finally:
            self.clients.discard(websocket)

    async def _dispatch(self, cmd: dict) -> dict:
        op = cmd.get("cmd") or cmd.get("op")
        step = cmd.get("step", 0)

        if op == "reset":
            pos = cmd.get("position")
            self.hal.reset(Position(**pos) if pos else None)
            await self._pose_event()
            return {"reply": "reset", "ok": True, "position": self.hal.get_position()}

        if op == "move_forward":
            d = float(cmd.get("distance_cm", 0))
            result = self.hal.move_forward(d)
            await self._broadcast({"type": "move", "distance_cm": d, "step": step})
            await self._pose_event()
            return {"reply": "move_forward", **result}

        if op in ("rotate_left", "rotate_right"):
            deg = float(cmd.get("degrees", 0))
            result = getattr(self.hal, op)(deg)
            direction = "left" if op == "rotate_left" else "right"
            await self._broadcast({"type": "rotate", "degrees": deg, "direction": direction, "step": step})
            await self._pose_event()
            return {"reply": op, **result}

        if op == "stop":
            await self._broadcast({"type": "halt", "status": "stopped"})
            return {"reply": "stop", **self.hal.stop()}

        if op == "observe":
            result = self.hal.observe()
            await self._broadcast(
                {"type": "observe", "step": step, "landmarks": len(result["nearby_landmarks"])}
            )
            return {"reply": "observe", "ok": True, **result}

        if op == "get_position":
            return {"reply": "get_position", "ok": True, "position": self.hal.get_position()}

        if op == "speak":
            text = cmd.get("text", "")
            await self._broadcast({"type": "speak", "text": text, "step": step})
            return {"reply": "speak", "ok": True}

        if op in ("run_started", "run_complete", "arrived"):
            await self._broadcast({"type": op, **{k: v for k, v in cmd.items() if k not in ("cmd", "op")}})
            return {"reply": op, "ok": True}

        return {"reply": "error", "ok": False, "error": f"unknown cmd: {op!r}"}


def _serve_http(port: int) -> ThreadingHTTPServer:
    handler = partial(_ViewerHandler, directory=str(HERE))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class _ViewerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib name
        if self.path in ("/", ""):
            self.path = "/viewer.html"
        return super().do_GET()

    def log_message(self, *args):  # silence per-request logging
        pass


async def main() -> None:
    ws_port = int(os.environ.get("SIM_WS_PORT", "9091"))
    http_port = int(os.environ.get("SIM_HTTP_PORT", "9092"))
    server = SimServer(SimulatorHAL())
    _serve_http(http_port)
    print(
        f"sim2d up\n"
        f"  viewer : http://localhost:{http_port}\n"
        f"  ws     : ws://localhost:{ws_port}\n"
        f"  arena  : {len(server.hal.landmarks)} landmarks, observe_radius={server.hal.observe_radius}m"
    )
    async with websockets.serve(server.handle, "127.0.0.1", ws_port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nsim2d stopped")
