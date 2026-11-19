"""全局状态、安全闸门与浏览器命令广播总线。

安全约束（自上而下）：
  1. Panic 锁存：触发后归零、禁止再次输出，需手动 reset。
  2. 武装 / 预览：默认 dry_run（预览，不输出到硬件），需手动武装。
  3. 强度上限：用户上限 1–20，实际输出 = min(请求, 用户上限)；设备硬件量程同为 0–20。
  4. 急停：任意时刻可归零。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from fastapi import WebSocket

from . import mapping

log = logging.getLogger("state")

# 超过该秒数未收到任何信号（含心跳保活），视为掉线/无数据，自动归零停止。
# 桥接脚本每 2 秒重发当前值保活；阈值需大于心跳周期并留出网络抖动余量。
STALE_SECONDS = 5.0


class State:
    def __init__(self):
        # 安全层
        self.panic = False
        self.armed = False
        self.dry_run = True
        self.user_max = 20  # 1–20

        # 当前设备输出（三马达强度 0–20）
        self.motors: Dict[str, int] = {"A": 0, "B": 0, "C": 0}
        # 最近一次来源信号（原始缓存，供分通道映射）
        self.raw: Dict[str, Optional[object]] = {"bpm": None, "speed": None, "grip": None, "level": None}
        self.bpm: Optional[int] = None
        self.level_btn: Optional[int] = None
        self.source = "none"          # rgate / bridge / manual
        self.last_cmd = ""
        self.phase = "setup"
        self.paused = False

        # 三通道各自的强度来源：bpm / grip / level / off
        self.channel_source: Dict[str, str] = {"A": "speed", "B": "grip", "C": "level"}
        # 三通道档位偏移：正=偏强，负=偏弱，范围 -5..+5；0=不偏移
        self.channel_offset: Dict[str, int] = {"A": 0, "B": 0, "C": 0}

        # 连接状态
        self.rgate_connected = False
        self.rgate_role = ""
        self.device_browser_count = 0
        self.battery: Optional[int] = None
        self.device_info: Optional[dict] = None

        self.clamp_log: List[dict] = []
        # 最后一次收到有效驱动指令的时间戳；None = 尚未收到任何指令
        self.last_signal_at: Optional[float] = None
        self._bus: List[WebSocket] = []
        self._lock = asyncio.Lock()

    # ---------- 安全闸门 ----------
    def _clamp(self, level: int) -> int:
        limit = max(0, min(20, self.user_max))
        clamped = max(0, min(int(level), limit))
        if clamped != int(level):
            self.clamp_log.append({
                "ts": time.time(), "requested": int(level),
                "actual": clamped, "reason": "strength_limit",
            })
        return clamped

    def can_output(self) -> tuple[bool, str]:
        if self.panic:
            return False, "panic"
        if not self.armed or self.dry_run:
            return False, "not_armed"
        return True, "ok"

    # ---------- 信号入口 ----------
    def _commit(self, a: int, b: int, c: int, source: str, bpm=None, level_btn=None):
        a, b, c = self._clamp(a), self._clamp(b), self._clamp(c)
        self.motors = {"A": a, "B": b, "C": c}
        self.source = source
        self.last_signal_at = time.time()
        if bpm is not None:
            self.bpm = bpm
        if level_btn is not None:
            self.level_btn = level_btn

    def _recompute(self, source: str):
        """按各通道来源从原始缓存重算三马达强度，并叠加通道档位偏移。"""
        a = mapping.apply_offset(
            mapping.source_level(self.channel_source["A"], self.raw, self.user_max),
            self.channel_offset["A"], self.user_max)
        b = mapping.apply_offset(
            mapping.source_level(self.channel_source["B"], self.raw, self.user_max),
            self.channel_offset["B"], self.user_max)
        c = mapping.apply_offset(
            mapping.source_level(self.channel_source["C"], self.raw, self.user_max),
            self.channel_offset["C"], self.user_max)
        self._commit(a, b, c, source)

    async def apply_bpm(self, bpm, source: str = "rgate") -> dict:
        self.raw["bpm"] = bpm
        self.bpm = bpm
        self._recompute(source)
        await self._broadcast_device()
        return self.status()

    async def apply_speed(self, speed, source: str = "rgate") -> dict:
        self.raw["speed"] = speed
        self._recompute(source)
        await self._broadcast_device()
        return self.status()

    async def apply_grip(self, grip, source: str = "rgate") -> dict:
        self.raw["grip"] = grip
        self._recompute(source)
        await self._broadcast_device()
        return self.status()

    async def apply_level_btn(self, k, source: str = "rgate") -> dict:
        self.raw["level"] = k
        self.level_btn = k
        self._recompute(source)
        await self._broadcast_device()
        return self.status()

    async def apply_cmd(self, cmd: str, source: str = "rgate") -> dict:
        self.last_cmd = cmd
        cur = max(self.motors["A"], self.motors["B"], self.motors["C"])
        level = mapping.cmd_step(cmd, cur, self.user_max)
        self._commit(level, level, level, source)
        await self._broadcast_device()
        return self.status()

    async def set_motors(self, a: int, b: int, c: int, source: str = "manual") -> dict:
        self._commit(a, b, c, source)
        await self._broadcast_device()
        return self.status()

    async def apply_channels(self, channels: dict) -> bool:
        """保存三通道来源（bpm/grip/level/off），非法值保留原值并重算。"""
        ok = True
        for ch in ("A", "B", "C"):
            v = str(channels.get(ch, "")).lower()
            if v in mapping.SOURCE_KEYS:
                self.channel_source[ch] = v
            else:
                ok = False
        self._recompute("manual")
        await self._broadcast_device()
        return ok

    async def apply_offsets(self, offsets: dict) -> bool:
        """保存三通道档位偏移（-5..+5），非法值保留原值并重算。"""
        ok = True
        for ch in ("A", "B", "C"):
            raw = offsets.get(ch, self.channel_offset[ch])
            off = mapping.clamp_offset(raw)
            if off != raw:
                ok = False
            self.channel_offset[ch] = off
        self._recompute("manual")
        await self._broadcast_device()
        return ok

    async def zero(self, reason: str = "stop"):
        self.motors = {"A": 0, "B": 0, "C": 0}
        self.source = "none"
        self.last_signal_at = time.time()
        await self._broadcast_device(force_stop=True, reason=reason)

    def signal_age(self) -> Optional[float]:
        """距上次有效驱动指令的秒数；从未收到指令返回 None。"""
        if self.last_signal_at is None:
            return None
        return time.time() - self.last_signal_at

    def is_stale(self) -> bool:
        """是否超过无数据阈值（且当前仍有输出）。"""
        age = self.signal_age()
        return age is not None and age > STALE_SECONDS and any(v > 0 for v in self.motors.values())

    # ---------- 武装 / 急停 ----------
    async def trigger_panic(self):
        self.panic = True
        self.armed = False
        self.dry_run = True
        await self.zero("panic")
        log.warning("Panic 已触发，设备归零")

    def reset_panic(self):
        self.panic = False
        self.dry_run = True
        self.armed = False

    def arm(self) -> bool:
        if self.panic:
            return False
        self.armed = True
        self.dry_run = False
        return True

    def disarm(self):
        self.armed = False
        self.dry_run = True

    # ---------- 广播总线 ----------
    async def connect_device(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._bus.append(ws)
        self.device_browser_count = len(self._bus)
        # 接入即下发当前状态
        await self._send(ws, self._device_command(force_stop=False))

    async def disconnect_device(self, ws: WebSocket):
        async with self._lock:
            if ws in self._bus:
                self._bus.remove(ws)
        self.device_browser_count = len(self._bus)

    def _device_command(self, force_stop: bool = False, reason: str = "") -> dict:
        ok, _ = self.can_output()
        # 急停 / 未武装 / 预览模式：硬件一律归零，界面状态仍保留计算值用于显示
        if force_stop or self.panic or not ok:
            return {"type": "device_command", "cmd": "stop", "reason": reason or "gate"}
        return {
            "type": "device_command",
            "cmd": "realtime",
            "motors": dict(self.motors),
            "bpm": self.bpm,
            "level": self.level_btn,
            "source": self.source,
        }

    async def _broadcast_device(self, force_stop: bool = False, reason: str = ""):
        msg = self._device_command(force_stop=force_stop, reason=reason)
        await self._broadcast(msg)

    async def _send(self, ws: WebSocket, msg: dict):
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    async def _broadcast(self, msg: dict):
        async with self._lock:
            targets = list(self._bus)
        dead = []
        for ws in targets:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self._bus:
                        self._bus.remove(ws)
            self.device_browser_count = len(self._bus)

    async def push_status(self):
        await self._broadcast({"type": "status", **self.status()})

    # ---------- 状态快照 ----------
    def status(self) -> dict:
        ok, reason = self.can_output()
        return {
            "panic": self.panic,
            "armed": self.armed,
            "dry_run": self.dry_run,
            "can_output": ok,
            "gate_reason": reason,
            "user_max": self.user_max,
            "motors": dict(self.motors),
            "channel_source": dict(self.channel_source),
            "channel_offset": dict(self.channel_offset),
            "raw": dict(self.raw),
            "bpm": self.bpm,
            "level_btn": self.level_btn,
            "source": self.source,
            "last_cmd": self.last_cmd,
            "phase": self.phase,
            "paused": self.paused,
            "signal_age": self.signal_age(),
            "stale": self.is_stale(),
            "rgate_connected": self.rgate_connected,
            "rgate_role": self.rgate_role,
            "device_browser_count": self.device_browser_count,
            "battery": self.battery,
            "device_info": self.device_info,
            "clamp_log": self.clamp_log[-10:],
        }


state = State()
