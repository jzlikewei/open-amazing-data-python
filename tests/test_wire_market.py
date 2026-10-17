"""传输层的单连接验收测试。

单连接是硬约束：网关只允许一个会话，两条连接里第二次登录必然
``status=-98``，实测四种拓扑只有「单连接走 ``/amd/dgw/dgw``、登录一次」
能通（传输栈实测：查出双连接设计是错的）。这几条测试钉住的就是那个拓扑。

四组判据：

1. **只有一条连接**：两族共用 `/amd/dgw/dgw`；`send()` / `send_market()`
   之后底层是同一个 socket；登录帧全程只发一次，传输层不自己登录
2. **生命周期按单连接收敛**：`close()` 只关那一条、且不停后台循环；
   重连后两族都走新连接；连着时 `reopen()` 不开第二条
3. **分包两族都收齐**：`headers.all_pack_num` 判据只有一处，4 帧喂进去
   回填的是合并后的全量，断链时半截响应按失败回填
4. **传输层不碰 CSV**：原始字符串行原样交给 compat 层

不碰网络：把 ``TgwClient`` 的建连与登录两步替换掉，读帧协程、分包收齐、
回填这几段走真代码。回填用的是真的
``tgwpy.compat.tgw_module.deliver_response``——``data`` 的形状顺带被
真实契约验了一遍，不是对着自己的假回调自说自话。登录也走真的
``tgw_module.Login()``：它同步等 `deliver_logon`，所以测试开一条线程跑它、
主线程投登录应答。

跑法（worktree 里没有 .venv，借主仓库的）：

    .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import pytest
import zstandard

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy.client import PATH_MARKET, PATH_QUERY_INFO, TgwClient  # noqa: E402
from tgwpy.compat import structs, tgw_module  # noqa: E402
from tgwpy.guard import RequestGate             # noqa: E402
from tgwpy.wire import WssTransport  # noqa: E402


# --- 假连接 -----------------------------------------------------------------


class FakeWs:
    """只实现 wire.py 用到的那几个方法：``send`` / ``recv`` / ``close``。

    ``recv()`` 轮询而不是挂死：测试线程要能随时投帧进来、也能随时调
    ``close()``，阻塞式等待会把后台事件循环一起卡住。
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.sent: list[bytes] = []
        self.closed = False
        self._lock = threading.Lock()
        self._inbox: deque[bytes] = deque()
        self._broken = threading.Event()

    def feed(self, raw: bytes) -> None:
        """投一帧进收信箱。测试线程调用，线程安全。"""
        with self._lock:
            self._inbox.append(raw)

    def break_link(self) -> None:
        """模拟链路被掐断。与 :meth:`close` 的区别是不走协程，
        断链后 `recv()` 直接抛错，测试线程当场就能看到效果。"""
        self.closed = True
        self._broken.set()

    async def send(self, payload) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self.sent.append(payload)

    async def recv(self):
        while True:
            with self._lock:
                if self._inbox:
                    return self._inbox.popleft()
            if self._broken.is_set():
                raise ConnectionResetError("假连接断了")
            await _sleep()

    async def close(self) -> None:
        self.break_link()


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.005)


# --- 帧构造 -----------------------------------------------------------------


def wire_frame(env: dict) -> bytes:
    """按线上真实格式造帧：1 字节前缀 + zstd 流（`TgwClient._decode_frame` 认这个）。"""
    return b"\x00" + zstandard.ZstdCompressor().compress(
        json.dumps(env, separators=(",", ":")).encode())


def response(task_id: int, rows: list, pack: int | None = None,
             total: int | None = None, status: int = 0) -> bytes:
    headers: dict[str, Any] = {"id": task_id}
    if pack is not None:
        headers["pack_num"] = pack
    if total is not None:
        headers["all_pack_num"] = total
    return wire_frame({"headers": headers, "status": status, "data": rows})


def logon_response(token: str = "tok-1", status: int = 0) -> bytes:
    """认证应答。``tag == "OnRspLogon"`` 是读帧循环认它的唯一依据。"""
    return wire_frame({"headers": {"tag": "OnRspLogon", "token": token},
                       "status": status, "msg": "" if status == 0 else "被拒"})


# 抓包拿到的 4 根日线原始 CSV（取自 tests/test_market.py 的 RAW_ROWS）
ROWS_1 = [
    "600519,101,20260420,1403000000,1413940000,1400000000,1410890000,"
    "364513900,512714604145000",
    "600519,101,20260421,1411000000,1419900000,1410000000,1412010000,"
    "217542700,307459519172000",
]
ROWS_2 = [
    "600519,101,20260422,1412000000,1418000000,1410000000,1415000000,"
    "250000000,360000000000000",
]
ROWS_3 = [
    "600519,101,20260423,1415000000,1422000000,1413000000,1420000000,"
    "260000000,370000000000000",
]
ROWS_4 = [
    "600519,101,20260424,1420000000,1462000000,1419000000,1458490000,"
    "554553100,800341099907000",
]
ALL_ROWS = ROWS_1 + ROWS_2 + ROWS_3 + ROWS_4


# --- 夹具 -------------------------------------------------------------------


class Recorder:
    """业务层回调的替身。签名 `(data, status)`，与 SPI 的 `OnResponse` 一致。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.done = threading.Event()

    def __call__(self, data, status) -> None:
        self.calls.append((data, status))
        self.done.set()

    def wait(self, timeout: float = 5.0) -> tuple[Any, Any]:
        assert self.done.wait(timeout), "传输层没有回调业务层"
        return self.calls[-1]


class QuietLog:
    """吞掉 compat 的日志，免得测试输出被 stderr 淹掉，同时留个可断言的入口。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def OnLog(self, level: int, message: str, length: int) -> None:  # noqa: N802
        self.messages.append(message)


@pytest.fixture
def transport(monkeypatch):
    """装一个不发网络请求的 WssTransport，并清干净 compat 的全局会话。"""
    sockets: list[FakeWs] = []
    #: 谁调过 `TgwClient.logon()`（自登录）。单连接设计下必须是空表——
    #: 登录只归 compat 的 `Login()`。
    logon_calls: list[str] = []

    async def fake_aenter(self):
        ws = FakeWs(self.url)
        sockets.append(ws)
        self._ws = ws
        return self

    async def fake_aexit(self, *exc):
        if self._ws is not None:
            await self._ws.close()

    async def fake_logon(self, timeout: float = 15.0):
        logon_calls.append(self.path)
        self.token = "tok-1"
        return {"status": 0, "headers": {"token": self.token}}

    monkeypatch.setattr(TgwClient, "__aenter__", fake_aenter)
    monkeypatch.setattr(TgwClient, "__aexit__", fake_aexit)
    monkeypatch.setattr(TgwClient, "logon", fake_logon)

    tgw_module.session().reset()
    tgw_module.session().set_log_spi(QuietLog())
    # 协议分发测试不该替生产的发包节奏买单：零间隔闸门。
    # 节流本身的行为在 tests/test_guard.py 里单独验。
    t = WssTransport("1.2.3.4", "8600", "user", "pwd",
                     gate=RequestGate(min_interval=0.0, backoff_base=0.0))
    t.sockets = sockets
    t.logon_calls = logon_calls
    tgw_module.set_transport(t)

    yield t

    tgw_module.session().reset()
    t.close()
    time.sleep(0.05)        # 让 close 的协程在循环里跑完，别留悬空任务
    t.shutdown()


def arm(task_id: int) -> Recorder:
    """在 compat 的任务表里登记一个任务，响应回来时回调它。"""
    rec = Recorder()
    tgw_module.session().task(task_id).callback = rec
    return rec


def wait_for(pred: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"{what}：等 {timeout}s 还没发生")


def sent_frames(transport) -> list[bytes]:
    """按发出顺序汇总所有连接上的帧。单连接下正常只有一条。"""
    return [f for ws in transport.sockets for f in ws.sent]


def sent_methods(transport) -> list[str]:
    """同 :func:`sent_frames`，只取 method。只能用于 compat 造的 JSON 帧流程。"""
    return [json.loads(f)["method"] for f in sent_frames(transport)]


def raw_frame(method: str) -> bytes:
    """造一个只有 method 的帧。统计 method 序列时用它，别塞裸字节。"""
    return json.dumps({"method": method}).encode()


def compat_login(transport, token: str = "tok-1") -> bool:
    """走 compat 的 ``Login()`` 登一次——这是本设计里唯一的登录入口。

    compat 的 ``Login`` 是同步的（等 ``logon_event``），所以测试开一条线程
    跑它，主线程等连接建起来再投认证应答。
    """
    cfg = tgw_module.Cfg()
    cfg.username, cfg.password = "user", "pwd"
    cfg.server_vip, cfg.server_port = "1.2.3.4", 8600
    result: list[bool] = []

    th = threading.Thread(
        target=lambda: result.append(
            tgw_module.Login(cfg, tgw_module.ApiMode.kInternetMode)),
        daemon=True)
    th.start()
    wait_for(lambda: transport.client._ws is not None, "Login 没建起连接")
    transport.client._ws.feed(logon_response(token))
    th.join(tgw_module.LOGON_TIMEOUT + 5)
    assert not th.is_alive(), "Login 等到超时还没返回"
    return result == [True]


def kline_cfg() -> structs.ReqKline:
    """业务层 `market_data.py:199-208` 逐字段赋值后的 `tgw.ReqKline`。"""
    cfg = structs.ReqKline()
    cfg.security_code = "600519"
    cfg.market_type = 101
    cfg.cyc_type = tgw_module.MDDatatype.kDayKline
    cfg.begin_date, cfg.end_date = 20260420, 20260424
    cfg.begin_time, cfg.end_time = 900, 1700
    cfg.cq_flag, cfg.auto_complete = 0, 1
    return cfg


# --- 判据 1：一条连接，两族共用 ----------------------------------------------


def test_transport_holds_one_client_on_the_market_path(transport):
    """传输层只持有一个 client，路径是 `/amd/dgw/dgw`。"""
    assert transport.client.url.endswith(PATH_MARKET)
    assert not hasattr(transport, "market_client"), "不该再有第二条连接"
    assert transport.closed is True
    assert transport.sockets == [], "构造完不该已经建连"


def test_tgw_client_default_path_is_the_market_path():
    """默认通道换成 `/amd/dgw/dgw`；`/amd/dgw/` 常量保留待回退。"""
    assert TgwClient("1.2.3.4", 8600, "u", "p").url == \
        "wss://1.2.3.4:8600/amd/dgw/dgw"
    assert PATH_QUERY_INFO == "/amd/dgw/"


def test_both_families_land_on_the_same_socket(transport):
    """`send()` 和 `send_market()` 之后，底层 `_ws` 是同一个对象。"""
    transport.send(b"info-bytes")
    wait_for(lambda: transport.client._ws is not None
             and transport.client._ws.sent, "连接没建起来")
    first_ws = transport.client._ws

    transport.send_market(b"market-bytes")
    wait_for(lambda: len(first_ws.sent) == 2, "行情帧没发到同一条连接")

    assert transport.client._ws is first_ws
    assert first_ws.sent == [b"info-bytes", b"market-bytes"]
    assert len(transport.sockets) == 1, "两族共用一条连接，不该建出第二条"


def test_logon_frame_is_sent_exactly_once(transport):
    """完整流程跑一遍，`ReqLogon` 全程只发出去一次。

    第二次登录就是那个 `status=-98`：网关只允许一个会话。
    """
    assert compat_login(transport), "compat 的 Login 应该成功"
    assert sent_methods(transport) == ["ReqLogon"]

    # 登录之后两族各打一次，登录帧不能再多
    transport.send(raw_frame("ReqGetThirdInfo"))
    transport.send_market(raw_frame("ReqGetKline"))
    wait_for(lambda: len(transport.sockets[0].sent) == 3, "查询帧没发出去")

    assert sent_methods(transport) == ["ReqLogon", "ReqGetThirdInfo", "ReqGetKline"]
    assert len(transport.sockets) == 1
    assert transport.logon_calls == [], "传输层不该替 compat 登录"


def test_send_market_never_logs_in_by_itself(transport):
    """没有 compat 的 `Login()` 时，`send_market()` 不自登录。

    老设计里行情连接按需自登录，真实网关上一次就是 -98。
    """
    transport.send_market(b"market-bytes")
    wait_for(lambda: transport.client._ws is not None
             and transport.client._ws.sent, "行情帧没发出去")

    assert transport.logon_calls == [], "行情发送触发了自登录"
    assert transport.client.token is None
    assert tgw_module.session().logged_in is False


# --- 判据 2：生命周期按单连接收敛 -------------------------------------------


def test_close_closes_the_only_connection_and_keeps_the_loop(transport):
    transport.send(b"i")
    transport.send_market(b"m")
    wait_for(lambda: len(transport.sockets) == 1, "连接没建起来")
    ws = transport.sockets[0]

    transport.close()
    wait_for(lambda: ws.closed, "连接没关掉")

    assert transport.closed is True
    assert transport.client._ws is None
    # 关键约束：close() 不能停后台循环，否则业务层 Close() 之后的
    # Login() 再也连不上（实测报「未登录」前置检查失败）
    assert transport._loop is not None and not transport._loop.is_closed()


def test_send_after_close_reopens_once_for_both_families(transport):
    """close() 之后重连：两族都走新连接，不会各建一条。"""
    transport.send(b"i1")
    transport.send_market(b"m1")
    wait_for(lambda: len(transport.sockets) == 1, "连接没建起来")
    old_ws = transport.sockets[0]

    transport.close()
    wait_for(lambda: old_ws.closed, "连接没关掉")

    transport.send_market(b"m2")
    wait_for(lambda: transport.client._ws is not None
             and transport.client._ws.sent == [b"m2"], "行情没重连")
    new_ws = transport.client._ws
    assert new_ws is not old_ws

    transport.send(b"i2")
    wait_for(lambda: new_ws.sent == [b"m2", b"i2"], "资讯没走新连接")

    assert transport.client._ws is new_ws
    assert len(transport.sockets) == 2, "重连只该多一条连接"


def test_reopen_while_connected_does_not_open_a_second_one(transport):
    """连着的时候 `reopen()` 什么都不做。第二条连接就是第二次会话。"""
    transport.send(b"i")
    wait_for(lambda: transport.client._ws is not None
             and transport.client._ws.sent, "连接没建起来")
    ws = transport.client._ws

    assert transport.reopen() is True

    assert transport.client._ws is ws
    assert len(transport.sockets) == 1
    assert ws.sent == [b"i"]


def test_reconnect_does_not_duplicate_the_pump(transport):
    """重连只换一个读帧协程；旧的那个必须已经取消，否则两个协程抢 recv()。"""
    transport.send(b"i1")
    wait_for(lambda: transport.pump is not None, "读帧协程没起来")
    first_pump = transport.pump

    transport.close()
    transport.send(b"i2")
    wait_for(lambda: transport.pump is not None
             and transport.pump is not first_pump, "读帧协程没换")

    assert first_pump.cancelled() or first_pump.done()


# --- 判据 3：分包两族都收齐 -------------------------------------------------


def test_multi_frame_waits_for_all_packs(transport):
    """4 帧喂完才回填，回填的是合并后的 4 帧全量，不是最后一帧。"""
    rec = arm(4242)
    transport.send_market(b"q")
    ws = transport.client._ws

    for i, rows in enumerate((ROWS_1, ROWS_2, ROWS_3), start=1):
        ws.feed(response(4242, rows, pack=i, total=4))

    assert not rec.done.wait(0.3), "3/4 帧就回填了，后一帧会认不到任务"
    assert tgw_module.session().pending_task_ids() == (4242,)
    assert rec.calls == []

    ws.feed(response(4242, ROWS_4, pack=4, total=4))
    data, status = rec.wait()

    assert status == 0
    assert data == ALL_ROWS
    assert len(data) == len(ALL_ROWS) == 5, "4 帧必须合并成 5 行，不能只剩最后一帧"
    assert tgw_module.session().pending_task_ids() == ()


def test_info_multi_frame_also_waits_for_all_packs(transport):
    """资讯族现在也走同一个判据：带 `all_pack_num` 就收齐。

    老断言「资讯逐帧到就回填」描述的是双连接设计，那条连接现在已经不
    存在，收齐才是对的——第一包就出表会把后面的记录全丢掉。
    """
    rec = arm(41)
    transport.send(b"q")
    ws = transport.client._ws
    ws.feed(response(41, [{"MARKET_CODE": "600519"}], pack=1, total=2))

    assert not rec.done.wait(0.3), "1/2 帧就回填了，后一帧会认不到任务"
    assert rec.calls == []

    ws.feed(response(41, [{"MARKET_CODE": "000858"}], pack=2, total=2))
    data, status = rec.wait()

    assert status == 0
    assert data == [{"MARKET_CODE": "600519"}, {"MARKET_CODE": "000858"}]
    assert tgw_module.session().pending_task_ids() == ()


def test_single_frame_needs_no_pack_headers(transport):
    """没有 all_pack_num 就是没分包，两族都立刻回填，不能傻等。

    资讯 `A010050002` 实测就是这一形态（单帧无 pack 头）。
    """
    rec_info, rec_market = arm(11), arm(12)
    transport.send(b"q1")
    transport.send_market(b"q2")
    ws = transport.client._ws
    ws.feed(response(11, [{"MARKET_CODE": "600519"}]))
    ws.feed(response(12, ROWS_1))

    data, status = rec_info.wait()
    assert (data, status) == ([{"MARKET_CODE": "600519"}], 0)
    data, status = rec_market.wait()
    assert (data, status) == (ROWS_1, 0)


def test_error_frame_fails_the_task(transport):
    """半途来的错误帧：暂存的分包作废，任务按失败回填。"""
    rec = arm(12)
    transport.send_market(b"q")
    ws = transport.client._ws
    ws.feed(response(12, ROWS_1, pack=1, total=2))
    wait_for(lambda: 12 in transport.pending, "首帧没收下")

    ws.feed(wire_frame({"headers": {"id": 12}, "status": -89, "msg": "kHqsError"}))

    data, status = rec.wait()
    assert data is None
    assert "kHqsError" in status
    assert transport.pending == {}


def test_broken_connection_fails_partial_response(transport):
    """差几包时断链，半截响应要有个交代，否则任务永远挂在表里。"""
    rec = arm(13)
    transport.send_market(b"q")
    ws = transport.client._ws
    ws.feed(response(13, ROWS_1, pack=1, total=4))
    wait_for(lambda: 13 in transport.pending, "首帧没收下")

    ws.break_link()        # 断链

    data, status = rec.wait()
    assert data is None
    assert "连接中断" in status
    assert transport.pending == {}
    assert tgw_module.session().pending_task_ids() == ()


def test_broken_connection_reopens_on_the_next_query(transport):
    """断链之后传输层自认连接已作废，下一条查询重连。"""
    transport.send_market(b"m1")
    wait_for(lambda: transport.client._ws is not None, "连接没建起来")
    ws = transport.client._ws
    ws.break_link()
    wait_for(lambda: transport.closed is True, "断链没被感知")

    transport.send_market(b"m2")
    wait_for(lambda: transport.client._ws is not None
             and transport.client._ws.sent == [b"m2"], "没重连")

    assert transport.client._ws is not ws
    assert len(transport.sockets) == 2


# --- 判据 4：CSV 与签名 -----------------------------------------------------


def test_transport_passes_csv_rows_through_untouched(transport):
    """`data` 到 compat 手里必须还是原始字符串行，一行都没拆过。"""
    rec = arm(21)
    transport.send_market(b"q")
    transport.client._ws.feed(response(21, ROWS_1))

    data, _status = rec.wait()
    assert data == ROWS_1
    assert all(isinstance(row, str) for row in data)
    assert not any(isinstance(row, (dict, list)) for row in data)
    # 传输层没碰过内容：列数、逗号都原样
    assert all(row.count(",") == 8 for row in data)


def test_send_market_signature_is_the_contract_one():
    """compat 层按这个签名调，签名变了就对不上。"""
    sig = inspect.signature(WssTransport.send_market)
    assert list(sig.parameters) == ["self", "payload"]
    assert sig.parameters["payload"].annotation == "bytes"
    assert sig.return_annotation == "None"


# --- 判据 5：同一条连接上行情→资讯→行情连打三次 ------------------------------


def test_three_calls_in_a_row_over_one_connection(transport):
    """连打三次，响应各回各的任务，全程一条连接、一次登录。

    真实网关上这个序列可行，这里做回归。task_id 从**真正发出去的帧**里读，
    顺带钉住「响应按线上的 id 分发」这条链。
    """
    assert compat_login(transport)
    ws = transport.client._ws

    kline_rec, info_rec, kline_rec2 = Recorder(), Recorder(), Recorder()

    def sent_id() -> int:
        """等新帧落到线上，从它里面读 task_id——响应按线上的 id 分发。"""
        seen = len(sent_frames(transport))
        wait_for(lambda: len(sent_frames(transport)) > seen, "请求帧没发出去")
        return json.loads(sent_frames(transport)[-1])["headers"]["id"]

    tgw_module.QueryKline(kline_cfg(), query_spi=kline_rec)
    ws.feed(response(sent_id(), ROWS_1))
    data, status = kline_rec.wait()
    assert status == 0
    assert len(data) == 2, "K 线两行"

    info_id = tgw_module.GetTaskID()
    tgw_module.SetThirdInfoParam(info_id, "function_id", "A010050002")
    tgw_module.SetThirdInfoParam(info_id, "market_code", "600519.SH")
    tgw_module.QueryThirdInfo(info_id, query_spi=info_rec)
    assert sent_id() == info_id
    ws.feed(response(info_id, [{"MARKET_CODE": "600519"}]))
    data, status = info_rec.wait()
    assert status == 0
    # 资讯族回调的也是 DataFrame，跟 K 线一个整形出口
    assert data["MARKET_CODE"].tolist() == ["600519"]

    tgw_module.QueryKline(kline_cfg(), query_spi=kline_rec2)
    ws.feed(response(sent_id(), ROWS_2))
    data, status = kline_rec2.wait()
    assert status == 0
    assert len(data) == 1, "第二根 K 线一行"

    # 三次都在同一条连接上，登录帧仍然只有一条
    assert transport.client._ws is ws
    assert len(transport.sockets) == 1
    assert sent_methods(transport) == [
        "ReqLogon", "ReqGetKline", "ReqGetThirdInfo", "ReqGetKline"]
    assert tgw_module.session().pending_task_ids() == ()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_logon_token_reaches_the_compat_session(transport):
    """认证应答里的 token 必须落到 compat 会话上。

    **这条曾经是断的**：网关把 token 放在认证应答的 `headers.token` 里，
    但只有 `TgwClient` 落到了 `client.token`，compat 的 `_SESSION.token`
    一直是空串。而 compat 拼信封（`encode_third_info_request` /
    `build_request`）读的正是 `_SESSION.token` —— 结果每条查询都带空
    token 出去，服务端回 `status=-87`（kDbAndCodeTableNoCode）。

    这条断言在真实网关上验过：修之前 kline 直接 -87，修之后返回 5 行。
    """
    from tgwpy.compat import tgw_module as tm

    transport._on_logon({"tag": "OnRspLogon", "id": 0, "token": "TOKEN-XYZ"}, 0, {})

    assert transport.client.token == "TOKEN-XYZ"
    assert tm._SESSION.token == "TOKEN-XYZ", "token 没进 compat 会话"
    assert tm._SESSION.logged_in is True


def test_logon_failure_does_not_pollute_the_session_token(transport):
    """认证失败不能把 token 写成脏值。"""
    from tgwpy.compat import tgw_module as tm

    tm._SESSION.reset()
    transport._on_logon({"tag": "OnRspLogon", "id": 0, "token": "SHOULD-NOT-STICK"},
                        -1, {"msg": "登录失败"})
    assert tm._SESSION.logged_in is False
    assert tm._SESSION.token != "SHOULD-NOT-STICK"


# --- U3：空结果与暂时性错误不进停手计数 ------------------------------------


def _error_frame(task_id: int, status: int, msg: str = "") -> bytes:
    env: dict[str, Any] = {"headers": {"id": task_id}, "status": status, "data": ""}
    if msg:
        env["msg"] = msg
    return wire_frame(env)


@pytest.mark.parametrize("status,counted", [(-100, False), (-89, False), (-82, True)])
def test_U3_只有真失败才进停手计数(transport, status, counted):
    """-100 是「没数据」，-89 由调用方重试；两者都不该把整轮推向退出码 5。"""
    rec = arm(77)
    transport.send(b"q")
    transport.gate.record_failure("之前的一次失败")
    before = transport.gate.failures
    transport.client._ws.feed(_error_frame(77, status))
    _data, text = rec.wait()
    assert f"status={status}" in text
    if counted:
        assert transport.gate.failures == before + 1
    elif status == -100:
        assert transport.gate.failures == 0, "空结果说明后端活着，清零"
    else:
        assert transport.gate.failures == before, "暂时性错误不加也不清"


def test_U3_有msg时错误文本仍带status(transport):
    """业务层只拿到这一串字，丢了码就分不出暂时性错误。"""
    rec = arm(78)
    transport.send(b"q")
    transport.client._ws.feed(_error_frame(78, -89, msg="行情服务错误"))
    _data, text = rec.wait()
    assert "行情服务错误" in text and "status=-89" in text
