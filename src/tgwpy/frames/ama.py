"""AMA（公网 Internet 模式）登录认证帧构造。

来源：`amd::ums::AMAClientImpl::OnConnect(Endpoint*, Property&)` @ 0x514590
（`build/tgw-1.0.9.2-py3-none-any/tgw/common_linux_lib64/libtgw.so`）。

⚠️ 本文件的偏移全部来自 `objdump -d` 原始反汇编，不是 angr 的结构体渲染。
angr 把 0x1e/0x20/0x22/0x24 整体错位了 2 字节，README 里的旧表格不可信。

一帧登录 = 两条 WebSocket 二进制消息，`OnConnect` 里各调一次 `SendMsgBlock`：

    1) ServiceTypeReq  17 字节，条件发送（AMAAuthInfo+0x4CA != 0，
       `amd::rqa::Impl::UMSClientInit` @ 0x292c2f 把它置 1，所以必发）
    2) RQAAuthReq      8431 + len(json) 字节

帧体（RQAAuthReq）的空间布局是硬约束：

    0x000  头部散字段
    0x02e  char[64]   username
    0x06e  char[128]  password（已哈希，见 hash_password）
    0x0ee  u8         服务名个数
    0x0ef  char[32][256]  服务名表，正好顶到 0x20ef
    0x20ef char[]     boost::property_tree 生成的 JSON，长度不定

0x0ef + 256*32 == 0x20ef 严丝合缝，这个吻合度是整张表最硬的证据。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json as _json
import os
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------- 常量

# .rodata 0x600a20，OnConnect 之前由 UMSClientInit 用 strncpy 填进
# AMAAuthInfo+0x3C9（→ AMAClientImpl+0x5D9），再被读出来当 Version。
VERSION = "V4.3.0.260626-rc2.0-YHZQ"

# `mov edi, 0x7ea5` @ 0x514792
MAGIC = 0x7EA5

SERVICE_TYPE_REQ_LEN = 0x11          # NewMessage(ep, 0x11) @ 0x514754
AUTH_FIXED_LEN = 0x20EF              # 8431，NewMessage(ep, json_len + 0x20ef) @ 0x514921
SERVICE_NAME_OFF = 0x0EF
SERVICE_NAME_MAX = 256
SERVICE_NAME_LEN = 32

# UMSClientInit 填的 Config 项 +0x24 = 6，OnConnect 读同一处 ×1000 当超时毫秒。
# Config 被 memcpy 到 AMAClientImpl+0x90（0x4cc.. 处的 rep movsq，0x513bf5），
# 所以 0x90+0x28 = 0xb8 正好是 OnConnect 读的那个 u16。
DEFAULT_TIMEOUT_MS = 6000

# `amd::rqa::Utils::ConvertMulChannelMode` @ 0x2da2c0 按名字 OR 位：
#   "TCP"  @ .rodata 0x60a702 -> 0x20000
#   "QTCP" @ .rodata 0x60a6d8 -> 0x80000
#   "RTCP" @ .rodata 0x60a6dd -> 0x100000
# 名字列表为空则 out == 0。互联网模式下 channel_mode 由原生层给默认值，
# 这里默认取 QTCP（kInternetMode 对应的查询通道），未验证。
CHANNEL_MODE_NONE = 0x000000
CHANNEL_MODE_TCP = 0x20000
CHANNEL_MODE_QTCP = 0x80000
CHANNEL_MODE_RTCP = 0x100000
DEFAULT_CHANNEL_MODE = CHANNEL_MODE_QTCP


# ---------------------------------------------------------------- 密码

def hash_password(password: str) -> str:
    """密码在进帧之前已经被原生层哈希过两次，原地覆盖。

    `amd::ums::AMAClientImpl::Start` @ 0x513d78-0x513e3c：
        str    = std::string(self->+0x258)          # 明文密码
        h1     = sha256::hash256_hex_string(str)    # 0x513dbe
        h2     = sha256::hash256_hex_string(h1)     # 0x513de6
        strncpy(self->+0x258, h2, 0x100)            # 0x513e3c，就地覆盖
    `hash256_hex_string` @ 0x518100 输出 64 字符小写十六进制。
    长度 64 < 128，OnConnect 的 strncpy(dst, src, 128) 会把余下补零。

    Python 侧 `tgw.Cfg.password` 传的是明文，哈希必须在这里补上。
    """
    h1 = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hashlib.sha256(h1.encode("ascii")).hexdigest()


# ---------------------------------------------------------------- JSON

def build_json(version: str = VERSION, order_book_logon_type: int = 0,
               enable_iopv: bool = True, style: str = "pretty") -> bytes:
    """复刻 `write_json(stream, ptree, /*pretty=*/1)` 的输出。

    键来自 0x514811/0x514850/0x514881 三处 `sub_911f00` 的字符串常量，
    值来自紧跟其后的 put<unsigned int> / put<bool> 调用：
        Version            <- AMAAuthInfo+0x3C9  (.rodata 0x600a20)
        OrderBookLogonType <- AMAAuthInfo+0x4CB  (UMSClientInit 未赋值 -> 0)
        EnableIOPV        <- 栈上常量 1
    version 为空串时 Version 键整个不出现（0x51480b 的 `je`）。
    """
    obj: dict[str, str] = {}
    if version:
        obj["Version"] = version
    obj["OrderBookLogonType"] = str(order_book_logon_type)
    obj["EnableIOPV"] = "true" if enable_iopv else "false"
    if style == "compact":
        return _json.dumps(obj, separators=(",", ":")).encode()
    # boost 的 write_json 缩进宽度来自 write_json_helper 0x2b4a57 的
    # `lea edx, [4*rcx]`（深度 × 4），分隔符是 `" : "`，冒号后还有一个空格。
    body = ",\n".join(f'    "{k}" : "{v}"' for k, v in obj.items())
    return ("{\n" + body + "\n}").encode()


# ---------------------------------------------------------------- 帧

def build_service_type_req() -> bytes:
    """17 字节前置头，OnConnect 0x51477a-0x5147c0。

    先显式清零 17 字节（0x51477a/0x514781/0x514789 三条 mov），再逐字段赋值。
    """
    buf = bytearray(SERVICE_TYPE_REQ_LEN)          # 显式清零
    struct.pack_into("<H", buf, 0x00, MAGIC)       # 0x5147a2
    buf[0x02] = 0x01                                # 0x5147a9
    struct.pack_into("<I", buf, 0x03, 0x01)         # 0x5147ad
    struct.pack_into("<I", buf, 0x07, 0x04)         # 0x5147b4
    struct.pack_into("<H", buf, 0x0B, 0x0D)         # 0x5147bb
    struct.pack_into("<I", buf, 0x0D, 0x01)         # 0x5147c0
    return bytes(buf)


def build_auth_frame(username: str, password: str, *,
                     services: tuple[str, ...] = (),
                     channel_mode: int = DEFAULT_CHANNEL_MODE,
                     timeout_ms: int = DEFAULT_TIMEOUT_MS,
                     order_book_logon_type: int = 0,
                     enable_iopv: bool = True,
                     version: str = VERSION,
                     plain_password: bool = False,
                     json_style: str = "pretty",
                     service_type_req: bool = True,
                     ) -> tuple[bytes, bytes] | bytes:
    """构造完整登录帧。

    默认返回 `(ServiceTypeReq, RQAAuthReq)` 二元组；`service_type_req=False`
    只返回 RQAAuthReq。

    ⚠️ 未验证的格子：
      - 0x02..0x06、0x0a..0x19：原生代码从不赋值，值来自内存池脏数据。
        这里留 0。服务端若读这些字段，真客户端给的是随机值，我们的帧就
        不可能对得上——这也是扫描时要重点试的维度。
      - 0x0ef..0x20ef 的服务名表：只有前 `count` 项被 strncpy 覆盖，
        剩下 8192 字节同样是脏数据，这里留 0。
      - frame[0x26] channel_mode 的具体取值取决于服务端下发的 channel 模式。
      - OrderBookLogonType 实测取 0（UMSClientInit 从没写过 +0x4CB）。
    """
    js = build_json(version, order_book_logon_type, enable_iopv, json_style)
    js_len = len(js)

    total = AUTH_FIXED_LEN + js_len
    body = bytearray(total)                        # 显式清零（原生不清）
    struct.pack_into("<I", body, 0x1A, js_len + 0x20CF)   # 0x51495d
    body[0x00] = 0x06                                      # 0x51496a
    body[0x01] = 0x00                                      # 0x51496d
    body[0x07] = 0x06                                      # 0x514978
    body[0x08] = 0x00                                      # 0x514974
    body[0x09] = 0x02                                      # 0x51497c
    struct.pack_into("<H", body, 0x1E, 32)                 # 0x514980
    struct.pack_into("<H", body, 0x24, js_len + 0x20CF - 6)  # 0x514984
    struct.pack_into("<H", body, 0x22, 0x01)               # 0x514989
    struct.pack_into("<H", body, 0x20, 0x0B)               # 0x51498d

    pwd = password if plain_password else hash_password(password)
    _strncpy(body, 0x2E, username, 64)                     # 0x514996
    _strncpy(body, 0x6E, pwd, 128)                         # 0x5149b5
    struct.pack_into("<I", body, 0x26, channel_mode & 0xFFFFFFFF)  # 0x5149c3
    body[0xEE] = len(services) & 0xFF                      # 0x5149d5
    struct.pack_into("<I", body, 0x2A, timeout_ms)         # 0x514a05
    for i, name in enumerate(services[:SERVICE_NAME_MAX]):  # 0x514a20-0x514a55
        _strncpy(body, SERVICE_NAME_OFF + SERVICE_NAME_LEN * i, name, SERVICE_NAME_LEN)
    body[AUTH_FIXED_LEN:AUTH_FIXED_LEN + js_len] = js      # 0x514a5c

    if service_type_req:
        return build_service_type_req(), bytes(body)
    return bytes(body)


def _strncpy(buf: bytearray, off: int, src: str, n: int) -> None:
    """等价于 C 的 strncpy：截断到 n，不补零（调用方缓冲区已清零）。"""
    raw = src.encode("utf-8")[:n]
    buf[off:off + len(raw)] = raw


def describe(frame: bytes) -> str:
    return f"{len(frame):5d}B  {frame[:48].hex()}" + (" ..." if len(frame) > 48 else "")


# ---------------------------------------------------------------- 联调

def _dumps(msg) -> str:
    if isinstance(msg, bytes):
        return msg.hex()
    return "\n".join(f"{'--':>2} {len(m):5d}B  {m[:32].hex()}..." for m in msg)


async def _try(env_overrides: dict, seconds: float, tag: str) -> bytes | None:
    """开一条连接发一帧，等回包。返回首个回包或 None。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from probe import env, now, ssl_context  # noqa: PLC0415
    import websockets  # noqa: PLC0415

    opts = dict(env_overrides)
    user, pwd = env("TGW_USER", True), env("TGW_PASSWORD", True)
    payload = build_auth_frame(user, pwd, **opts)

    host, port = env("TGW_HOST", True), env("TGW_PORT", True)
    url = f"wss://{host}:{port}/amd/dgw/"
    async with websockets.connect(
        url, ssl=ssl_context(), server_hostname="www.dgw.com",
        open_timeout=20, max_size=None,
    ) as ws:
        for part in (payload if isinstance(payload, tuple) else (payload,)):
            await ws.send(part)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
        except asyncio.TimeoutError:
            print(f"  [{now()}] {tag}: 无回包")
            return None
        if isinstance(raw, str):
            raw = raw.encode()
        print(f"  [{now()}] {tag}: 回包 {len(raw)}B  {raw[:64].hex()}")
        return raw


def main() -> int:
    ap = argparse.ArgumentParser(description="AMA 登录帧构造 / 联调")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="只构造并打印，不连网关")
    b.add_argument("--channel-mode", type=lambda s: int(s, 0), default=DEFAULT_CHANNEL_MODE)
    b.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS)
    b.add_argument("--plain-password", action="store_true")
    b.add_argument("--json-style", default="pretty", choices=["pretty", "compact"])
    b.add_argument("--no-service-type-req", action="store_true")
    b.set_defaults(fn=lambda a: _cmd_build(a))

    s = sub.add_parser("send", help="连网关发一帧")
    s.add_argument("--channel-mode", type=lambda s: int(s, 0), default=DEFAULT_CHANNEL_MODE)
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS)
    s.add_argument("--plain-password", action="store_true")
    s.add_argument("--json-style", default="pretty", choices=["pretty", "compact"])
    s.add_argument("--no-service-type-req", action="store_true")
    s.add_argument("--seconds", type=float, default=10.0)
    s.set_defaults(fn=_cmd_send)

    w = sub.add_parser("sweep", help="按维度扫可疑字段")
    w.add_argument("--seconds", type=float, default=8.0)
    w.set_defaults(fn=_cmd_sweep)

    args = ap.parse_args()
    return args.fn(args)


def _opts(a) -> dict:
    return {
        "channel_mode": a.channel_mode,
        "timeout_ms": a.timeout,
        "plain_password": a.plain_password,
        "json_style": a.json_style,
        "service_type_req": not a.no_service_type_req,
    }


def _cmd_build(a) -> int:
    user, pwd = os.environ.get("TGW_USER", "USER"), os.environ.get("TGW_PASSWORD", "PASS")
    parts = build_auth_frame(user, pwd, **_opts(a))
    if not isinstance(parts, tuple):
        parts = (parts,)
    body = parts[-1]
    print(_dumps(parts))
    print(f"frame total = {len(body)}  json_len = {len(body) - AUTH_FIXED_LEN}")
    for off, wid, name in ((0x00, 1, "field_0"), (0x07, 1, "field_7"), (0x09, 1, "field_9"),
                           (0x1A, 4, "len1a"), (0x1E, 2, "f1e"), (0x20, 2, "f20"),
                           (0x22, 2, "f22"), (0x24, 2, "len24"), (0x26, 4, "channel_mode"),
                           (0x2A, 4, "timeout"), (0xEE, 1, "svc_count")):
        print(f"  0x{off:04x} ({wid}B) = {int.from_bytes(body[off:off + wid], 'little')}")
    print(f"  username  = {body[0x2E:0x2E + 64].split(chr(0).encode())[0]!r}")
    print(f"  password  = {body[0x6E:0x6E + 64].split(chr(0).encode())[0]!r}")
    print(f"  json      = {body[AUTH_FIXED_LEN:].decode()!r}")
    return 0


async def _sweep(seconds: float) -> int:
    dims: list[tuple[str, dict]] = []
    for cm in (0x80000, 0x20000, 0x100000, 0x0):
        dims.append((f"channel_mode=0x{cm:x}", {"channel_mode": cm}))
    for to in (6000, 3000, 0):
        dims.append((f"timeout={to}", {"timeout_ms": to}))
    dims.append(("plain_password", {"plain_password": True}))
    dims.append(("json_compact", {"json_style": "compact"}))
    dims.append(("no_service_type_req", {"service_type_req": False}))

    user, pwd = os.environ["TGW_USER"], os.environ["TGW_PASSWORD"]
    host, port = os.environ["TGW_HOST"], os.environ["TGW_PORT"]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from probe import now, ssl_context
    import websockets

    hits = 0
    for tag, opts in dims:
        parts = build_auth_frame(user, pwd, **opts)
        if not isinstance(parts, tuple):
            parts = (parts,)
        try:
            async with websockets.connect(
                f"wss://{host}:{port}/amd/dgw/", ssl=ssl_context(),
                server_hostname="www.dgw.com", open_timeout=20, max_size=None,
            ) as ws:
                for p in parts:
                    await ws.send(p)
                raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
                if isinstance(raw, str):
                    raw = raw.encode()
                print(f"  [{now()}] {tag}: 回包 {len(raw)}B  {raw.hex()}")
                hits += 1
        except asyncio.TimeoutError:
            print(f"  [{now()}] {tag}: 无回包")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{now()}] {tag}: 异常 {type(exc).__name__}: {exc}")
    print(f"扫了 {len(dims)} 组，命中 {hits} 组")
    return 0 if hits else 1


def _cmd_sweep(a) -> int:
    return asyncio.run(_sweep(a.seconds))


def _cmd_send(a) -> int:
    user, pwd = os.environ["TGW_USER"], os.environ["TGW_PASSWORD"]
    parts = build_auth_frame(user, pwd, **_opts(a))
    if not isinstance(parts, tuple):
        parts = (parts,)
    print(_dumps(parts))

    async def go():
        from ..probe import now, ssl_context
        import websockets

        url = f"wss://{os.environ['TGW_HOST']}:{os.environ['TGW_PORT']}/amd/dgw/"
        async with websockets.connect(
            url, ssl=ssl_context(), server_hostname="www.dgw.com",
            open_timeout=20, max_size=None,
        ) as ws:
            for p in parts:
                await ws.send(p)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=a.seconds)
            except asyncio.TimeoutError:
                print(f"[{now()}] {a.seconds}s 内无回包")
                return 1
            if isinstance(raw, str):
                raw = raw.encode()
            print(f"[{now()}] 回包 {len(raw)}B\n{raw.hex()}")
            return 0

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
