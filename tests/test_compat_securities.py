"""代码表查询（`QuerySecuritiesInfo` / `QueryETFInfo`）在 tgw 替身上的验收测试。

**这份测试钉的是一件最容易复发的事：线上 method 名不是 SDK 方法名。**

`QuerySecuritiesInfo` / `QueryETFInfo` 是 `libtgw_python3*.so` 的 C++ 包装名，
网关上根本没有这两个字符串。MITM 抓参照 SDK 的真实流量确认线上发的是
`ReqGetCodeTableList` / `ReqGetETFCodeTableList`；照 SDK 名发**静默失败**——
网关不回错误码也不断链，看起来像网络问题。

**判据是「`-82` 被如实上报」，不是「取到数据」。** 服务端在 8600 端口上恒定
回 `status=-82`（`kFlowOverLimit`），官方 SDK 发的同一份报文也回 `-82`，
7 种 `security_type` 逐字节相同（抓包实证）。
参数空间已穷举：带代码 / 不带代码 / 换市场码 / 换 method，一律 `-82`。
所以请求构造正确 + `-82` 变成可见失败，就是这两个接口此刻能做的全部。

不碰网络：`send` 是个记账器，回包由测试自己调 `deliver_response` 注入。

跑法（worktree 里没有 .venv，借主仓库的）：

    .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy.compat import structs  # noqa: E402
from tgwpy.compat import tgw_module as tm  # noqa: E402

# --- 抓包原文（MITM 实证）--------------------------------------------------
#
# **原样照抄，不做归一化。** 键序、管道符、空代码位都是判据的一部分。

#: `python3 cli.py base code_list --type <任意值>` 实际发出（【抓包实证】）
CAPTURED_CODE_TABLE_REQUEST = {
    "headers": {"id": 1, "userName": "<user>", "token": "<token>"},
    "method": "ReqGetCodeTableList",
    "params": {"Security": "|102"},
}
CAPTURED_CODE_TABLE_RESPONSE = {
    "headers": {"id": 1, "tag": "109", "code_num": 0},
    "status": -82,
    "data": [],
}

#: `python3 cli.py other etf_pcf --codes 510050.SH` 实际发出（【抓包实证】）
CAPTURED_ETF_REQUEST = {
    "headers": {"id": 1, "userName": "<user>", "token": "<token>"},
    "method": "ReqGetETFCodeTableList",
    "params": {"Security": "510050|101"},
}
CAPTURED_ETF_RESPONSE = {
    "headers": {"id": 1, "tag": "111", "code_num": 0},
    "status": -82,
    "data": [],
}

#: 传输层收到 `status != 0` 的信封后交给 compat 的形状。`wire.py` 的
#: `_pump_loop` 走的就是这条：业务错误回 `data=None` + 字符串 status。
FLOW_OVER_LIMIT_STATUS = f"status={CAPTURED_CODE_TABLE_RESPONSE['status']}"


# --- 测试替身：记账传输层 + 已登录会话 ------------------------------------


class _FakeTransport:
    """只记账，不联网。`send` 是资讯通道的硬契约，`send_market` 是行情通道的。"""

    def __init__(self) -> None:
        self.info: list[bytes] = []
        self.market: list[bytes] = []
        self.raise_on_send: Exception | None = None

    def send(self, payload: bytes) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.info.append(payload)

    def send_market(self, payload: bytes) -> None:
        self.market.append(payload)


@pytest.fixture
def wire(monkeypatch):
    """装好一个已登录的会话，产出记账传输层。测完还原全局状态。"""
    tm._SESSION.reset()
    transport = _FakeTransport()
    tm.set_transport(transport)
    tm._SESSION.logged_in = True
    tm._SESSION.username = "<user>"
    tm._SESSION.token = "<token>"
    tm._SESSION.api_mode = tm.ApiMode.kInternetMode
    yield transport
    tm._SESSION.reset()


def _sec_item(security_code: str = "", market=tm.MarketType.kSZSE) -> structs.SubCodeTableItem:
    """业务层 `base_data.py:103-105` 逐字段赋值后的 `tgw.SubCodeTableItem`。"""
    item = structs.SubCodeTableItem()
    item.security_code = security_code
    item.market = market
    return item


def _sent(wire) -> dict:
    """解析最后一条发出去的报文。"""
    assert wire.info, "资讯通道没有收到任何报文"
    return json.loads(wire.info[-1])


# --- 同步形态的驱动器：扮演传输层，按 task_id 逐个回包 ---------------------
#
# 同步查询自己就在等自己，同一个线程里没法既发又收，所以回包必须由另一条
# 线程注入。这里不假设「同时挂着一个任务」——`QueryETFInfo` 的同步形态是
# **串行**的（发一个等一个再发下一个），驱动器按 task_id 逐个应答即可，
# N 个 code 与 1 个 code 走同一条路径。


class _Gateway:
    """后台应答器：主线程调 `run(fn)`，它在另一条线程里把查询结果送回去。

    `reply(task_id)` 返回 `(rows, status)`。返回 `status` 非 None 时按失败
    投递（`data=None`），跟 `wire.py::_pump_loop` 的业务错误分支同形。
    """

    def __init__(self, reply) -> None:
        self._reply = reply
        self._answer: dict[int, tuple] = {}

    def run(self, fn, *args, timeout: float = 5.0, **kwargs):
        """在后台线程调 `fn`，本线程负责应答，返回 `(返回值, 抛出的异常)`。"""
        box: dict[str, object] = {}
        answered: set[int] = set()

        def _call():
            try:
                box["result"] = fn(*args, **kwargs)
            except Exception as exc:      # noqa: BLE001 - 这里就是要看抛什么
                box["error"] = exc

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        deadline = time.time() + timeout
        while worker.is_alive() and time.time() < deadline:
            for task_id in tm.pending_task_ids():
                if task_id in answered:
                    continue
                answered.add(task_id)
                rows, status = self._reply(task_id)
                self._answer[task_id] = (rows, status)
                tm.deliver_response(task_id, rows, status)
            time.sleep(0.005)
        worker.join(timeout=timeout)
        assert not worker.is_alive(), f"同步查询没有返回：{box.get('error')!r}"
        return box.get("result"), box.get("error")


# --- 判据 1：请求字节与抓包原文逐字段相等 ----------------------------------


def test_securities_info_wire_matches_capture_field_by_field(wire):
    """`QuerySecuritiesInfo` 的报文必须与抓包原文逐字段相等。"""
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)

    assert len(wire.info) == 1
    got = _sent(wire)
    want = CAPTURED_CODE_TABLE_REQUEST
    assert got["method"] == want["method"]
    assert got["params"] == want["params"]
    assert set(got) == set(want), f"信封键集不符：{sorted(got)} != {sorted(want)}"
    assert set(got["headers"]) == set(want["headers"])


def test_etf_info_wire_matches_capture_field_by_field(wire):
    """`QueryETFInfo` 的报文必须与抓包原文逐字段相等。"""
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: None)

    assert len(wire.info) == 1
    got = _sent(wire)
    want = CAPTURED_ETF_REQUEST
    assert got["method"] == want["method"]
    assert got["params"] == want["params"]
    assert set(got) == set(want)
    assert set(got["headers"]) == set(want["headers"])


def test_wire_carries_id_in_headers_with_session_credentials(wire):
    """`id` 在 `headers` 里（顶层不放），并带上会话凭据。

    `headers.id` 是运行时任务号，只断言它等于登记进任务表的那个——
    同一个号必须同时出现在两处，否则 `deliver_response` 认领不到。
    """
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    payload = _sent(wire)

    assert "id" not in payload
    assert payload["headers"]["id"] == tm.pending_task_ids()[0]
    assert payload["headers"]["userName"] == "<user>"
    assert payload["headers"]["token"] == "<token>"


def test_params_contain_exactly_one_business_key(wire):
    """`params` 里只有 `Security` 一个键，不夹带别的。"""
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    assert list(_sent(wire)["params"]) == ["Security"]


# --- 判据 2：线上 method 名钉死，防止后人照 SDK 名改回去 -------------------


def test_wire_method_is_not_the_sdk_method_name(wire):
    """**线上 method 是 `ReqGetCodeTableList`，不是 `QuerySecuritiesInfo`。**

    `QuerySecuritiesInfo` 是 `libtgw_python3*.so` 的 C++ 包装名，网关上没有这个
    字符串。照 SDK 名发会静默失败——不回错误码、不断链，看起来像网络问题。
    这条测试就是为了让「改回 SDK 名」这个动作当场红。
    """
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    method = _sent(wire)["method"]

    assert method == "ReqGetCodeTableList"
    assert "QuerySecuritiesInfo" not in method
    # 报文里任何一个位置都不能出现 SDK 名
    assert b"QuerySecuritiesInfo" not in wire.info[0]


def test_etf_wire_method_is_not_the_sdk_method_name(wire):
    """ETF 侧同理：线上是 `ReqGetETFCodeTableList`。

    注意别照成 `QueryETFCodeTableList`——SDK 侧的包装名是
    `QueryETFInfo`（少一个 `CodeTable`），三个名字两两不同，照哪个都会错。
    """
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: None)
    method = _sent(wire)["method"]

    assert method == "ReqGetETFCodeTableList"
    assert "QueryETF" not in method
    assert b"QueryETFInfo" not in wire.info[0]
    assert b"QueryETFCodeTableList" not in wire.info[0]


# --- 判据 3：`Security` 的拼装规则 -----------------------------------------


def test_security_param_is_code_pipe_market_code(wire):
    """`Security` 的格式是 `<代码>|<市场码>`，管道符分隔。"""
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: None)
    assert _sent(wire)["params"]["Security"] == "510050|101"


def test_empty_security_code_leaves_leading_pipe(wire):
    """代码为空时拼出 `|102`——业务层取全量代码表就是这么发的。"""
    tm.QuerySecuritiesInfo(_sec_item("", tm.MarketType.kSZSE),
                           query_spi=lambda d, s: None)
    assert _sent(wire)["params"]["Security"] == "|102"


def test_market_code_uses_wire_values_not_enum_ordinals(wire):
    """市场码取 `MarketType` 的**线上值**（SSE=101 / SZSE=102 / NEEQ=2）。

    不是 `_enums_stub` 的占位序号（那一套 kSZSE=8 / kSSE=7）。抓包里
    `|102` 正是 `kSZSE`——两套编码在这里数值一致，是这条规则能成立的前提。
    """
    seen = []
    for market in (tm.MarketType.kSSE, tm.MarketType.kSZSE, tm.MarketType.kNEEQ):
        seen.append(int(market))
        tm.QuerySecuritiesInfo(_sec_item("", market), query_spi=lambda d, s: None)
    got = [json.loads(p)["params"]["Security"] for p in wire.info]

    assert seen == [101, 102, 2]
    assert got == ["|101", "|102", "|2"]


def test_none_market_is_a_visible_failure_not_a_silent_pipe_zero(wire):
    """`kNone(0)` 不是合法市场码：发 `|0` 只会换一个看不懂的 `-82`。"""
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item("", tm.MarketType.kNone),
                           query_spi=lambda d, s: seen.append((d, s)))

    assert wire.info == [], "kNone 不该发到线上"
    assert len(seen) == 1 and seen[0][0] is None
    assert isinstance(seen[0][1], str) and "请求构造失败" in seen[0][1]


# --- 判据 4：走资讯通道，不走行情通道 -------------------------------------


def test_code_table_queries_go_to_info_channel_not_market(wire):
    """这两个接口走资讯通道 `send`（`/amd/dgw/`），不是 `send_market`。

    抓包里它们在 `/cmd/dgw/dgw{N}_query` 上（抓包实证
    §2.1），与 K 线 / 快照那条行情通道是两条连接。发错通道等于发到没人监听的
    连接上，连 `-82` 都收不到。
    """
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: None)

    assert len(wire.info) == 2
    assert wire.market == [], "代码表查询不该走行情通道"


# --- 判据 5：`-82` 是可见、可上报的结果，不是异常穿透 ----------------------


def test_flow_over_limit_response_is_reported_with_context(wire):
    """服务端回 `-82` 时，替身把失败**说出来**，并解释这个码名不副实。

    登录回包里 `used_week_flow=743216632 / total_control=1000000000`，
    额度只用了 74%（抓包实证）——把
    「周流量耗尽」原样报上去会误导人去查配额。
    """
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: seen.append((d, s)))
    task_id = tm.pending_task_ids()[0]

    # 传输层收到 `status != 0` 的信封：data=None + 字符串 status
    tm.deliver_response(task_id, None, FLOW_OVER_LIMIT_STATUS)

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str)
    assert "-82" in status
    assert "kFlowOverLimit" in status
    assert "A010010001" in status, "要指出证券基础信息的替代路径"


def test_flow_over_limit_does_not_leak_a_task(wire):
    """`-82` 也要让任务出表，否则任务表永远不减。"""
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    tm.deliver_response(tm.pending_task_ids()[0], None, FLOW_OVER_LIMIT_STATUS)
    assert tm.pending_task_ids() == ()


def test_successful_response_yields_dataframe(wire):
    """`status=0` 时出 DataFrame，列集由服务端给，不钉死。

    取不到数据时钉一列集就是编数据。业务层要读的列
    （`security_code` / `security_type` / `code_market` / `english_name`）
    从未实测到返回样例，这里只验形状与行数。
    """
    rows = [
        {"security_code": "600519", "security_type": "01001",
         "code_market": 101, "english_name": "                    "},
        {"security_code": "000001", "security_type": "01001",
         "code_market": 102, "english_name": "                    "},
    ]
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: seen.append((d, s)))
    tm.deliver_response(tm.pending_task_ids()[0], rows, None)

    data, status = seen[0]
    assert status is None
    assert data.__class__.__name__ == "DataFrame"
    assert len(data) == 2
    assert data["security_code"].tolist() == ["600519", "000001"]


def test_return_df_format_false_gives_record_list(wire):
    """`return_df_format=False` 出记录列表，官方中间层也是这个分岔。"""
    rows = [{"security_code": "600519", "security_type": "01001"}]
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item(), return_df_format=False,
                           query_spi=lambda d, s: seen.append((d, s)))
    tm.deliver_response(tm.pending_task_ids()[0], rows, None)

    data, _ = seen[0]
    assert isinstance(data, list) and data == rows


def test_empty_response_gives_empty_dataframe(wire):
    """空数据给空表，不给缺列的空壳。"""
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: seen.append((d, s)))
    tm.deliver_response(tm.pending_task_ids()[0], [], None)

    data, _ = seen[0]
    assert data.__class__.__name__ == "DataFrame"
    assert len(data) == 0


# --- 判据 6：同步形态（query_spi=None）返回 (None, 文案) 或抛 --------------
#
# `base_data.py` 拿到 `(code_table_df, error)` 后**丢掉 error**，紧接着
# `code_table_df['security_code']`（第 106–111 行）。回 `None` 会在几十行
# 之外炸成 `TypeError: 'NoneType' object is not subscriptable`，看不出是谁
# 失败——模块文档「两条硬规则」第 2 条据此定：同步形态宁可抛。


def test_sync_flow_over_limit_raises_named_error(wire):
    """同步形态拿到 `-82`：抛 `NotConnected`，文案带接口名与 `-82`。"""
    result, err = _Gateway(
        lambda _tid: (None, FLOW_OVER_LIMIT_STATUS)
    ).run(tm.QuerySecuritiesInfo, _sec_item())

    assert result is None, "失败路径不该返回半截结果"
    assert isinstance(err, tm.NotConnected)
    assert "QuerySecuritiesInfo" in str(err)
    assert "ReqGetCodeTableList" in str(err), "文案要带线上 method 名"
    assert "-82" in str(err)


def test_sync_flow_over_limit_does_not_leak_a_task(wire):
    """同步形态抛异常后任务也要出表，否则下一次的 task_id 会撞。"""
    _Gateway(lambda _tid: (None, FLOW_OVER_LIMIT_STATUS)).run(
        tm.QuerySecuritiesInfo, _sec_item())
    assert tm.pending_task_ids() == ()


def test_sync_success_returns_dataframe(wire):
    """同步形态成功时回 `(DataFrame, None)`，不抛。"""
    rows = [{"security_code": "600519", "security_type": "01001"}]
    result, err = _Gateway(lambda _tid: (rows, None)).run(
        tm.QuerySecuritiesInfo, _sec_item())

    assert err is None
    assert result[1] is None
    frame = result[0]
    assert frame.__class__.__name__ == "DataFrame"
    assert len(frame) == 1
    assert frame["security_code"][0] == "600519"


# --- 判据 7：三条失败路径（未登录 / 无 transport / 发送异常）----------------


def test_async_not_logged_in_calls_back_with_error(wire):
    """未登录：必须回调（业务层卡在 `wait_event.wait()` 上），status 是字符串。"""
    tm._SESSION.logged_in = False
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str) and "未登录" in status
    assert wire.info == [], "未登录时不该发报文"


def test_async_no_transport_calls_back_with_error(wire):
    """没接传输层：同样走回调，`error_list` 如实收到。"""
    tm._SESSION.set_transport(None)
    seen: list = []
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str) and "传输层" in status


def test_async_send_exception_calls_back_with_error(wire):
    """发送失败必须变成可见失败——抛出去会让业务层永远醒不过来。"""
    wire.raise_on_send = OSError("连接被对端重置")
    seen: list = []
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str)
    assert "发送失败" in status and "连接被对端重置" in status


def test_sync_not_logged_in_raises(wire):
    """同步形态未登录：抛 `NotConnected`，不返回空数据冒充成功。"""
    tm._SESSION.logged_in = False
    with pytest.raises(tm.NotConnected) as exc:
        tm.QuerySecuritiesInfo(_sec_item())
    assert "未登录" in str(exc.value)


def test_sync_no_transport_raises(wire):
    tm._SESSION.set_transport(None)
    with pytest.raises(tm.NotConnected) as exc:
        tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)])
    assert "传输层" in str(exc.value)


def test_sync_send_exception_raises(wire):
    wire.raise_on_send = RuntimeError("传输层内部炸了")
    with pytest.raises(tm.NotConnected) as exc:
        tm.QuerySecuritiesInfo(_sec_item())
    assert "传输层内部炸了" in str(exc.value)


def test_failed_query_does_not_leak_task(wire):
    """失败的查询也要出任务表，否则任务表永远不减。"""
    tm._SESSION.logged_in = False
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    assert tm.pending_task_ids() == ()


# --- 判据 8：ETF 逐 code 发请求 -------------------------------------------
#
# 业务层 `get_etf_pcf` 先按 `code_list` 逐个 `get_tgw_type_code` 拆出
# `(market_type, security_code)` 填进 `SubCodeTableItem`，攒成列表一次性
# 传进来（`base_data.dis.txt:483-188`）。所以入参是**列表**，逐个发。


def test_etf_info_sends_one_request_per_code(wire):
    """列表里有几个 code 就发几条报文，每条一个 `Security`。"""
    items = [
        _sec_item("510050", tm.MarketType.kSSE),
        _sec_item("159915", tm.MarketType.kSZSE),
        _sec_item("588000", tm.MarketType.kSSE),
    ]
    tm.QueryETFInfo(items, query_spi=lambda d, s: None)

    assert len(wire.info) == 3
    got = [json.loads(p)["params"]["Security"] for p in wire.info]
    assert got == ["510050|101", "159915|102", "588000|101"]
    assert all(json.loads(p)["method"] == "ReqGetETFCodeTableList"
               for p in wire.info)


def test_etf_info_each_request_carries_its_own_task_id(wire):
    """每条报文一个独立 task_id，否则第二个 code 的回包认不回第一个。"""
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE),
                     _sec_item("159915", tm.MarketType.kSZSE)],
                    query_spi=lambda d, s: None)

    ids = [json.loads(p)["headers"]["id"] for p in wire.info]
    assert len(set(ids)) == 2
    assert set(ids) == set(tm.pending_task_ids())


def test_etf_info_each_code_gets_its_own_callback(wire):
    """每个 code 一次回调，各自带各的数据。"""
    items = [_sec_item("510050", tm.MarketType.kSSE),
             _sec_item("159915", tm.MarketType.kSZSE)]
    seen: list = []
    tm.QueryETFInfo(items, query_spi=lambda d, s: seen.append((d, s)))
    task_ids = tm.pending_task_ids()

    tm.deliver_response(task_ids[0], [{"security_code": "510050"}], None)
    tm.deliver_response(task_ids[1], [{"security_code": "159915"}], None)

    assert [d[0]["security_code"][0] for d, _ in seen] == ["510050", "159915"]


def test_etf_info_empty_list_reports_failure(wire):
    """空列表不是「查到了 0 条」，是参数错——必须说出来。"""
    seen: list = []
    tm.QueryETFInfo([], query_spi=lambda d, s: seen.append((d, s)))

    assert wire.info == []
    assert len(seen) == 1 and seen[0][0] is None
    assert isinstance(seen[0][1], str) and "空" in seen[0][1]


def test_etf_info_sync_success_returns_list_in_input_order(wire):
    """同步形态成功时按入参顺序回列表，每个元素是 `[信息表, 成分表]`。

    同步形态是**串行**的：发一个、等回包、再发下一个。回包由 `_Gateway`
    按 task_id 逐个应答，顺序即入参顺序。
    """
    items = [_sec_item("510050", tm.MarketType.kSSE),
             _sec_item("159915", tm.MarketType.kSZSE)]
    sent_codes: list[str] = []

    def reply(_task_id):
        # 按报文实际发出的顺序回，与真实网关的应答顺序一致
        code = json.loads(wire.info[-1])["params"]["Security"].split("|")[0]
        sent_codes.append(code)
        return [{"security_code": code, "nav": 1.23,
                 "creation_redemption_unit": 1000000}], None

    result, err = _Gateway(reply).run(tm.QueryETFInfo, items)

    assert err is None
    pairs, ret_err = result
    assert ret_err is None
    assert sent_codes == ["510050", "159915"]
    assert len(pairs) == 2
    assert all(len(pair) == 2 for pair in pairs), "每个元素必须是二元组"
    info, _constituent = pairs[0]
    assert info["security_code"][0] == "510050"
    assert pairs[1][0]["security_code"][0] == "159915"


def test_etf_info_splits_info_and_constituent_frames(wire):
    """回包按列名拆成 `[信息表, 成分表]`——业务层就是这么下标的。

    `get_etf_pcf` 取 `etf_pcf[0]['market_type'].iloc[0]` 拼 code，
    再把 `etf_pcf[1]` 当成分表 `apply(axis=1)` 后存 `['code']`。
    只给一帧它接不上。
    """
    rows = [
        {"security_code": "510050", "market_type": 101, "nav": 2.5},
        {"underlying_symbol": "600519", "component_share": 0.1,
         "substitute_flag": 0},
    ]
    seen: list = []
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: seen.append((d, s)))
    tm.deliver_response(tm.pending_task_ids()[0], rows, None)

    pair, status = seen[0]
    assert status is None
    info, constituent = pair
    assert info["security_code"].tolist() == ["510050"]
    assert constituent["underlying_symbol"].tolist() == ["600519"]


def test_etf_info_uses_business_layer_column_lists(wire):
    """两组列名逐字来自 `base_data.dis.txt:200/208`，不是猜的集合。"""
    assert len(tm._ETF_INFO_COLUMNS) == 25
    assert len(tm._ETF_CONSTITUENT_COLUMNS) == 9
    assert "creation_redemption_unit" in tm._ETF_INFO_COLUMNS
    assert "nav" in tm._ETF_INFO_COLUMNS
    assert "underlying_symbol" in tm._ETF_CONSTITUENT_COLUMNS
    assert "component_share" in tm._ETF_CONSTITUENT_COLUMNS
    # 业务层第 529 行无条件取 [0] 当信息表，所以两组不能完全重合
    assert tm._ETF_INFO_COLUMNS != tm._ETF_CONSTITUENT_COLUMNS


def test_etf_info_sync_flow_over_limit_raises_on_first_code(wire):
    """服务端拒绝时第一项就抛，不发后续 code。

    交回半截列表比全失败更难排查：业务层会拿着长度不足的 `etf_pcf_list`
    继续往下走，在几十行之外炸出看不懂的 KeyError。
    """
    items = [_sec_item("510050", tm.MarketType.kSSE),
             _sec_item("159915", tm.MarketType.kSZSE)]
    result, err = _Gateway(
        lambda _tid: (None, FLOW_OVER_LIMIT_STATUS)
    ).run(tm.QueryETFInfo, items)

    assert result is None
    assert isinstance(err, tm.NotConnected)
    assert "QueryETFInfo" in str(err)
    assert "ReqGetETFCodeTableList" in str(err)
    assert len(wire.info) == 1, "第一项失败就不该再发第二条"
    assert tm.pending_task_ids() == ()


# --- 判据 9：响应侧的 tag 与状态 -------------------------------------------


def test_captured_responses_have_distinct_tags(wire):
    """两条通道的 `tag` 不同：证券信息 109、ETF 111。

    值本身取自抓包（【抓包实证】）。tag 由服务端回，替身不解析它——
    这条钉的是「别把两个接口当成同一条通道」。
    """
    assert CAPTURED_CODE_TABLE_RESPONSE["headers"]["tag"] == "109"
    assert CAPTURED_ETF_RESPONSE["headers"]["tag"] == "111"
    assert (CAPTURED_CODE_TABLE_RESPONSE["headers"]["tag"]
            != CAPTURED_ETF_RESPONSE["headers"]["tag"])
    assert CAPTURED_CODE_TABLE_RESPONSE["status"] == tm.FLOW_OVER_LIMIT == -82


def test_flow_over_limit_constant_matches_enums():
    """`-82` 的枚举名是 `kFlowOverLimit`——常量不是拍脑袋写的。"""
    from tgwpy.enums import ErrorCode as Mined

    assert tm.FLOW_OVER_LIMIT == int(Mined.kFlowOverLimit) == -82


# --- 判据 10：不再走 _unimplemented ----------------------------------------


def test_both_queries_are_no_longer_unimplemented(wire):
    """`compat.QuerySecuritiesInfo` / `QueryETFInfo` 不再调 `_unimplemented`。

    发一条报文就是最直接的证据——`_unimplemented` 压根不发报文。
    """
    tm.QuerySecuritiesInfo(_sec_item(), query_spi=lambda d, s: None)
    tm.QueryETFInfo([_sec_item("510050", tm.MarketType.kSSE)],
                    query_spi=lambda d, s: None)
    assert len(wire.info) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
