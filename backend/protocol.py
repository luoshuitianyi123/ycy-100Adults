"""YSKJ 三马达设备 BLE 通信协议 V1.1

帧格式：包头 0x35 | 命令字 | 载荷… | 校验和
校验和 = 前面所有字节逐字节累加后取低 8 位（sum & 0xFF）。
多字节数据高字节在前（本协议载荷均为单字节强度，无多字节数值）。

Service  FF40
Write    FF41  WRITE_WITHOUT_RESPONSE | DYNAMIC
Notify   FF42  NOTIFY

命令字：
  0x10 设备信息查询（主机→从机 3 字节；从机→主机 10 字节）
  0x11 固定模式控制（主机→从机 5 字节，无需应答）
  0x12 实时强度控制（主机→从机 6 字节，无需应答）
  0x13 电量上报（从机→主机 5 字节）
"""

from __future__ import annotations

# ---- GATT UUID ----
SVC_UUID = "0000ff40-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff41-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff42-0000-1000-8000-00805f9b34fb"

# ---- 帧常量 ----
HEADER = 0x35

CMD_INFO = 0x10
CMD_MODE = 0x11
CMD_REALTIME = 0x12
CMD_BATTERY = 0x13

# 马达位掩码（0x11 固定模式命令使用，可按位组合）
MOTOR_A = 0x01
MOTOR_B = 0x02
MOTOR_C = 0x04
MOTOR_MASK = {"A": MOTOR_A, "B": MOTOR_B, "C": MOTOR_C}

# 强度量程：0 关闭，1-20 强度等级
LEVEL_MIN = 0
LEVEL_MAX = 20


def checksum(body: list[int]) -> int:
    """前面所有字节累加和取低 8 位。"""
    return sum(body) & 0xFF


def _clamp_level(v: int) -> int:
    return max(LEVEL_MIN, min(LEVEL_MAX, int(v)))


def build_info_query() -> list[int]:
    """0x10 设备信息查询：35 10 <sum>。"""
    body = [HEADER, CMD_INFO]
    body.append(checksum(body))
    return body


def build_mode(motor_mask: int, mode: int) -> list[int]:
    """0x11 固定模式控制：35 11 <马达掩码> <模式选择> <sum>。

    mode: 0 关闭；1-N 运行固定模式（N 来自信息查询上报数）。
    motor_mask: MOTOR_A / MOTOR_B / MOTOR_C 或组合（AB=0x03, ABC=0x07）。
    """
    mask = motor_mask & 0x07
    mode = max(0, int(mode))
    body = [HEADER, CMD_MODE, mask, mode]
    body.append(checksum(body))
    return body


def build_realtime(a: int, b: int, c: int) -> list[int]:
    """0x12 实时强度控制：35 12 <A> <B> <C> <sum>。

    强度 0 关闭，1-20 等级；只有数据改变时才需要下发。
    """
    body = [
        HEADER,
        CMD_REALTIME,
        _clamp_level(a),
        _clamp_level(b),
        _clamp_level(c),
    ]
    body.append(checksum(body))
    return body


def parse_notify(data) -> dict:
    """解析 FF42 上报帧。

    入参可为 bytes / list[int] / DataView（浏览器侧会转成 Uint8Array 后传入）。
    返回 dict：{type: 'info'|'battery'|'unknown'|'bad', ...}
    """
    if data is None:
        return {"type": "bad", "reason": "empty"}
    if hasattr(data, "buffer"):  # DataView / Uint8Array
        data = list(data)
    else:
        data = list(data)

    if not data:
        return {"type": "bad", "reason": "empty"}
    if data[0] != HEADER:
        return {"type": "bad", "reason": f"header=0x{data[0]:02x}", "raw": data}

    if len(data) < 3:
        return {"type": "bad", "reason": "short", "raw": data}

    cmd = data[1]

    if cmd == CMD_INFO and len(data) >= 10:
        # 35 10 型号ID 版本 A模式数 B模式数 C模式数 预留 预留 sum
        return {
            "type": "info",
            "model_id": data[2],
            "version": data[3],
            "modes": {"A": data[4], "B": data[5], "C": data[6]},
            "raw": data,
        }

    if cmd == CMD_BATTERY and len(data) >= 5:
        # 35 13 01 电量(0-100) sum
        batt = data[3]
        return {"type": "battery", "battery": max(0, min(100, batt)), "raw": data}

    return {"type": "unknown", "cmd": f"0x{cmd:02x}", "raw": data}


def to_hex(frame: list[int]) -> str:
    return " ".join(f"{b:02x}" for b in frame)
