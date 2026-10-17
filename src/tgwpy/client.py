"""tgw 公网查询客户端 —— 纯 Python 实现。

已实测跑通的部分：登录。

## 为什么裸 JSON、不加分帧

`[u32 总长][10 字节头][载荷]` 这套分帧来自 `amd::rqa::TcpProtocolDecode::OnMessage`，
那是 **TCP 通道**。公网走 WebSocket（`amd::mdga::WssConnectConn::Send`），
它把 JSON 字符串直接丢进 WebSocket 帧 —— WebSocket 本身已有消息边界，
多加前缀会导致服务端静默丢弃。这一点是踩过坑才确认的。

## 用法

    export TGW_HOST=101.230.159.234 TGW_PORT=8600
    export TGW_USER=... TGW_PASSWORD=...
    # CA 证书默认用仓内 src/tgwpy/cert/dgw-ca.crt，换证时 export TGW_CA_CERT=/path/to/ca.crt

    python src/tgwpy/client.py logon
    python src/tgwpy/client.py query A010010001 market_code=600519
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
#: 指定网关 CA 证书。设了就用它，没设用仓内那份（随公开仓发布）。
CA_CERT_ENV = "TGW_CA_CERT"
#: 随仓发布的根证书，厂商 tgw 1.0.9.2 wheel 里 `tgw/cert/.ca.crt` 的原样拷贝。
#: 有效期到 2031-04-20，来源与换证办法见同目录的 README.md。
BUNDLED_CA_CERT = Path(__file__).resolve().parent / "cert" / "dgw-ca.crt"
#: 两条通道路径。传输层现在只连其中一条，**默认是 PATH_MARKET**。
#: 依据：网关只允许一个会话，两条连接里第二次登录必然 `status=-98`，
#: 而 `/amd/dgw/dgw` 上 `ReqGetThirdInfo` 实测同样正常（42 条利润表）——
#: 一次登录供两族共用（真实网关实测：双连接必被拒）。
#: `PATH_QUERY_INFO` 保留：文档与测试引用它，将来若要回退仍有着落。
PATH_QUERY_INFO = "/amd/dgw/"      # ReqGetThirdInfo 等资讯/下载类
PATH_MARKET = "/amd/dgw/dgw"       # 默认通道；ReqGetKline / ReqGetSnapshot 在此
TLS_SERVER_NAME = "www.dgw.com"   # 证书按域名签，不按 IP
VERSION = "V4.3.0.260626-rc2.0-YHZQ"

sys.path.insert(0, str(ROOT / "src"))
from tgwpy.frames.query import make_task_id  # noqa: E402


def resolve_ca_cert() -> Path:
    """网关 CA 证书在哪，验一遍在不在。`probe.py` 也走这个函数。

    **不在就直接抛，不退回去换一张。** 少了它，报错是 TLS 握手失败／
    业务层一句 `login fail`，离真正的原因隔着好几层，排查一次要半天。
    """
    raw = os.environ.get(CA_CERT_ENV)
    path = Path(os.path.expanduser(raw)) if raw else BUNDLED_CA_CERT
    if not path.is_file():
        source = (f"{CA_CERT_ENV} 指向的就是它" if raw
                  else f"没设 {CA_CERT_ENV}，用的是仓内 src/tgwpy/cert/")
        raise FileNotFoundError(
            f"找不到网关 CA 证书 {path}（{source}）。"
            f"要指到别处就设 {CA_CERT_ENV}=/path/to/ca.crt，"
            f"换证的说明见 src/tgwpy/cert/README.md"
        )
    return path


def ssl_context() -> ssl.SSLContext:
    """网关只接受 TLS 1.0（实测 1.2 失败、1.3 收 alert 70）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1
    ctx.load_verify_locations(str(resolve_ca_cert()))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    # OpenSSL 3 默认安全级别 2 会禁掉 TLS 1.0 的这套加密
    ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    return ctx


def logon_request(user: str, password: str) -> dict:
    return {
        "headers": {"userName": user, "id": make_task_id(time.time())},
        "method": "ReqLogon",
        "params": {
            "Username": user,
            "Password": password,          # 明文，公网路径不做哈希
            "MacAddress": "00:11:22:33:44:55",
            "Version": VERSION,
            "ProcessId": os.getpid(),
            "ForceLogout": 1,
            "PushBandWidth": 0,
            "QueryBandWidth": 0,
        },
    }


def third_info_request(user: str, token: str, function_id: str,
                       item: list[tuple[str, str]], offset: int = 0,
                       count: int = 1000) -> dict:
    """已实测可用。三个要点都是踩坑换来的：

    - ``id`` 在 ``headers`` 里，不在顶层（SendThirdInfoRequest 开头的
      ``sub_7fe9f0(v2, a1, ...)`` 把第一个参数写进 headers）
    - ``function_id`` / ``offset`` / ``count`` / ``item`` / ``params``
      全在顶层，不在 ``params`` 对象里
    - 回包前有 4 字节前缀；``headers.id`` 是 32 位截断值
    """
    return {
        "headers": {
            "id": make_task_id(time.time()),
            "userName": user,
            "token": token,
        },
        "method": "ReqGetThirdInfo",
        "function_id": function_id,
        "offset": offset,
        "count": count,
        "item": [{"key": k, "value": v} for k, v in item],
        "params": {},
    }


class TgwClient:
    """一条 WebSocket 连接。登录后可用 token 发查询。

    默认连 ``PATH_MARKET``：网关只认一个会话，资讯与行情共用这一条。
    ``path`` 留成参数是为了能显式回到 ``PATH_QUERY_INFO``。
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 path: str = PATH_MARKET):
        self.host, self.port, self.path = host, port, path
        self.url = f"wss://{host}:{port}{path}"
        self.user, self.password = user, password
        self.token: str | None = None
        self._ws = None

    async def __aenter__(self) -> "TgwClient":
        import websockets

        self._ws = await websockets.connect(
            self.url, ssl=ssl_context(), server_hostname=TLS_SERVER_NAME,
            open_timeout=20, max_size=None,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def _recv_json(self, timeout: float) -> dict:
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        if isinstance(raw, str):
            raw = raw.encode()
        return json.loads(raw.decode("utf-8"))

    async def logon(self, timeout: float = 15.0) -> dict:
        """已实测稳定成功。返回完整响应，含 token。"""
        await self._ws.send(
            json.dumps(logon_request(self.user, self.password),
                       separators=(",", ":")).encode()
        )
        rsp = await self._recv_json(timeout)
        if rsp.get("status") != 0:
            raise RuntimeError(f"登录失败 status={rsp.get('status')} rsp={rsp}")
        self.token = rsp["headers"]["token"]
        return rsp

    async def query(self, function_id: str, params: dict,
                    timeout: float = 25.0) -> list[dict]:
        """发一条下载查询，返回解包后的记录列表。

        已实测跑通。响应格式：**1 字节前缀 + zstd 压缩流**，
        解压后是 JSON 信封，其中 ``data`` 字段还是**嵌套的 JSON 字符串**，
        要再 parse 一次才是记录数组。

        ``params`` 的值要带交易所后缀，如 ``market_code=002220.SZ``，
        否则服务端回 ``status=301010 未填写正确的market_code``。
        """
        if not self.token:
            raise RuntimeError("未登录")
        item = list(params.items())
        await self._ws.send(
            json.dumps(third_info_request(self.user, self.token, function_id, item),
                       separators=(",", ":")).encode()
        )

        # 分包：按 all_pack_num 收齐
        records: list[dict] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self._ws.recv(),
                                             timeout=max(0.2, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if isinstance(raw, str):
                raw = raw.encode()
            env = self._decode_frame(raw)
            if env is None:
                continue
            if env.get("status", 0) != 0:
                raise RuntimeError(f"{function_id} 失败 status={env['status']} "
                                   f"msg={env.get('msg')}")
            records.extend(self._extract_records(env))
            if self._is_last_pack(env):
                break
        return records

    async def send_market(self, method: str, params: dict,
                          timeout: float = 25.0) -> list[dict]:
        """发一条行情类请求（`ReqGetKline` / `ReqGetSnapshot` …）。

        与 :meth:`query` 的区别只剩报文形状：信封的 ``id`` 放在 ``headers``
        里、顶层不放，响应是 CSV 字符串数组而不是 JSON 记录。通道是同一条
        （默认路径），两族共用一次登录。
        """
        if not self.token:
            raise RuntimeError("未登录")
        from tgwpy.frames.market import (METHOD_KLINE, METHOD_SNAPSHOT,
                                      build_request, decode_kline_rows,
                                      decode_snapshot_rows)

        await self._ws.send(
            build_request(method, params, make_task_id(time.time()),
                          username=self.user, token=self.token))

        records: list[dict] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=max(0.2, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if isinstance(raw, str):
                raw = raw.encode()
            env = self._decode_frame(raw)
            if env is None:
                continue
            if env.get("status", 0) != 0:
                raise RuntimeError(f"{method} 失败 status={env['status']} "
                                   f"msg={env.get('msg')}")
            payload = self._extract_records(env)
            if method == METHOD_KLINE:
                records.extend(decode_kline_rows(payload))
            elif method == METHOD_SNAPSHOT:
                records.extend(decode_snapshot_rows(payload))
            else:
                records.extend(payload)
            if self._is_last_pack(env):
                break
        return records

    @staticmethod
    def _is_last_pack(env: dict) -> bool:
        """分包响应是否已经收齐。

        `all_pack_num` 缺省表示服务端没打算分包（单帧），算收齐。
        判据来自抓包：单日快照回 4 帧，`pack_num` 逐帧 1..4。
        **唯一判据**：本类的 :meth:`query` / :meth:`send_market` 与
        `wire.WssTransport` 的读帧循环都调它，分包语义只有这一处定义。
        """
        hdr = env.get("headers") or {}
        total, num = hdr.get("all_pack_num"), hdr.get("pack_num")
        return not total or num == total

    @staticmethod
    def _loads_loose(text: str) -> dict:
        """解出的内容末尾可能带填充/终止字节，用 raw_decode 只取前面那段 JSON。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            obj, _end = json.JSONDecoder().raw_decode(text.lstrip())
            return obj

    @staticmethod
    def _decode_frame(raw: bytes) -> dict | None:
        """1 字节前缀 + zstd 流 → JSON 信封。失败返回 None。"""
        import zstandard

        try:
            return TgwClient._loads_loose(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        for off in (1, 0, 2, 4):
            try:
                out = zstandard.ZstdDecompressor().decompress(
                    raw[off:], max_output_size=64 << 20)
                return TgwClient._loads_loose(out.decode("utf-8"))
            except Exception:  # noqa: BLE001 - 换下一个偏移再试
                continue
        return None

    @staticmethod
    def _extract_records(env: dict) -> list[dict]:
        """信封里的 data 是嵌套 JSON 字符串，剥出记录数组。"""
        data = env.get("data")
        if isinstance(data, str):
            try:
                data = TgwClient._loads_loose(data)
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict):
            body = data.get("body", data)
            if isinstance(body, dict):
                return body.get("data") or []
        return data or []


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        sys.exit(f"缺少环境变量 {name}")
    return v


async def _run_logon() -> int:
    async with TgwClient(_env("TGW_HOST"), _env("TGW_PORT"),
                         _env("TGW_USER"), _env("TGW_PASSWORD")) as c:
        rsp = await c.logon()
        print(json.dumps(rsp, ensure_ascii=False, indent=2)[:2000])
        print(f"\n登录成功，token = {c.token}")
    return 0


async def _run_query(fn: str, params: dict) -> int:
    async with TgwClient(_env("TGW_HOST"), _env("TGW_PORT"),
                         _env("TGW_USER"), _env("TGW_PASSWORD")) as c:
        await c.logon()
        print(f"token = {c.token}")
        rows = await c.query(fn, params)
        print(f"\n{fn} 返回 {len(rows)} 条记录")
        if rows:
            print(f"字段: {', '.join(rows[0])}")
            print(json.dumps(rows[:2], ensure_ascii=False, indent=2)[:2000])
    return 0


async def _run_kline(code: str, begin: int, end: int, period: str) -> int:
    """`market kline` 的命令行入口。已用抓包参数实测通。"""
    from tgwpy.frames.market import (METHOD_KLINE, PERIOD_TYPE,
                                      KlineRequest)

    req = KlineRequest(
        security_code=code.split(".")[0],
        market_type=101 if code.endswith(".SH") else 102,
        period_type=PERIOD_TYPE[period],
        begin_date=begin,
        end_date=end,
    )
    params = req.to_params()
    async with TgwClient(_env("TGW_HOST"), _env("TGW_PORT"),
                         _env("TGW_USER"), _env("TGW_PASSWORD"),
                         path=PATH_MARKET) as c:
        await c.logon()
        print(f"token = {c.token}")
        print(f"请求 {json.dumps(params, ensure_ascii=False)}")
        rows = await c.send_market(METHOD_KLINE, params)
        print(f"\n返回 {len(rows)} 条记录")
        if rows:
            print(json.dumps(rows[:3], ensure_ascii=False, indent=2)[:2000])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("logon")
    q = sub.add_parser("query")
    q.add_argument("function_id")
    q.add_argument("params", nargs="*", help="形如 market_code=600519")
    k = sub.add_parser("kline")
    k.add_argument("code", help="600519.SH")
    k.add_argument("--begin", type=int, default=20260420)
    k.add_argument("--end", type=int, default=20260424)
    k.add_argument("--period", default="day")
    args = ap.parse_args()

    if args.cmd == "logon":
        return asyncio.run(_run_logon())
    if args.cmd == "kline":
        return asyncio.run(_run_kline(args.code, args.begin, args.end, args.period))
    params = dict(p.split("=", 1) for p in args.params)
    return asyncio.run(_run_query(args.function_id, params))


if __name__ == "__main__":
    sys.exit(main())
