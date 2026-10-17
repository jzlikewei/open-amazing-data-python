"""对真实网关做协议探测。

libtgw.so 的 WebSocket 层是 websocketpp + boost::asio TLS，都是标准实现，
所以握手、分帧、心跳直接用 Python 的 websockets 库，本文件只负责
把线上真实报文记录下来，作为反推消息帧格式的依据。

凭据一律从环境变量读，不落盘、不进 git：
    export TGW_HOST=...        网关 IP
    export TGW_PORT=...        端口
    export TGW_USER=...        账号
    export TGW_PASSWORD=...    密码

用法：
    python src/tgwpy/probe.py handshake          # 连上，看服务端先说什么
    python src/tgwpy/probe.py send <hex|@file>    # 发一段原始字节并打印回包
    python src/tgwpy/probe.py listen [秒]         # 只收不发
每次会话的报文追加写入 logs/probe-*.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS = ROOT / "logs"
URL_PATH = "/amd/dgw/"

# 被当脚本直接跑时（`python src/tgwpy/probe.py`）sys.path[0] 是 src/tgwpy，
# 补上 src 才能 `import tgwpy.client`。
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tgwpy.client import resolve_ca_cert  # noqa: E402

# 服务端证书 CN=www.dgw.com（archforce 自签链），不是按 IP 签的。
# 传 IP 进去校验必然报 "IP address mismatch"，要用这个域名做 SNI 才能验过。
TLS_SERVER_NAME = "www.dgw.com"


def env(name: str, required: bool = False, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"缺少环境变量 {name}")
    return v


def ssl_context() -> ssl.SSLContext:
    """按网关实际行为建 TLS 上下文。

    实测两个网关（电信 101.230.159.234 / 联通 140.206.44.234:8600）：
      - 只接受 TLS 1.0，握手协商结果 ECDHE-RSA-AES256-SHA；
        TLS 1.2 报 unsupported protocol，TLS 1.3 收到 alert 70
        （protocol_version）。wheel 里的 libssl.so.10 是 OpenSSL 1.1.0，
        印证了这个老协议栈。
      - 证书 CN=www.dgw.com，由 archforce 的 *.dgw.com 自签，
        正好是 wheel 里 tgw/cert/.ca.crt。

    所以：降到 TLS 1.0 + SECLEVEL=0，并按 www.dgw.com 做 SNI 校验。
    证书链是完整可验的，只是不能用 IP 验。

    CA 走 `client.resolve_ca_cert()`：环境变量 `TGW_CA_CERT` 优先，
    没设用仓内 `src/tgwpy/cert/dgw-ca.crt`（见 probe.py 同目录的证书说明）。
    """
    cert = resolve_ca_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1
    ctx.load_verify_locations(str(cert))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    # OpenSSL 3 默认安全级别 2 禁掉 TLS 1.0 的这套加密，必须降到 0
    ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    print(f"[tls] TLS1.0 + SECLEVEL=0, CA={cert.name}, SNI={TLS_SERVER_NAME}")
    return ctx


def now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class Recorder:
    """把收发报文按 JSONL 落盘，供离线分析。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fh = path.open("a", encoding="utf-8")
        self.seq = 0

    def log(self, direction: str, data: bytes, note: str = "") -> None:
        self.seq += 1
        rec = {
            "t": now(),
            "seq": self.seq,
            "dir": direction,
            "len": len(data),
            "hex": data.hex(),
            "note": note,
        }
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()
        arrow = "->" if direction == "send" else "<-"
        print(f"[{now()}] {arrow} {len(data):5d}B  {data[:48].hex()}"
              + (" ..." if len(data) > 48 else "") + f"  {note}")


def dump_printable(data: bytes) -> str:
    """尽力从二进制里捞可读片段，协议没逆出来前靠肉眼定位字段名。"""
    out, cur = [], []
    for b in data:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= 4:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 4:
        out.append("".join(cur))
    return " | ".join(out)


async def session(args) -> int:
    host = env("TGW_HOST", True)
    port = env("TGW_PORT", True)
    url = f"wss://{host}:{port}{URL_PATH}"

    rec = Recorder(LOGS / f"probe-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
    print(f"[{now()}] 连接 {url}")
    print(f"[{now()}] 日志 {rec.path.relative_to(ROOT)}")

    import websockets  # 延迟导入，缺依赖时给清楚的提示

    try:
        async with websockets.connect(
            url,
            ssl=ssl_context(),
            server_hostname=TLS_SERVER_NAME,
            open_timeout=20,
            max_size=None,
        ) as ws:
            print(f"[{now()}] 已连接  subprotocol={ws.subprotocol}")

            async def drain(seconds: float) -> None:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, str):
                        rec.log("recv", raw.encode(), "文本帧")
                        print("   可读片段:", dump_printable(raw.encode())[:200])
                    else:
                        rec.log("recv", raw)
                        print("   可读片段:", dump_printable(raw)[:200])

            if args.cmd == "handshake":
                # 服务端可能先发 greeting，也可能干等客户端先说话
                await drain(args.seconds)
            elif args.cmd == "listen":
                await drain(args.seconds)
            elif args.cmd == "send":
                data = Path(args.payload[1:]).read_bytes() if args.payload.startswith("@") \
                    else bytes.fromhex(args.payload)
                rec.log("send", data, "手工构造")
                await ws.send(data)
                await drain(args.seconds)

    except Exception as exc:  # noqa: BLE001 - 探测阶段任何异常都是信息
        print(f"[{now()}] 连接/会话失败：{type(exc).__name__}: {exc}")
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("handshake", "listen", "send"):
        p = sub.add_parser(name)
        p.add_argument("payload", nargs="?")
        p.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()
    return asyncio.run(session(args))


if __name__ == "__main__":
    sys.exit(main())
