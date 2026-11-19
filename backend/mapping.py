"""信号 → 设备强度映射。

来源信号：
  - BPM 数值：0=停止，合法整数 20–300；
    本程序取有效下限 40、上限 300 做线性映射。
  - level 档：1–9 整数。
  - cmd：fast / slow / stop / go。

设备强度量程 0–20（0 关闭，1–20 等级）。数值越大，强度越高。
"""

from __future__ import annotations

from typing import Optional

# ---- BPM 区间（外部页面前端校验：0 或 20–300 整数） ----
BPM_STOP = 0
BPM_MIN_VALID = 20      # 外部页面允许的最低非零 BPM
BPM_LOW = 40            # 本程序有效映射下限（用户口径：最慢 40，可停止）
BPM_HIGH = 300          # 本程序映射上限（用户口径：最快 300）

# 外部页面内部 6 档锚点（备查，不参与本地分档）
DRIVE_BPM = {
    "stop": 0,
    "vslow": 40,
    "slow": 60,
    "mid": 100,
    "fast": 140,
    "vfast": 180,
}
DRIVE_LEVELS = ["stop", "vslow", "slow", "mid", "fast", "vfast"]

# 本地分档：40–300 每 20 一档，共 14 档，再拉伸到强度 1–max_level
BPM_STEP = 20
BPM_LEVELS = (BPM_HIGH - BPM_LOW) // BPM_STEP + 1  # 14


def bpm_to_level(bpm, max_level: int = 20) -> int:
    """BPM → 设备强度等级（0–max_level），按 20 BPM 一档阶梯分档。

    - None / 0 / stop → 0（停止）
    - 20 ≤ bpm < 40 → 1（最低有效档）
    - 40–300：每 20 一档，共 14 档，档位拉伸映射到 1–max_level
    - >300 → max_level
    """
    if bpm is None:
        return 0
    try:
        b = int(bpm)
    except (TypeError, ValueError):
        return 0

    if b <= BPM_STOP:
        return 0
    max_level = max(1, min(20, int(max_level)))

    if b < BPM_LOW:
        # 20–39：外部页面允许但低于用户口径下限，给最低有效档
        return 1

    if b >= BPM_HIGH:
        idx = BPM_LEVELS - 1
    else:
        idx = (b - BPM_LOW) // BPM_STEP
    idx = max(0, min(BPM_LEVELS - 1, idx))

    level = 1 + round(idx / (BPM_LEVELS - 1) * (max_level - 1))
    return max(1, min(max_level, level))


def level_btn_to_level(k, max_level: int = 20) -> int:
    """外部页面 level 按钮 1–9 → 设备强度 1–max_level 线性映射。"""
    try:
        k = int(k)
    except (TypeError, ValueError):
        return 0
    if k <= 0:
        return 0
    max_level = max(1, min(20, int(max_level)))
    k = max(1, min(9, k))
    return 1 + round((k - 1) / 8 * (max_level - 1))


# ---- 速度段 / 强度点条（外部页面档位） ----
# 速度 rp-seg：6 个点，点亮个数 0–5；强度 rp-dot5：5 个点，点亮个数 0–4。
GRIP5 = ["feather", "soft", "normal", "hard", "harder"]
SOURCE_KEYS = ("bpm", "speed", "grip", "level", "off")


def _grip_index(grip) -> Optional[int]:
    """把 grip 档名或索引归到 0–4；无法识别返回 None。"""
    if grip is None:
        return None
    if isinstance(grip, (int, float)) and not isinstance(grip, bool):
        i = int(grip)
        return i if 0 <= i < len(GRIP5) else None
    s = str(grip).strip().lower()
    return GRIP5.index(s) if s in GRIP5 else None


def _slot_to_level(slot, slots: int, max_level: int) -> int:
    """档位 0=停止；1..slots 线性拉伸到 1–max_level。"""
    if slot is None:
        return 0
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return 0
    if slot <= 0:
        return 0
    max_level = max(1, min(20, int(max_level)))
    slot = max(1, min(slots, slot))
    return 1 + round((slot - 1) / (slots - 1) * (max_level - 1))


def speed_to_level(speed, max_level: int = 20) -> int:
    """速度段档位 0–5 → 强度；0 停止，其余线性拉伸。"""
    return _slot_to_level(speed, 5, max_level)


def grip_to_level(grip, max_level: int = 20) -> int:
    """强度 → 强度等级。数字为点条点亮个数(0–4)；档位名归到 1–5。"""
    idx = _grip_index(grip)
    if idx is None:
        return 0
    if isinstance(grip, (int, float)) and not isinstance(grip, bool):
        return _slot_to_level(idx, 4, max_level)
    return _slot_to_level(idx + 1, 5, max_level)


def source_level(source: str, raw: dict, max_level: int = 20) -> int:
    """按通道来源从原始信号缓存取数并映射到强度。"""
    if source == "bpm":
        return bpm_to_level(raw.get("bpm"), max_level)
    if source == "speed":
        return speed_to_level(raw.get("speed"), max_level)
    if source == "grip":
        return grip_to_level(raw.get("grip"), max_level)
    if source == "level":
        return level_btn_to_level(raw.get("level"), max_level)
    return 0


def clamp_level(v: int, max_level: int) -> int:
    return max(0, min(int(max_level), int(v)))


# ---- 通道档位偏移：正=整体偏强，负=整体偏弱 ----
OFFSET_MIN = -10
OFFSET_MAX = 10


def clamp_offset(o) -> int:
    """把偏移量夹到 [-10, +10]。"""
    try:
        o = int(o)
    except (TypeError, ValueError):
        return 0
    return max(OFFSET_MIN, min(OFFSET_MAX, o))


def apply_offset(level: int, offset, max_level: int) -> int:
    """在映射强度上叠加通道偏移。

    - level <= 0（停止 / 无数据 / 来源关闭）保持 0，偏移不把它抬高；
    - 其余按 level + offset 计算，再夹到 0–max_level。
    """
    if level <= 0:
        return 0
    return clamp_level(level + clamp_offset(offset), max_level)


def cmd_step(cmd: str, current: int, max_level: int) -> int:
    """fast/slow/stop/go 对当前强度做单步调整。"""
    step = max(1, round(max_level * 0.15))
    if cmd == "stop":
        return 0
    if cmd == "fast":
        return clamp_level(current + step, max_level)
    if cmd == "slow":
        return clamp_level(current - step, max_level)
    if cmd == "go":
        return clamp_level(current or step, max_level)
    return current
