"""FastAPI 主入口：外部信号 → 本地闸门 → 浏览器 Web Bluetooth 中继页。

链路：
  房间 WS (rgate_client) ─┐
  浏览器桥接脚本 (/ws/bridge) ├─→ state 闸门/映射 ─→ /ws/device ─→ 中继页 Web Bluetooth ─→ 设备
  手动面板 (/api/control)  ─┘
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .state import state, STALE_SECONDS
from . import rgate_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("main")

def _resource_root() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent.parent

WEB_DIR = _resource_root() / "web"
VERSION = "1.0.0"
APP_NAME = "ycy-100Adults"


# ---------- rgate 事件 → 状态 ----------
async def on_rgate_event(event: dict):
    etype = event.get("type")
    if etype == "bpm":
        await state.apply_bpm(event.get("bpm"), source="rgate")
    elif etype == "speed":
        await state.apply_speed(event.get("speed"), source="rgate")
    elif etype == "grip":
        await state.apply_grip(event.get("grip"), source="rgate")
    elif etype == "level":
        await state.apply_level_btn(event.get("level"), source="rgate")
    elif etype == "cmd":
        await state.apply_cmd(event.get("cmd"), source="rgate")
    elif etype == "rgate_status":
        state.rgate_connected = bool(event.get("connected"))
        state.rgate_role = event.get("role", state.rgate_role)
        await state.push_status()
    elif etype == "state":
        raw = event.get("raw", {})
        state.phase = raw.get("phase", state.phase)
        state.paused = bool(raw.get("paused", state.paused))
        await state.push_status()


rgate_client.rgate.on_event = on_rgate_event


async def watchdog_loop():
    """无数据看门狗：超过阈值未收到有效指令且仍有输出，自动归零停止。"""
    while True:
        await asyncio.sleep(1.0)
        try:
            if state.is_stale():
                await state.zero("timeout")
                log.info("无数据超时（%ss），设备自动停止", STALE_SECONDS)
        except Exception:
            log.exception("watchdog 异常")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("%s 服务启动 v%s", APP_NAME, VERSION)
    rgate_client.rgate.start()
    watchdog = asyncio.create_task(watchdog_loop())
    yield
    watchdog.cancel()
    await rgate_client.rgate.stop()


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)

# 桥接脚本运行在外部页面，跨域连本地 WS/HTTP，放开本地来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 页面 ----------
_NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "relay.html", headers=_NO_CACHE)


@app.get("/relay.html")
async def relay_page():
    return FileResponse(WEB_DIR / "relay.html", headers=_NO_CACHE)


# ---------- 状态 ----------
@app.get("/api/status")
async def api_status():
    return {"ok": True, "version": VERSION, **state.status()}


# ---------- 安全控制 ----------
@app.post("/api/control/panic")
async def api_panic():
    await state.trigger_panic()
    return {"ok": True, "status": state.status()}


@app.post("/api/control/reset")
async def api_reset():
    state.reset_panic()
    return {"ok": True, "status": state.status()}


@app.post("/api/control/arm")
async def api_arm():
    ok = state.arm()
    return {"ok": ok, "status": state.status()}


@app.post("/api/control/disarm")
async def api_disarm():
    state.disarm()
    await state.zero("disarm")
    return {"ok": True, "status": state.status()}


class MotorsReq(BaseModel):
    a: int = 0
    b: int = 0
    c: int = 0


@app.post("/api/control/motors")
async def api_motors(req: MotorsReq):
    await state.set_motors(req.a, req.b, req.c, source="manual")
    return {"ok": True, "status": state.status()}


class BpmReq(BaseModel):
    bpm: int


@app.post("/api/control/bpm")
async def api_bpm(req: BpmReq):
    await state.apply_bpm(req.bpm, source="manual")
    return {"ok": True, "status": state.status()}


class LevelReq(BaseModel):
    level: int


@app.post("/api/control/level")
async def api_level(req: LevelReq):
    await state.apply_level_btn(req.level, source="manual")
    return {"ok": True, "status": state.status()}


class CmdReq(BaseModel):
    cmd: str


@app.post("/api/control/cmd")
async def api_cmd(req: CmdReq):
    await state.apply_cmd(req.cmd, source="manual")
    return {"ok": True, "status": state.status()}


class MaxReq(BaseModel):
    user_max: int


@app.post("/api/control/max")
async def api_max(req: MaxReq):
    state.user_max = max(1, min(20, req.user_max))
    return {"ok": True, "user_max": state.user_max}


class ChannelsReq(BaseModel):
    channels: dict


@app.post("/api/control/channels")
async def api_channels(req: ChannelsReq):
    ok = await state.apply_channels(req.channels)
    return {"ok": ok, "status": state.status()}


class OffsetsReq(BaseModel):
    offsets: dict


@app.post("/api/control/offsets")
async def api_offsets(req: OffsetsReq):
    ok = await state.apply_offsets(req.offsets)
    return {"ok": ok, "status": state.status()}


# ---------- rgate 房间连接 ----------
class RgateConnect(BaseModel):
    url: str = ""
    room: str = ""
    key: str = ""
    role: str = "player"


@app.post("/api/rgate/connect")
async def api_rgate_connect(req: RgateConnect):
    if req.url:
        if not rgate_client.rgate.connect_from_url(req.url, role=req.role or None):
            return {"ok": False, "detail": "链接 hash 格式应为 #<room>.<key> 或 #join.<room>.<player>（两段 32 位 hex）"}
    else:
        if not (req.room and req.key):
            return {"ok": False, "detail": "需提供 url 或 room+key"}
        rgate_client.rgate.configure(req.room, req.key, role=req.role)
    rgate_client.rgate.start()
    return {"ok": True, "status": state.status()}


@app.post("/api/rgate/disconnect")
async def api_rgate_disconnect():
    await rgate_client.rgate.stop()
    state.rgate_connected = False
    return {"ok": True, "status": state.status()}


@app.get("/api/rgate/status")
async def api_rgate_status():
    return {"ok": True, "connected": state.rgate_connected, "role": state.rgate_role,
            "room": rgate_client.rgate.room}


# ---------- 设备浏览器 WS（Web Bluetooth 写入设备） ----------
@app.websocket("/ws/device")
async def ws_device(ws: WebSocket):
    await state.connect_device(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = __import__("json").loads(raw)
            except Exception:
                continue
            if data.get("type") == "device_status":
                if "battery" in data:
                    state.battery = data["battery"]
                if data.get("device_info"):
                    state.device_info = data["device_info"]
                await state.push_status()
    except WebSocketDisconnect:
        await state.disconnect_device(ws)


# ---------- 浏览器桥接脚本 WS（读页面 BPM/档位后转发到本地） ----------
@app.websocket("/ws/bridge")
async def ws_bridge(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = __import__("json").loads(raw)
            except Exception:
                continue
            await handle_bridge_message(data)
    except WebSocketDisconnect:
        pass


async def handle_bridge_message(data: dict):
    """桥接脚本上报的事件，结构与 rgate 事件对齐，source=bridge。"""
    etype = data.get("type")
    if etype == "bpm":
        await state.apply_bpm(data.get("bpm"), source="bridge")
    elif etype == "speed":
        await state.apply_speed(data.get("speed"), source="bridge")
    elif etype == "grip":
        await state.apply_grip(data.get("grip"), source="bridge")
    elif etype == "level":
        await state.apply_level_btn(data.get("level"), source="bridge")
    elif etype == "cmd":
        await state.apply_cmd(data.get("cmd"), source="bridge")
    elif etype == "motors":
        m = data.get("motors", {})
        await state.set_motors(m.get("A", 0), m.get("B", 0), m.get("C", 0), source="bridge")


class BridgeBpm(BaseModel):
    bpm: int


@app.post("/api/bridge/bpm")
async def api_bridge_bpm(req: BridgeBpm):
    """HTTP 备选入口：桥接脚本无法用 WS 时走 POST。"""
    await state.apply_bpm(req.bpm, source="bridge")
    return {"ok": True, "status": state.status()}
