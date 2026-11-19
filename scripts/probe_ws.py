"""房间 WS 连通性 / 报文探测调试脚本。

用法：
  python probe_ws.py <房间链接或 room.key> [role]
"""
import asyncio
import json
import re
import sys

import websockets

HASH_RE = re.compile(r"([0-9a-f]{32})\.([0-9a-f]{32})")
HOST = "example.com"  # 按目标站点配置


def parse(arg: str):
    m = HASH_RE.search(arg)
    if not m:
        raise SystemExit("参数需含 #<room>.<key> 或 #join.<room>.<player>（两段 32 位 hex）")
    return m.group(1), m.group(2)


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "room.key"
    role = sys.argv[2] if len(sys.argv) > 2 else "player"
    room, key = parse(target)
    url = f"wss://{HOST}/rws/{room}?role={role}&key={key}"
    print("connect", url, flush=True)
    try:
        async with websockets.connect(url, ping_interval=None, open_timeout=10) as ws:
            print("connected", flush=True)
            await ws.send(json.dumps({"t": "ping"}))
            n = 0
            while n < 15:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    print("[timeout] 8s 无报文", flush=True)
                    break
                n += 1
                print("MSG>", str(raw)[:400], flush=True)
    except Exception as e:
        print("ERR", type(e).__name__, e, flush=True)


asyncio.run(main())
