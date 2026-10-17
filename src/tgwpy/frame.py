"""构造并发送候选消息帧，用服务端的回包反推帧格式。

已确认的帧头结构（来自 QueryReqSender::Logon 反汇编）：

    偏移 0x00  1 字节   协议/版本号
    偏移 0x01  2 字节   消息类型（观察到 665 / 666 / 667）
    偏移 0x03  8 字节   发送时清零，推测 task_id
    偏移 0x0b  4 字节   发送时清零，推测 body 长度
    偏移 0x0f  2 字节   来自入参
    偏移 0x11  4 字节   来自入参
    偏移 0x75  1 字节   来自入参

登录帧定长 118 字节（esi = 0x76 = 0x75 + 1）。
用户名/密码走 set_follow_up 的分段跟随，头里存各段偏移——这部分还没逆出来。

用法：
    python src/tgwpy/frame.py build --msg 665 --ver 1
    python src/tgwpy/frame.py sweep            # 扫协议号/消息类型，看服务端有无回包
    python src/tgwpy/frame.py send --hex <hex> # 发任意帧
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FRAME_LEN = 118


def build(ver: int = 0, msg: int = 665, f0f: int = 0, f11: int = 0, f75: int = 0,
          body: bytes = b"") -> bytes:
    buf = bytearray(FRAME_LEN)
    buf[0x00] = ver & 0xFF
    struct.pack_into("<H", buf, 0x01, msg & 0xFFFF)
    struct.pack_into("<Q", buf, 0x03, 0)          # 发送时清零
    struct.pack_into("<I", buf, 0x0B, 0)          # 发送时清零
    struct.pack_into("<H", buf, 0x0F, f0f & 0xFFFF)
    struct.pack_into("<I", buf, 0x11, f11 & 0xFFFFFFFF)
    buf[0x75] = f75 & 0xFF
    if body:
        buf[0x13:0x13 + len(body)] = body[: 0x75 - 0x13]
    return bytes(buf)


def describe(data: bytes) -> str:
    if not data:
        return "(空)"
    head = " ".join(f"{b:02x}" for b in data[:16])
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{len(data):4d}B  {head}...  |{ascii_part[:64]}|"


def cmd_build(args) -> int:
    data = build(args.ver, args.msg, args.f0f, args.f11, args.f75)
    print(f"len={len(data)}")
    print(data.hex())
    for off in (0x00, 0x01, 0x03, 0x0B, 0x0F, 0x11, 0x75):
        w = {0x00: 1, 0x01: 2, 0x03: 8, 0x0B: 4, 0x0F: 2, 0x11: 4, 0x75: 1}[off]
        val = int.from_bytes(data[off:off + w], "little")
        print(f"  0x{off:02x} ({w}B) = {val}")
    return 0


def cmd_send(args) -> int:
    import asyncio

    from probe import Recorder, env, now, ssl_context
    import websockets

    data = bytes.fromhex(args.hex)
    host = env("TGW_HOST", True)
    port = env("TGW_PORT", True)
    rec = Recorder(Path("logs") / f"frames-{args.tag}.jsonl")

    async def go():
        url = f"wss://{host}:{port}/amd/dgw/"
        async with websockets.connect(
            url, ssl=ssl_context(), server_hostname="www.dgw.com",
            open_timeout=20, max_size=None,
        ) as ws:
            print(f"[{now()}] 已连接，发 {len(data)} 字节")
            rec.log("send", data, args.tag)
            await ws.send(data)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=args.seconds)
            except asyncio.TimeoutError:
                print(f"[{now()}] {args.seconds}s 内无回包")
                return
            if isinstance(raw, str):
                raw = raw.encode()
            rec.log("recv", raw, f"回包 <- {args.tag}")
            print(f"[{now()}] 回包 {describe(raw)}")

    asyncio.run(go())
    return 0


def cmd_sweep(args) -> int:
    import asyncio

    from probe import env, now, ssl_context
    import websockets

    host = env("TGW_HOST", True)
    port = env("TGW_PORT", True)

    async def go():
        url = f"wss://{host}:{port}/amd/dgw/"
        async with websockets.connect(
            url, ssl=ssl_context(), server_hostname="www.dgw.com",
            open_timeout=20, max_size=None,
        ) as ws:
            print(f"[{now()}] 已连接，开始扫描")
            for msg in args.msgs:
                for ver in args.vers:
                    data = build(ver=ver, msg=msg)
                    try:
                        await ws.send(data)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  msg={msg} ver={ver} 发送失败 {exc}")
                        continue
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=args.seconds)
                    except asyncio.TimeoutError:
                        print(f"  msg={msg:4d} ver={ver:3d}  无回包")
                        continue
                    if isinstance(raw, str):
                        raw = raw.encode()
                    print(f"  msg={msg:4d} ver={ver:3d}  有回包! {describe(raw)}")
                    return
            print("全部组合无回包")

    asyncio.run(go())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--ver", type=int, default=1)
    b.add_argument("--msg", type=int, default=665)
    b.add_argument("--f0f", type=int, default=0)
    b.add_argument("--f11", type=int, default=0)
    b.add_argument("--f75", type=int, default=0)
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("send")
    s.add_argument("--hex", required=True)
    s.add_argument("--tag", default="manual")
    s.add_argument("--seconds", type=float, default=8.0)
    s.set_defaults(fn=cmd_send)

    w = sub.add_parser("sweep")
    w.add_argument("--msgs", type=int, nargs="+", default=[665, 666, 667])
    w.add_argument("--vers", type=int, nargs="+", default=list(range(0, 8)))
    w.add_argument("--seconds", type=float, default=4.0)
    w.set_defaults(fn=cmd_sweep)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
