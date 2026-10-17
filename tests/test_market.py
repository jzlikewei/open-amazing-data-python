"""行情帧的单测：请求构造 + CSV 记录解码。

这些值全部来自 MITM 抓到的参照客户端真实报文（抓包实证
的「抓包定论」一节），不是从静态反编译推的。静态反编译只能定键名，
定不了取值——`period_type` 和 `begin_time` 就是这么错到无回包的。

不碰网络：只验证构造出来的字节和解出来的字典。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy.frames import market  # noqa: E402


# 参照 SDK 实际发出的报文（抓包原文）
CAPTURED_KLINE = {
    "security_code": "600519", "market_type": 101, "cq_flag": 0,
    "auto_complete": 1, "period_type": 10100, "begin_date": 20260420,
    "end_date": 20260424, "begin_time": 900, "end_time": 1700,
    "QueryBandWidth": 0.0,
}
CAPTURED_SNAPSHOT = {
    "security_code": "600519", "market_type": 101, "date": 20260424,
    "begin_time": 90000000, "end_time": 170000000, "data_type": 0,
    "QueryBandWidth": 0.0,
}


def test_kline_params_match_capture():
    req = market.KlineRequest(
        security_code="600519", market_type=101,
        period_type=market.PERIOD_TYPE["day"],
        begin_date=20260420, end_date=20260424,
    )
    assert req.to_params() == CAPTURED_KLINE


def test_snapshot_params_match_capture():
    req = market.SnapshotRequest(
        security_code="600519", market_type=101, date=20260424)
    assert req.to_params() == CAPTURED_SNAPSHOT


def test_id_lives_in_headers_not_toplevel():
    """`id` 在 headers 里。原先写在顶层，静态扫 .rodata 认错了位置。"""
    raw = json.loads(market.build_request(
        "ReqGetKline", CAPTURED_KLINE, 7, username="u", token="t"))
    assert raw["headers"]["id"] == 7
    assert "id" not in raw
    assert raw["method"] == "ReqGetKline"


def test_day_period_is_not_mddatatype_kdaykline():
    """日线走 10100 档，不是 MDDatatype 的 10008。

    10008 填上去服务端恒定无回包——这是行情族拿不到数据的直接原因。
    """
    assert market.PERIOD_TYPE["day"] == 10100
    assert market.PERIOD_TYPE["day"] != 10008
    assert market.PERIOD_TYPE["min1"] == 10000      # 分钟线仍用 10000 档


def test_kline_time_defaults_are_nonzero():
    """日线的 begin_time/end_time 是 900/1700，不是 0。填 0 服务端不回包。"""
    req = market.KlineRequest(
        security_code="600519", market_type=101,
        period_type=market.PERIOD_TYPE["day"],
        begin_date=20260420, end_date=20260424)
    assert req.begin_time == 900 and req.end_time == 1700


def test_snapshot_time_scale_differs_from_kline():
    """快照的时间字段比 K 线大十万倍，别想当然沿用。"""
    assert market.SnapshotRequest("600519", 101, 20260424).begin_time == 90000000
    assert market.SnapshotRequest("600519", 101, 20260424).begin_time != 900


def test_market_and_info_use_different_paths():
    from tgwpy import client

    assert client.PATH_MARKET == "/amd/dgw/dgw"
    assert client.PATH_QUERY_INFO == "/amd/dgw/"


# --- CSV 记录解码 -----------------------------------------------------------

# 抓包拿到的 5 根日线原始 CSV（取前两根）
RAW_ROWS = [
    "600519,101,20260420,1403000000,1413940000,1400000000,1410890000,"
    "364513900,512714604145000",
    "600519,101,20260421,1411000000,1419900000,1410000000,1412010000,"
    "217542700,307459519172000",
]
# 参照实现 CLI 对同一批数据输出的数值
EXPECTED = [
    {"security_code": "600519", "market_type": 101, "kline_time": "2026-04-20",
     "open": 1403.0, "high": 1413.94, "low": 1400.0, "close": 1410.89,
     "volume": 3645139.0, "amount": 5127146041.45},
    {"security_code": "600519", "market_type": 101, "kline_time": "2026-04-21",
     "open": 1411.0, "high": 1419.9, "low": 1410.0, "close": 1412.01,
     "volume": 2175427.0, "amount": 3074595191.72},
]


def test_decode_kline_rows_matches_reference_values():
    """按位还原后要与参照实现的输出逐项相等。"""
    assert market.decode_kline_rows(RAW_ROWS) == EXPECTED


def test_decode_kline_rows_skips_malformed():
    """字段数不对就跳过，不要硬凑。"""
    assert market.decode_kline_rows(["1,2,3", "", None]) == []


def test_decode_kline_rows_ignores_non_string():
    assert market.decode_kline_rows([{"a": 1}]) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --- 快照 CSV 解码 -----------------------------------------------------------

# 抓包拿到的快照行：首行（全 0，盘前）与末行（有真实买卖档）
SNAP_FIRST = (
    "600519,101,20260424090025000,S 10,1419000000,0,0,0,0,0,"
    "0|0|0|0|0|0|0|0|0|0,0|0|0|0|0|0|0|0|0|0,0|0|0|0|0|0|0|0|0|0,0|0|0|0|0|0|0|0|0|0,"
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
# 参照实现 query_snapshot 的末行，35 列
SNAP_LAST_REFERENCE = {
    "code": "600519.SH", "trade_time": "2026-04-24 16:29:55",
    "pre_close": 1419.0, "last": 1458.49, "open": 1413.1, "high": 1458.88,
    "low": 1413.1, "close": 1458.49, "volume": 5545531.0,
    "amount": 8003410999.07, "num_trades": 42029,
    "high_limited": 1560.9, "low_limited": 1277.1,
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
    "iopv": 0.0, "trading_phase_code": "E110",
}


def test_snapshot_rows_decode_all_reference_columns():
    """末行 34 个参照列（code 另拼）必须逐项相等。"""
    rec = market.decode_snapshot_rows([SNAP_LAST])[0]
    for name, want in SNAP_LAST_REFERENCE.items():
        if name == "code":
            continue
        got = rec[name]
        if isinstance(want, float):
            assert abs(got - want) < 1e-6, f"{name}: {got} != {want}"
        else:
            assert got == want, f"{name}: {got!r} != {want!r}"


def test_snapshot_code_is_string_not_int():
    """600519 必须留成字符串，转 int 就废了。"""
    rec = market.decode_snapshot_rows([SNAP_LAST])[0]
    assert rec["security_code"] == "600519"
    assert isinstance(rec["security_code"], str)
    assert rec["market_type"] == 101


def test_snapshot_trade_time_formatting():
    rec = market.decode_snapshot_rows([SNAP_FIRST])[0]
    assert rec["trade_time"] == "2026-04-24 09:00:25"
    assert rec["trading_phase_code"] == "S 10"


def test_snapshot_has_ten_levels_exposes_five():
    """线上给 10 档，参照实现的 DataFrame 只暴露 5 档。"""
    rec = market.decode_snapshot_rows([SNAP_LAST])[0]
    assert len(rec["bid_prices"]) == market.SNAPSHOT_LEVELS == 10
    assert rec["bid_price5"] == 1458.44
    assert not any(k.startswith("bid_price6") for k in rec)


def test_snapshot_unnamed_fields_are_marked():
    """线上多出的列不猜名字，明确标成 _fNN。"""
    rec = market.decode_snapshot_rows([SNAP_LAST])[0]
    assert rec["_f20"] == 35.847
    assert rec["_f24"] is None          # 恒空的列
