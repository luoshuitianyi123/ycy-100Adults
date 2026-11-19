"""外部房间 WebSocket 客户端（通用骨架，host 可配置）。

协议：
  - 房间链接 hash：`#<room 32hex>.<key 32hex>`
  - WS：`wss://<host>/rws/<room>?role=<role>&key=<key>`
    role: player（被操控方）/ controller（控制方）/ watch
  - 心跳：每 20s 发 `{"t":"ping"}`
  - 下行报文（JSON）：
      {t:"state",  bpm, drive, mode, level, phase, paused, text, ...}
      {t:"drive",  k:"auto"|"stop"|<bpm 20-300>}
      {t:"bpm",    k:<bpm 20-300 或 0>}
      {t:"cmd",    k:"fast"|"slow"|"stop"|"go"}
      {t:"level",  k:1..9}
      {t: 其他类型}（本程序仅记录）

BPM 合法值：0=停止，20–300 整数；其余视为非法丢弃。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable, Optional

import websockets

log = logging.getLogger("rgate")

DEFAULT_HOST = "example.com"  # 按目标站点配置
# 控制方链接：#<room>.<controllerKey>
CTRL_RE = re.compile(r"^#?([0-9a-f]{32})\.([0-9a-f]{32})$")
# 被操控方（player）邀请链接：#join.<room>.<playerKey>
JOIN_RE = re.compile(r"^#?join\.([0-9a-f]{32})\.([0-9a-f]{32})$")
VALID_CMD = {"fast", "slow", "stop", "go"}


def parse_hash(fragment: str) -> Optional[dict]:
    """解析 hash，返回 {room, key, role}；识别 controller 与 player(join) 两种格式。"""
    if not fragment:
        return None
    f = fragment.strip()
    m = JOIN_RE.match(f)
    if m:
        return {"room": m.group(1), "key": m.group(2), "role": "player"}
    m = CTRL_RE.match(f)
    if m:
        return {"room": m.group(1), "key": m.group(2), "role": "controller"}
    return None


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _valid_bpm(v) -> Optional[int]:
    b = _as_int(v)
    if b is None:
        return None
    if b == 0:
        return 0
    if 20 <= b <= 300:
        return b
    return None


def _valid_level(v) -> Optional[int]:
    k = _as_int(v)
    if k is not None and 1 <= k <= 9:
        return k
    return None


class RgateClient:
    def __init__(self, on_event: Callable[[dict], "asyncio.Future | None"]):
        self.on_event = on_event
        self.room = ""
        self.key = ""
        self.role = "player"
        self.host = DEFAULT_HOST
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ws = None
        self.connected = False
        self.phase = "setup"
        self.paused = False

    # ---------- 配置 / 生命周期 ----------
    def configure(self, room: str, key: str, role: str = "player", host: str = DEFAULT_HOST):
        self.room = room.strip()
        self.key = key.strip()
        self.role = role or "player"
        self.host = host or DEFAULT_HOST

    def connect_from_url(self, url: str, role: Optional[str] = None) -> bool:
        """从完整房间链接解析并配置；支持 #room.key 与 #join.room.player。"""
        frag = ""
        if "#" in url:
            frag = url.split("#", 1)[1]
        parsed = parse_hash(frag)
        if not parsed:
            return False
        self.room, self.key = parsed["room"], parsed["key"]
        self.role = role or parsed["role"]
        return True

    def start(self):
        # 配置可能已变更（换房间/角色）：取消旧任务后重新起连接
        if self._task and not self._task.done():
            self._task.cancel()
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._ws:
            try:
                await self._ws.close(1000, "stop")
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.connected = False

    # ---------- 主循环 ----------
    async def _run(self):
        backoff = 2.0
        while not self._stop.is_set():
            if not (self.room and self.key):
                await asyncio.sleep(2)
                continue
            url = f"wss://{self.host}/rws/{self.room}?role={self.role}&key={self.key}"
            try:
                async with websockets.connect(url, ping_interval=None, open_timeout=10) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 2.0
                    log.info("rgate 已连接 role=%s room=%s", self.role, self.room)
                    await self._emit({"type": "rgate_status", "connected": True, "role": self.role})
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            self._handle_raw(raw)
                    finally:
                        ping_task.cancel()
                        self.connected = False
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                log.warning("rgate 断开: %s", e)
                await self._emit({"type": "rgate_status", "connected": False, "reason": str(e)})
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)

    async def _ping_loop(self, ws):
        while not self._stop.is_set():
            try:
                await asyncio.sleep(20)
                if ws.state.name == "OPEN":
                    await ws.send(json.dumps({"t": "ping"}))
            except Exception:
                return

    def _handle_raw(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        self._dispatch(msg)

    # ---------- 报文分发 ----------
    def _dispatch(self, n: dict):
        t = n.get("t")
        if t == "state":
            self.phase = n.get("phase", self.phase)
            if n.get("paused") is not None:
                self.paused = bool(n["paused"])
            bpm = _valid_bpm(n.get("bpm"))
            drive = n.get("drive")
            # drive 优先：可能是 "stop" 或 bpm 数值
            if drive == "stop":
                asyncio.create_task(self._emit({"type": "cmd", "cmd": "stop"}))
            elif isinstance(drive, int) and _valid_bpm(drive) is not None:
                asyncio.create_task(self._emit({"type": "bpm", "bpm": _valid_bpm(drive)}))
            elif bpm is not None:
                asyncio.create_task(self._emit({"type": "bpm", "bpm": bpm}))
            lvl = _valid_level(n.get("level"))
            if lvl is not None:
                asyncio.create_task(self._emit({"type": "level", "level": lvl}))
            asyncio.create_task(self._emit({"type": "state", "raw": n}))

        elif t == "drive":
            k = n.get("k")
            if k == "stop":
                asyncio.create_task(self._emit({"type": "cmd", "cmd": "stop"}))
            elif k == "auto":
                pass  # 自动模式，无具体数值
            else:
                bpm = _valid_bpm(k)
                if bpm is not None:
                    asyncio.create_task(self._emit({"type": "bpm", "bpm": bpm}))

        elif t == "bpm":
            bpm = _valid_bpm(n.get("k"))
            if bpm is not None:
                asyncio.create_task(self._emit({"type": "bpm", "bpm": bpm}))

        elif t == "cmd":
            k = n.get("k")
            if k in VALID_CMD:
                asyncio.create_task(self._emit({"type": "cmd", "cmd": k}))

        elif t == "level":
            lvl = _valid_level(n.get("k"))
            if lvl is not None:
                asyncio.create_task(self._emit({"type": "level", "level": lvl}))

        # 其他报文类型：本程序不驱动输出，仅留钩子

    async def _emit(self, event: dict):
        try:
            if self.on_event:
                res = self.on_event(event)
                if asyncio.iscoroutine(res):
                    await res
        except Exception as e:
            log.warning("事件回调失败: %s", e)


rgate = RgateClient(on_event=None)
