"""行情查询（`QueryKline` / `QuerySnapshot`）在 tgw 替身上的验收测试。

**判据来源分两类，别混。**

* 请求字节：拿抓包拿到的**报文原文**
  当黄金标准，逐字段相等。字段名来自静态反编译，**值**只能从真实
  报文读——`period_type` 填 10008 就恒定无回包。
* 响应 DataFrame：列名与量纲来自业务层字节码
  （`utils/convert.py` 的 `convert_history_kline` / `convert_history_tick_stock`）。
  业务层自己会再除一遍 1e6/100/1e5，替身交出去就必须是**未缩放的整数**。

不碰网络：`send_market` 是个记账器，回包由测试自己调
`deliver_response` 注入。

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

from conftest import needs_business_layer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy.compat import tgw_module as tm  # noqa: E402
from tgwpy.compat import structs  # noqa: E402

# --- 抓包原文（真实网关实证）------------------------------------------------

CAPTURED_KLINE_PARAMS = {
    "security_code": "600519", "market_type": 101, "cq_flag": 0,
    "auto_complete": 1, "period_type": 10100, "begin_date": 20260420,
    "end_date": 20260424, "begin_time": 900, "end_time": 1700,
    "QueryBandWidth": 0.0,
}
CAPTURED_SNAPSHOT_PARAMS = {
    "security_code": "600519", "market_type": 101, "date": 20260424,
    "begin_time": 90000000, "end_time": 170000000, "data_type": 0,
    "QueryBandWidth": 0.0,
}

#: 抓到的 K 线 CSV 前两根（`tests/test_market.py` 同源）
RAW_KLINE_ROWS = [
    "600519,101,20260420,1403000000,1413940000,1400000000,1410890000,"
    "364513900,512714604145000",
    "600519,101,20260421,1411000000,1419900000,1410000000,1412010000,"
    "217542700,307459519172000",
]
#: 抓到的快照首行（全 0，盘前）与末行（有真实买卖档）
SNAP_FIRST = (
    "600519,101,20260424090025000,S 10,1419000000,0,0,0,0,0,"
    "0|0|0|0|0|0|0|0|0|0,0|0|0|0|0|0|0|0|0|0,0|0|0|0|0|0|0|0|0|0,"
    "0|0|0|0|0|0|0|0|0|0,"
    "0,0,0,0,1560900000,1277100000,0,0,0,0,,,,,,0,0,0,0,0,0,"
)
SNAP_LAST = (
    "600519,101,20260424162955000,E110,1419000000,1413100000,1458880000,1413100000,"
    "1458490000,1458490000,"
    "1458480000|1458470000|1458460000|1458450000|1458440000|1458420000|1458400000|"
    "1458390000|1458380000|1458370000,"
    "30000|70000|355000|80000|3600000|20000|80000|215000|90000|270000,"
    "1458490000|1458500000|1458510000|1458520000|1458530000|1458540000|1458550000|"
    "1458560000|1458580000|1458590000,"
    "806100|420000|180000|75200|30000|10000|130000|140000|50000|20000,"
    "42029,554553100,800341099907000,0,1560900000,1277100000,35847000,49883700,"
    "1409278000,1498567000,,,,,,0,0,0,0,0,0,"
)
RAW_SNAPSHOT_ROWS = [SNAP_FIRST, SNAP_LAST]

#: 参照实现 CLI 对同一批 K 线输出的数值。替身交出去的是**未缩放整数**，
#: 乘回去的常数就锁在这里：`1403.0 * 1e6 == 1403000000`。
REFERENCE_KLINE = [
    {"kline_time": 20260420, "kline_time_iso": "2026-04-20",
     "open": 1403.0, "high": 1413.94, "low": 1400.0,
     "close": 1410.89, "volume": 3645139.0, "amount": 5127146041.45},
    {"kline_time": 20260421, "kline_time_iso": "2026-04-21",
     "open": 1411.0, "high": 1419.9, "low": 1410.0,
     "close": 1412.01, "volume": 2175427.0, "amount": 3074595191.72},
]
#: 参照实现 `query_snapshot` 末行的 35 列（业务层 `convert_history_tick_stock`
#: 的输出列名，与替身交进去的输入列名不是同一套）
REFERENCE_SNAPSHOT = {
    "code": "600519.SH",
    "pre_close": 1419.0, "last": 1458.49, "open": 1413.1, "high": 1458.88,
    "low": 1413.1, "close": 1458.49, "volume": 5545531.0,
    "amount": 8003410999.07, "num_trades": 42029,
    "high_limited": 1560.9, "low_limited": 1277.1, "iopv": 0.0,
    "trading_phase_code": "E110",
    "ask_price1": 1458.49, "ask_volume1": 8061.0,
    "bid_price1": 1458.48, "bid_volume1": 300.0,
    "ask_price2": 1458.5, "ask_volume2": 4200.0,
    "bid_price2": 1458.47, "bid_volume2": 700.0,
    "ask_price3": 1458.51, "ask_volume3": 1800.0,
    "bid_price3": 1458.46, "bid_volume3": 3550.0,
    "ask_price4": 1458.52, "ask_volume4": 752.0,
    "bid_price4": 1458.45, "bid_volume4": 800.0,
    "ask_price5": 1458.53, "ask_volume5": 300.0,
    "bid_price5": 1458.44, "bid_volume5": 36000.0,
}

# --- 测试替身：记账传输层 + 已登录会话 ------------------------------------


class _FakeTransport:
    """只记账，不联网。`send_market` 是传输层与替身之间的硬契约。"""

    def __init__(self) -> None:
        self.market: list[bytes] = []
        self.info: list[bytes] = []
        self.raise_on_market: Exception | None = None

    def send(self, payload: bytes) -> None:
        self.info.append(payload)

    def send_market(self, payload: bytes) -> None:
        if self.raise_on_market is not None:
            raise self.raise_on_market
        self.market.append(payload)


@pytest.fixture
def wire(monkeypatch):
    """装好一个已登录的会话，产出记账传输层。测完还原全局状态。"""
    tm._SESSION.reset()
    transport = _FakeTransport()
    tm.set_transport(transport)
    tm._SESSION.logged_in = True
    tm._SESSION.username = "600001"
    tm._SESSION.token = "TOKEN"
    tm._SESSION.api_mode = tm.ApiMode.kInternetMode
    yield transport
    tm._SESSION.reset()


def _kline_cfg(**over) -> structs.ReqKline:
    """业务层 `market_data.py:199-208` 逐字段赋值后的 `tgw.ReqKline`。"""
    cfg = structs.ReqKline()
    cfg.security_code = over.get("security_code", "600519")
    cfg.market_type = over.get("market_type", 101)
    cfg.cyc_type = over.get("cyc_type", tm.MDDatatype.kDayKline)
    cfg.begin_date = over.get("begin_date", 20260420)
    cfg.end_date = over.get("end_date", 20260424)
    cfg.begin_time = over.get("begin_time", 900)
    cfg.end_time = over.get("end_time", 1700)
    cfg.cq_flag = over.get("cq_flag", 0)
    cfg.auto_complete = over.get("auto_complete", 1)
    return cfg


def _tick_cfg(**over) -> structs.ReqDefault:
    """业务层 `market_data.py:64-69` 逐字段赋值后的 `tgw.ReqDefault`。"""
    cfg = structs.ReqDefault()
    cfg.security_code = over.get("security_code", "600519")
    cfg.market_type = over.get("market_type", 101)
    cfg.date = over.get("date", 20260424)
    cfg.begin_time = over.get("begin_time", 90000000)
    cfg.end_time = over.get("end_time", 170000000)
    cfg.data_type = over.get("data_type", 0)
    return cfg


# --- 判据 1：请求字节与抓包原文逐字段相等 ----------------------------------


def test_kline_wire_matches_capture_field_by_field(wire):
    """`ReqGetKline` 的 params 必须与抓包原文逐字段相等。"""
    tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: None)

    assert len(wire.market) == 1
    payload = json.loads(wire.market[0])
    assert payload["method"] == "ReqGetKline"
    assert payload["params"] == CAPTURED_KLINE_PARAMS


def test_snapshot_wire_matches_capture_field_by_field(wire):
    """`ReqGetSnapshot` 的 params 必须与抓包原文逐字段相等。"""
    tm.QuerySnapshot(_tick_cfg(), query_spi=lambda d, s: None)

    assert len(wire.market) == 1
    payload = json.loads(wire.market[0])
    assert payload["method"] == "ReqGetSnapshot"
    assert payload["params"] == CAPTURED_SNAPSHOT_PARAMS


def test_wire_carries_id_in_headers_with_session_credentials(wire):
    """`id` 在 `headers` 里（顶层不放），并带上会话凭据。

    `headers.id` 是运行时任务号，只断言它等于登记进任务表的那个——
    同一个号必须同时出现在两处，否则 `deliver_response` 认领不到。
    """
    tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: None)
    payload = json.loads(wire.market[0])

    assert "id" not in payload
    assert payload["headers"]["id"] == tm.pending_task_ids()[0]
    assert payload["headers"]["userName"] == "600001"
    assert payload["headers"]["token"] == "TOKEN"


def test_period_type_map_covers_all_thirteen_cycles():
    """`Period` 的 13 个成员逐个映射到 10100/10000 两档周期码。

    `Period` 的值恒等于 `MDDatatype`（`utils/constant.py:27-40`），
    日线及以上走 10100 档，分钟线走 10000 档。
    """
    table = tm._period_table()
    assert table[tm.MDDatatype.kDayKline] == 10100
    assert table[tm.MDDatatype.kWeekKline] == 10101
    assert table[tm.MDDatatype.kMonthKline] == 10102
    assert table[tm.MDDatatype.kSeasonKline] == 10103
    assert table[tm.MDDatatype.kYearKline] == 10104
    assert table[tm.MDDatatype.k1KLine] == 10000
    assert table[tm.MDDatatype.k120KLine] == 10007
    assert len(table) == 13


def test_minute_line_uses_10000_band_not_day_band(wire):
    """分钟线的 `period_type` 落在 10000 档，不能混进 10100 档。"""
    tm.QueryKline(_kline_cfg(cyc_type=tm.MDDatatype.k5KLine),
                  query_spi=lambda d, s: None)
    params = json.loads(wire.market[0])["params"]
    assert params["period_type"] == 10002
    assert params["period_type"] < 10100


def test_night_session_begin_time_is_passed_through(wire):
    """夜盘品种业务层填 2000/1700，替身原样送线上，不私自改。"""
    tm.QueryKline(_kline_cfg(begin_time=2000, end_time=1700),
                  query_spi=lambda d, s: None)
    params = json.loads(wire.market[0])["params"]
    assert params["begin_time"] == 2000
    assert params["end_time"] == 1700


# --- 判据 2：同步模式（query_spi=None）返回 DataFrame ------------------------


def _run_sync(fn, *args, rows, delay=0.05, **kwargs):
    """起一个线程发同步查询，主线程拿到 task_id 后注入回包。

    回包必须由另一条线程注入，否则同步查询自己就在等自己。
    """
    box: dict[str, object] = {}

    def _call():
        box["result"] = fn(*args, **kwargs)

    worker = threading.Thread(target=_call)
    worker.start()
    deadline = time.time() + 5.0
    while not tm.pending_task_ids() and time.time() < deadline:
        time.sleep(0.005)
    assert tm.pending_task_ids(), "查询没能登记进任务表"
    time.sleep(delay)
    tm.deliver_response(tm.pending_task_ids()[0], rows, None)
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "同步查询没有返回"
    return box["result"]


def test_kline_sync_mode_returns_dataframe(wire):
    """`query_spi=None` 时阻塞等回包，返回 `(DataFrame, None)`。"""
    result, err = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS)

    assert err is None
    assert result.__class__.__name__ == "DataFrame"
    assert len(result) == 2
    assert list(result.columns) == list(tm._KLINE_COLUMNS)


def test_kline_sync_values_are_unscaled_integers(wire):
    """交出去的是 tgw 原生整数：业务层还要自己除一遍 1e6/100/1e5。"""
    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS)

    for row, want in zip(result.to_dict("records"), REFERENCE_KLINE):
        assert row["kline_time"] == want["kline_time"]
        assert row["open_price"] == want["open"] * 1e6
        assert row["high_price"] == want["high"] * 1e6
        assert row["low_price"] == want["low"] * 1e6
        assert row["close_price"] == want["close"] * 1e6
        assert row["volume_trade"] == want["volume"] * 1e2
        assert row["value_trade"] == want["amount"] * 1e5
        assert row["security_code"] == "600519"


def test_kline_df_feeds_business_layer_conversion(wire):
    """`convert_history_kline` 的四个除法 + `pd.to_datetime` 必须能跑通。

    这是替身交给业务层的**唯一验收标准**：业务层拿到这个 DataFrame
    后自己会再除一遍 1e6/100/1e5，替身先除了就对不上。
    """
    import pandas as pd

    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS)
    df = result

    # convert.py:773-783，逐字复刻
    convert = pd.DataFrame(columns=["code", "kline_time", "open", "high",
                                    "low", "close", "volume", "amount"])
    convert["code"] = ["600519.SH"] * len(df)
    convert["kline_time"] = pd.to_datetime(
        df["kline_time"].astype(str), format="%Y%m%d")
    for src, dst in (("open_price", "open"), ("high_price", "high"),
                     ("low_price", "low"), ("close_price", "close")):
        convert[dst] = df[src].div(1000000)
    convert["volume"] = df["volume_trade"].div(100).astype(int)
    convert["amount"] = df["value_trade"].div(100000)

    for row, want in zip(convert.to_dict("records"), REFERENCE_KLINE):
        assert abs(row["open"] - want["open"]) < 1e-9
        assert abs(row["close"] - want["close"]) < 1e-9
        assert row["volume"] == int(want["volume"])
        assert abs(row["amount"] - want["amount"]) < 1e-6
        assert row["kline_time"] == pd.Timestamp(want["kline_time_iso"])


#: 分钟线的线上原文。9 段，`kline_time` 是 12 位；一天 242 根，这里取头三根。
#: 列序照 `frames/market.KLINE_CSV_FIELDS`：
#: security_code, market_type, kline_time, open, high, low, close, volume, amount
RAW_KLINE_MIN1_ROWS = [
    "510050,101,202609290930,600123,600133,600113,600130,10000,5000000",
    "510050,101,202609290931,600130,600140,600120,600135,12000,6100000",
    "510050,101,202609290932,600135,600145,600125,600140,15000,7300000",
]


def test_min1_kline_time_keeps_twelve_digits(wire):
    """分钟线的 `kline_time` 必须是 **12 位**，8 位会被当成同一天。

    业务层 `convert_history_kline` 对分钟周期用 `'%Y%m%d%H%M'` 解析
    （`recovered/AmazingData/utils/convert.dis.txt` 源码行 770-772），
    截成 8 位等于把一天 242 根全压成同一个时间戳。
    """
    result, err = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_MIN1_ROWS)

    assert err is None
    times = result["kline_time"].tolist()
    assert times == [202609290930, 202609290931, 202609290932]
    assert len(set(times)) == 3, "三个时间戳必须彼此不同"


def test_min1_feeds_business_layer_minute_format(wire):
    """整条链验完：替身给 12 位 → 业务层的 `%Y%m%d%H%M` 解析得出来。"""
    import pandas as pd

    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_MIN1_ROWS)
    parsed = pd.to_datetime(result["kline_time"].astype(str), format="%Y%m%d%H%M")

    assert parsed.iloc[0] == pd.Timestamp("2026-09-29 09:30:00")
    assert parsed.iloc[2] == pd.Timestamp("2026-09-29 09:32:00")


@pytest.mark.parametrize("raw, want", [
    ("2026-04-20", 20260420),                 # 日线，8 位
    ("2026-09-29 09:30", 202609290930),       # 分钟线，12 位
    (20260420, 20260420),
    (None, 0),
    ("", 0),
])
def test_kline_time_int_keeps_digit_count(raw, want):
    """分隔符全剔掉，只留数字——位数是协议的一部分，不是显示格式。"""
    assert tm._kline_time_int(raw) == want


def test_snapshot_sync_mode_returns_dataframe(wire):
    """快照同步路径同样返回 DataFrame，列数与列名钉死。"""
    result, err = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)

    assert err is None
    assert result.__class__.__name__ == "DataFrame"
    assert len(result) == 2
    assert list(result.columns) == list(tm._SNAPSHOT_COLUMNS)
    # orig_time/security_code + 13 个单值列 + 两侧各 6 档 ×(价+量)
    assert len(result.columns) == 2 + 13 + 2 * 6 * 2


def test_snapshot_df_feeds_business_layer_conversion(wire):
    """复刻 `convert_history_tick_stock` 640-664，末行 34 列必须与参照相等。

    这是替身交给业务层的**唯一验收标准**：业务层拿到这个 DataFrame
    后自己再除一遍 1e6/100/1e5 并按 `%Y%m%d%H%M%S%f` 解析时间，
    替身先除了就对不上。
    """
    import pandas as pd

    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)
    snapshot_df = result

    # convert.py:641-664，逐字复刻
    convert = pd.DataFrame(columns=list(REFERENCE_SNAPSHOT))
    convert["code"] = ["600519.SH"] * len(snapshot_df)
    convert["trade_time"] = pd.to_datetime(
        snapshot_df["orig_time"].astype(str), format="%Y%m%d%H%M%S%f",
    ).dt.floor("s")
    for i in range(1, 7):
        convert[f"ask_price{i}"] = snapshot_df[f"offer_price{i}"].div(1000000)
        convert[f"ask_volume{i}"] = snapshot_df[f"offer_volume{i}"].div(100)
        convert[f"bid_price{i}"] = snapshot_df[f"bid_price{i}"].div(1000000)
        convert[f"bid_volume{i}"] = snapshot_df[f"bid_volume{i}"].div(100)
    convert["pre_close"] = snapshot_df["pre_close_price"].div(1000000)
    convert["last"] = snapshot_df["last_price"].div(1000000)
    convert["open"] = snapshot_df["open_price"].div(1000000)
    convert["high"] = snapshot_df["high_price"].div(1000000)
    convert["low"] = snapshot_df["low_price"].div(1000000)
    convert["close"] = snapshot_df["close_price"].div(1000000)
    convert["volume"] = snapshot_df["total_volume_trade"].div(100)
    convert["amount"] = snapshot_df["total_value_trade"].div(100000)
    convert["num_trades"] = snapshot_df["num_trades"]
    convert["high_limited"] = snapshot_df["high_limited"].div(1000000)
    convert["low_limited"] = snapshot_df["low_limited"].div(1000000)
    convert["iopv"] = snapshot_df["IOPV"].div(1000000)
    convert["trading_phase_code"] = snapshot_df["trading_phase_code"]

    # 参照实现 query_snapshot 的末行，35 列
    last = convert.to_dict("records")[-1]
    for name, want in REFERENCE_SNAPSHOT.items():
        got = last[name]
        if isinstance(want, str):
            assert got == want, f"{name}: {got!r} != {want!r}"
        elif name == "num_trades":
            assert got == want, f"{name}: {got} != {want}"
        else:
            assert abs(got - want) < 1e-6, f"{name}: {got} != {want}"


def test_snapshot_input_columns_are_unscaled_integers(wire):
    """替身交出去的是 tgw 原生整数，列名是 `convert_history_tick_stock` 读的那套。"""
    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)
    last = result.to_dict("records")[-1]

    assert last["offer_price1"] == 1458490000     # CSV 段 12 第 1 档
    assert last["bid_price1"] == 1458480000       # CSV 段 10 第 1 档
    assert last["pre_close_price"] == 1419000000
    assert last["total_volume_trade"] == 554553100
    assert last["total_value_trade"] == 800341099907000
    assert last["num_trades"] == 42029            # 段 14 不缩放


def test_snapshot_book_uses_offer_prefix_not_ask(wire):
    """档位列名跟业务层走：`offer_price1`，不是 `ask_price1`。

    `convert_history_tick_stock` 第 647-650 读的是 `offer_*`。
    """
    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)
    last = result.to_dict("records")[-1]

    assert "offer_price1" in result.columns and "ask_price1" not in result.columns
    assert "offer_volume1" in result.columns and "ask_volume1" not in result.columns
    # 业务层 `range(1, 7)` 读到第 6 档，线上 10 档够用
    assert last["offer_price6"] == 1458540000
    assert last["bid_price6"] == 1458420000
    # 只暴露 6 档：第 7 档线上明明有，替身不外送
    assert "offer_price7" not in result.columns


def test_snapshot_orig_time_keeps_millisecond_digits(wire):
    """`orig_time` 必须是 17 位串——业务层按 `%Y%m%d%H%M%S%f` 解析。

    `frames/market` 的 `_decode_time` 会把这 3 位毫秒丢掉，所以替身
    必须从原始 CSV 行取，不能用解码后的 `trade_time`。
    """
    import pandas as pd

    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)
    first, last = result["orig_time"].tolist()

    assert first == "20260424090025000"
    assert last == "20260424162955000"
    # convert.py:643-645 的原样复刻，缺 %f 会直接抛错
    assert str(pd.to_datetime(pd.Series([last]).astype(str),
                              format="%Y%m%d%H%M%S%f").dt.floor("s")[0]) \
        == "2026-04-24 16:29:55"


def test_snapshot_book_uses_offer_prefix_not_ask(wire):
    """档位列名跟业务层走：`offer_price1`，不是 `ask_price1`。

    `convert_history_tick_stock` 第 647-650 读的是 `offer_*`。
    """
    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)
    last = result.to_dict("records")[-1]

    assert "offer_price1" in result.columns and "ask_price1" not in result.columns
    assert "offer_volume1" in result.columns and "ask_volume1" not in result.columns
    assert last["offer_price1"] == 1458490000     # CSV 段 12 第 1 档
    assert last["bid_price1"] == 1458480000       # CSV 段 10 第 1 档
    # 业务层 `range(1, 7)` 读到第 6 档，线上给 10 档够用
    assert last["offer_price6"] == 1458540000
    assert last["bid_price6"] == 1458420000
    # 只外送 6 档：第 7 档线上明明有，替身不外送
    assert "offer_price7" not in result.columns


def test_return_df_format_false_gives_record_list(wire):
    """`return_df_format=False` 出记录列表，官方中间层也是这个分岔。"""
    result, err = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS,
                            return_df_format=False)

    assert err is None
    assert isinstance(result, list) and len(result) == 2
    assert list(result[0]) == list(tm._KLINE_COLUMNS)
    assert result[0]["open_price"] == 1403000000


# --- 判据 3：异步模式（有 query_spi）走回调 ---------------------------------


def test_kline_async_mode_calls_back_with_dataframe(wire):
    """给了 `query_spi` 就走回调，返回值只表示发出去了。"""
    seen: list = []
    ok, err = tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: seen.append((d, s)))

    assert (ok, err) == (True, None)
    assert len(seen) == 0, "回包还没到，不该有回调"

    tm.deliver_response(tm.pending_task_ids()[0], RAW_KLINE_ROWS, None)
    assert len(seen) == 1
    data, status = seen[0]
    assert status is None
    assert data.__class__.__name__ == "DataFrame"
    assert len(data) == 2


def test_snapshot_async_mode_calls_back_with_dataframe(wire):
    seen: list = []
    ok, err = tm.QuerySnapshot(_tick_cfg(), query_spi=lambda d, s: seen.append((d, s)))

    assert (ok, err) == (True, None)
    tm.deliver_response(tm.pending_task_ids()[0], RAW_SNAPSHOT_ROWS, None)

    data, status = seen[0]
    assert status is None
    assert data.__class__.__name__ == "DataFrame"
    assert len(data) == 2
    assert list(data.columns) == list(tm._SNAPSHOT_COLUMNS)


def test_async_callback_exception_does_not_escape(wire):
    """业务层回调抛异常只记日志，不能把传输层的接收线程带崩。"""
    def boom(data, status):
        raise RuntimeError("业务层自己的问题")

    tm.QueryKline(_kline_cfg(), query_spi=boom)
    tm.deliver_response(tm.pending_task_ids()[0], RAW_KLINE_ROWS, None)  # 不抛即通过


# --- 判据 4：三条失败路径都变成可见失败，不抛异常 --------------------------


def test_async_not_logged_in_calls_back_with_error(wire):
    """未登录：必须回调（业务层卡在 `wait_event.wait()` 上），且 status 是字符串。"""
    tm._SESSION.logged_in = False
    seen: list = []
    tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str) and "未登录" in status


def test_async_no_transport_calls_back_with_error(wire):
    """没接传输层：同样走回调，`error_list` 如实收到。"""
    tm._SESSION.set_transport(None)
    seen: list = []
    tm.QuerySnapshot(_tick_cfg(), query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str) and "传输层" in status


def test_async_send_exception_calls_back_with_error(wire):
    """发送失败必须变成可见失败——抛出去会让业务层永远醒不过来。"""
    wire.raise_on_market = OSError("连接被对端重置")
    seen: list = []
    tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str)
    assert "发送失败" in status and "连接被对端重置" in status


# 同步形态的失败按**官方契约**返回 `(None, 错误文案)`，不抛异常：
# `tgw/interface.py:214-219` 明确 `result=None, err=error_code`。
# 抛异常是 `QueryThirdInfo` 那条路的取舍（业务层丢掉 error 直接下标），
# 行情族不适用——`market_data.py` 只用 SPI 形态，err 它看得见。
def test_sync_not_logged_in_returns_none_and_error(wire):
    """同步形态未登录：回 `(None, 错误文案)`，不返回空数据冒充成功。"""
    tm._SESSION.logged_in = False
    result, err = tm.QueryKline(_kline_cfg())
    assert result is None
    assert isinstance(err, str) and "未登录" in err


def test_sync_no_transport_returns_none_and_error(wire):
    tm._SESSION.set_transport(None)
    result, err = tm.QuerySnapshot(_tick_cfg())
    assert result is None
    assert isinstance(err, str) and "传输层" in err


def test_sync_send_exception_returns_none_and_error(wire):
    wire.raise_on_market = OSError("连接被对端重置")
    result, err = tm.QueryKline(_kline_cfg())
    assert result is None
    assert isinstance(err, str) and "发送失败" in err


def test_sync_send_exception_does_not_propagate(wire):
    """发送异常绝不能冒到业务层——那会把它停在 `wait_event.wait()` 上。"""
    wire.raise_on_market = RuntimeError("传输层内部炸了")
    result, err = tm.QueryKline(_kline_cfg())   # 不抛即通过
    assert result is None
    assert "传输层内部炸了" in err


def test_unknown_period_reports_error_without_raising(wire):
    """`cyc_type` 不在 13 个周期里：回调字符串 / 同步返回 (None, 错误)。"""
    cfg = _kline_cfg()
    cfg.cyc_type = 999999

    seen: list = []
    tm.QueryKline(cfg, query_spi=lambda d, s: seen.append((d, s)))
    assert len(seen) == 1
    assert seen[0][0] is None
    assert isinstance(seen[0][1], str) and "请求构造失败" in seen[0][1]

    result, err = tm.QueryKline(_kline_cfg(cyc_type=999999))
    assert result is None
    assert isinstance(err, str) and "请求构造失败" in err


def test_failed_query_does_not_leak_task(wire):
    """失败的查询也要出任务表，否则 `req_list_len` 永远减不到 0。"""
    tm._SESSION.logged_in = False
    tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: None)
    assert tm.pending_task_ids() == ()


def test_timeout_returns_visible_failure(wire, monkeypatch):
    """同步等到点没回包，按失败处理，不无限期挂着。"""
    monkeypatch.setattr(tm, "MARKET_TIMEOUT", 0.1)
    result, err = tm.QueryKline(_kline_cfg())
    assert result is None
    assert isinstance(err, str) and "没等到响应" in err


# --- 判据 5：CSV → DataFrame 的列名 / dtype / 条数 -------------------------


def test_kline_frame_shape_dtype_and_row_count(wire):
    """列名、dtype、条数三项都要钉死。"""
    import pandas as pd

    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS)

    assert result.shape == (2, 8)
    assert list(result.columns) == [
        "kline_time", "open_price", "high_price", "low_price", "close_price",
        "volume_trade", "value_trade", "security_code",
    ]
    # 线上整数交出去，dtype 必须是整数，业务层的 `div(1e6)` 才有意义
    for col in ("open_price", "high_price", "low_price", "close_price",
                "volume_trade", "value_trade"):
        assert result[col].dtype.kind == "i", f"{col} 是 {result[col].dtype}"
    assert result["kline_time"].dtype.kind == "i"
    assert pd.api.types.is_string_dtype(result["security_code"])
    assert result["security_code"].tolist() == ["600519", "600519"]


def test_snapshot_frame_shape_dtype_and_row_count(wire):
    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)

    assert result.shape == (2, len(tm._SNAPSHOT_COLUMNS))
    assert result["orig_time"].tolist() == [
        "20260424090025000", "20260424162955000"]
    assert result["security_code"].tolist() == ["600519", "600519"]
    assert result["trading_phase_code"].tolist() == ["S 10", "E110"]
    assert result["num_trades"].dtype.kind == "i"
    for col in ("pre_close_price", "last_price", "open_price", "high_price",
                "low_price", "close_price", "high_limited", "low_limited",
                "IOPV", "total_volume_trade", "total_value_trade",
                "bid_price1", "offer_price1", "bid_volume1", "offer_volume1"):
        assert result[col].dtype.kind == "i", f"{col} 是 {result[col].dtype}"


def test_malformed_csv_rows_are_skipped(wire):
    """段数不对的行直接丢，不硬凑——凑出来的行会让业务层除错除数。"""
    rows = RAW_KLINE_ROWS + ["1,2,3", "", None, "600519,101,20260422"]
    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=rows)
    assert len(result) == 2


def test_malformed_snapshot_rows_are_skipped(wire):
    rows = [SNAP_LAST, "1,2,3", "", None]
    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=rows)
    assert len(result) == 1


def test_empty_rows_give_empty_frame_with_full_columns(wire):
    """空数据给空表 + 全列，不给缺列的空壳。"""
    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=[])
    assert len(result) == 0
    assert list(result.columns) == list(tm._KLINE_COLUMNS)


def test_response_with_unknown_task_id_is_dropped(wire):
    """迟到的包只记日志，不碰业务层，也不炸。"""
    seen: list = []
    tm.QueryKline(_kline_cfg(), query_spi=lambda d, s: seen.append((d, s)))
    tm.deliver_response(999999999, RAW_KLINE_ROWS, None)   # 不抛即通过
    assert seen == []


# --- 判据 6：真跑 AmazingData 的转换函数（最强证据）------------------------
#
# 前面的判据都是「按字节码复刻业务层的除法」，这一段直接 import 真模块
# 调 `convert_history_kline` / `convert_history_tick_stock`。复刻会抄错，
# 真跑不会——它要是列名对不上或量纲差 1e6，当场就炸。


def _install_compat() -> None:
    from tgwpy.compat import install

    install()


@needs_business_layer
def test_real_kline_frame_converts_to_reference_output(wire):
    """真跑 `convert_history_kline`，输出与参照实现的 8 列逐项相等。"""
    _install_compat()
    from AmazingData.utils.constant import Kline, Period
    from AmazingData.utils.convert import convert_history_kline

    assert list(Kline.model_fields) == [
        "code", "kline_time", "open", "high", "low", "close", "volume", "amount"]

    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS)
    out = convert_history_kline("600519.SH", Period.day.value, result)

    assert list(out.columns) == [
        "code", "kline_time", "open", "high", "low", "close", "volume", "amount"]
    for row, want in zip(out.to_dict("records"), REFERENCE_KLINE):
        assert row["code"] == "600519.SH"
        assert str(row["kline_time"])[:10] == want["kline_time_iso"]
        assert abs(row["open"] - want["open"]) < 1e-9
        assert abs(row["high"] - want["high"]) < 1e-9
        assert abs(row["low"] - want["low"]) < 1e-9
        assert abs(row["close"] - want["close"]) < 1e-9
        assert row["volume"] == int(want["volume"])
        assert abs(row["amount"] - want["amount"]) < 1e-6


@needs_business_layer
def test_real_snapshot_frame_converts_to_reference_output(wire):
    """真跑 `convert_history_tick_stock`，末行 34 列与参照实现逐项相等。"""
    _install_compat()
    from AmazingData.utils.constant import Snapshot
    from AmazingData.utils.convert import convert_history_tick_stock

    assert len(Snapshot.model_fields) == 35

    result, _ = _run_sync(tm.QuerySnapshot, _tick_cfg(), rows=RAW_SNAPSHOT_ROWS)
    out = convert_history_tick_stock("600519.SH", result)
    last = out.to_dict("records")[-1]

    assert list(out.columns) == list(Snapshot.model_fields)
    for name, want in REFERENCE_SNAPSHOT.items():
        got = last[name]
        if isinstance(want, str):
            assert got == want, f"{name}: {got!r} != {want!r}"
        elif name == "num_trades":
            assert got == want, f"{name}: {got} != {want}"
        else:
            assert abs(got - want) < 1e-6, f"{name}: {got} != {want}"


@needs_business_layer
def test_real_business_layer_rejects_unscaled_frame(wire):
    """反向判据：替身若提前把价格除掉，业务层会再除一遍，数值立刻离谱。

    这条把「量纲」钉死——列名对了但除数也对了，才算真的通。
    """
    _install_compat()
    from AmazingData.utils.constant import Period
    from AmazingData.utils.convert import convert_history_kline

    result, _ = _run_sync(tm.QueryKline, _kline_cfg(), rows=RAW_KLINE_ROWS)
    # 模拟「替身先除了一遍」的错法：1403000000 → 1403.0，业务层再除 → 0.001403
    halved = result.copy()
    halved["open_price"] = halved["open_price"] / 1e6
    out = convert_history_kline("600519.SH", Period.day.value, halved)
    assert abs(out.to_dict("records")[0]["open"] - 0.001403) < 1e-12


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
