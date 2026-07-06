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
    status: str = ""  # persons only: resting | on_floor | calling | unresponsive | on_duty


# The night ward: rooms (type=door) are the round's checkpoints; each room has a
# patient nearby. Patient status is only visible within ``status_radius`` (the
# robot's "lamp"), so checking a patient means actually approaching the bed.
WARD_LANDMARKS: list[Landmark] = [
    Landmark("room_101", "Room 101", -1.7, 1.4, "door"),
    Landmark("room_102", "Room 102", 0.0, 1.6, "door"),
    Landmark("room_103", "Room 103", 1.6, 1.2, "door"),
    Landmark("pharmacy", "Pharmacy", 1.0, -1.4, "door"),
    Landmark("patient_101", "Mr. Alvarez (bed 101)", -2.2, 1.8, "person", "resting"),
    Landmark("patient_102", "Mrs. Chen (bed 102)", 0.4, 2.1, "person", "resting"),
    Landmark("patient_103", "Mrs. Gomez (bed 103)", 2.5, -0.1, "person", "resting"),
    Landmark("nurse_carlos", "Nurse Carlos", -0.2, -0.3, "person", "on_duty"),
    Landmark("med_cart", "Medication Cart", -0.9, 0.6, "object"),
    Landmark("defibrillator", "Defibrillator", 0.7, 0.1, "object"),
]

# Statuses that count as a clinical anomaly the robot must report.
ANOMALY_STATUSES = {"on_floor", "calling", "unresponsive"}

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
        status_radius: float = 0.8,
    ):
        self.landmarks = landmarks if landmarks is not None else list(WARD_LANDMARKS)
        self._start = start or START
        self.pos = Position(self._start.x, self._start.y, self._start.heading)
        self.observe_radius = observe_radius
        self.status_radius = status_radius  # how close the robot must be to read a person's status
        self.reports: list[dict] = []  # report_status() calls this episode

    def reset(self, position: Optional[Position] = None) -> None:
        p = position or self._start
        self.pos = Position(p.x, p.y, p.heading)
        self.reports = []

    def set_status(self, landmark_id: str, status: str) -> dict:
        """Scenario injection: change a landmark's ground-truth status (e.g. a fall)."""
        for lm in self.landmarks:
            if lm.id == landmark_id:
                lm.status = status
                return {"ok": True, "id": landmark_id, "status": status}
        return {"ok": False, "error": f"unknown landmark: {landmark_id!r}"}

    def report_status(self, target: str, status: str) -> dict:
        """The robot files a finding. Correctness is judged by the eval, not here."""
        self.reports.append({"target": target, "status": status})
        return {"ok": True, "target": target, "status": status}

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
            if lm.type == "person":
                # A person's status is only readable up close (the robot's lamp).
                entry["status"] = lm.status if dist <= self.status_radius else "unknown"
            nearby.append(entry)
            if lm.type == "person" and dist < nearest_person_dist:
                nearest_person_dist = dist
                nearest_person = {
                    k: entry[k] for k in ("id", "label", "distance_m", "bearing_deg", "status")
                }
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
            "status_radius": self.hal.status_radius,
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

        if op == "report_status":
            target = cmd.get("target", "")
            status = cmd.get("status", "")
            result = self.hal.report_status(target, status)
            await self._broadcast({"type": "report", "target": target, "status": status, "step": step})
            return {"reply": "report_status", **result}

        if op == "set_status":
            result = self.hal.set_status(cmd.get("id", ""), cmd.get("status", ""))
            if result.get("ok"):
                await self._broadcast({"type": "status", "id": result["id"], "status": result["status"]})
            return {"reply": "set_status", **result}

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
