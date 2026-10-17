"""序列类四个方法的修正（V04、V05、V06、V07）——正向断言修正后的行为。

四条都是厂商 1.1.15 自己的（.pyc 上实测的原文写在每节的 docstring 里）：

* **V04** `get_history_stock_status` 两张表按交易日 left join，两边都有
  `MARKET_CODE`，merge 后成了 `MARKET_CODE_x` / `MARKET_CODE_y`，列清单里的
  `MARKET_CODE` 落到补列上——整列 NaN、dtype float64。自研包填真实代码。
* **V05** `get_index_weight` / `get_industry_weight` 落盘的是排序前的表、返回的
  是倒序排过的表，热跑读本地拿到的顺序与冷跑不同（冷跑 `20251231…`、热跑
  `20240101…`）。自研包落盘排序后的表，冷热顺序一致。
* **V06** 同两个方法，厂商在「网关没给这只代码且本地表为空」时两支各错一半：
  行业权重返回的 dict 里少这个键，指数权重对连列都没有的空本地表排序抛
  `KeyError: 'TRADE_DATE'`。自研包两支都给这只代码留一张列齐全的空表。
* **V07** `get_treasury_yield` 冷跑返回 `set_index('DATE')` 后的表，热跑读本地
  原样返回带 `DATE` 列的表，同一个查询两种形状。自研包读本地时也按冷跑的形状
  返回（落盘的表留着 `DATE` 列，增量起点还靠它）。

本文件随公开仓发布，不 import 厂商发行物，不读接口规范。
"""

from __future__ import annotations

import pandas as pd
import pytest
from open_harness import open_sdk, request_params

# --------------------------------------------------------------------------
# 内联夹具
# --------------------------------------------------------------------------

#: 历史证券状态的两趟：A010060002 给逐日状态，A010060005 只给交易日序列。
STATUS_FID = "A010060002"
TRADE_DAY_FID = "A010060005"
STATUS_CODE = "600519.SH"
#: 状态表里的三个交易日，第四个交易日只在交易日序列里出现（left join 的证据）。
STATUS_DAYS = ("20250331", "20250630", "20250930")
EXTRA_TRADE_DAY = "20251231"
STATUS_COLUMNS = ("MARKET_CODE", "TRADE_DATE", "PRECLOSE", "HIGH_LIMITED", "LOW_LIMITED",
                  "PRICE_HIGH_LMT_RATE", "PRICE_LOW_LMT_RATE", "IS_ST_SEC", "IS_SUSP_SEC",
                  "IS_WD_SEC", "IS_XR_SEC")

#: 方法 → (function_id, 代码参数, 缓存子目录, 服务端列名, 输出列名)
WEIGHTS = {
    "get_index_weight": (
        "A010200005", "index_code", "index_weight", "000300.SH",
        ("INDEX_CODE", "CON_CODE", "TRADE_DATE", "TOTAL_SHR", "FREE_SHR_RATIO",
         "CALC_SHR", "WEIGHT_FACTOR", "WEIGHT", "CLOSE"),
        ("INDEX_CODE", "CON_CODE", "TRADE_DATE", "TOTAL_SHARE", "FREE_SHARE_RATIO",
         "CALC_SHARE", "WEIGHT_FACTOR", "WEIGHT", "CLOSE"),
    ),
    "get_industry_weight": (
        "A010200008", "index_code", "industry_weight", "801030.SI",
        ("WEIGHT", "CON_CODE", "TRADE_DATE", "INDEX_CODE"),
        ("WEIGHT", "CON_CODE", "TRADE_DATE", "INDEX_CODE"),
    ),
}

TREASURY_FID = "A010050009"
TREASURY_CODE = "y1"
TREASURY_DAYS = ("20250331", "20250630", "20250930")
NO_DATA_OK = (None, 0)   # 网关回「成功、无数据」：不是失败，只是什么都没给


def _status_reply(request):
    """状态表按日期给 PRECLOSE，交易日序列多给一天，两张表对不上的那行留给 left join。"""
    params = request_params(request)
    function_id = params.get("function_id")
    if function_id == STATUS_FID:
        rows = [{"MARKET_CODE": params["market_code"], "TRADE_DATE": day,
                 "PRECLOSE": float(i + 1), "IS_ST_SEC": 0, "IS_SUSP_SEC": 0,
                 "IS_WD_SEC": 0, "IS_XR_SEC": 0}
                for i, day in enumerate(STATUS_DAYS)]
        return rows, 0
    if function_id == TRADE_DAY_FID:
        days = STATUS_DAYS + (EXTRA_TRADE_DAY,)
        return [{"MARKET_CODE": params["market_code"], "TRD_DATE": day} for day in days], 0
    return None


def _weight_reply(request, empty=False):
    """每个日期窗口回两行（窗口首尾），行序与日期都随窗口变，倒不倒序看得出来。"""
    params = request_params(request)
    for fid, code_param, _dir, _code, server_columns, _out_columns in WEIGHTS.values():
        if params.get("function_id") != fid:
            continue
        if empty:
            return NO_DATA_OK
        rows = []
        for day in (params["start_date"], params["end_date"]):
            row = {column: 1.5 for column in server_columns}
            row["TRADE_DATE"] = day
            row[code_param] = params[code_param]
            row["CON_CODE"] = "600519.SH"
            rows.append(row)
        return rows, 0
    return None


def _treasury_reply(request):
    params = request_params(request)
    if params.get("function_id") != TREASURY_FID:
        return None
    return [{"INDI_TYPE": params["indi_type"], "INDI_NUM": 1.25, "DDLN_DATE": day}
            for day in TREASURY_DAYS], 0


def _cache_path(tmp_path, sub_dir, code):
    return tmp_path / "infodata" / sub_dir / f"{code}.h5"


# --------------------------------------------------------------------------
# V04：历史证券状态的 MARKET_CODE 填真实代码
# --------------------------------------------------------------------------


def test_V04_MARKET_CODE是真实代码不是NaN(tmp_path):
    """.pyc 实测：dtype float64，60 行全 NaN——两表都有 MARKET_CODE，merge 后成了
    MARKET_CODE_x / MARKET_CODE_y，列清单里的 MARKET_CODE 落到补列上。"""
    root = str(tmp_path) + "/"
    with open_sdk(_status_reply) as (ad, gateway):
        out = ad.InfoData().get_history_stock_status([STATUS_CODE], root, False)
    frame = out[STATUS_CODE]
    assert list(frame.columns) == list(STATUS_COLUMNS)
    assert list(frame["MARKET_CODE"]) == [STATUS_CODE] * len(frame)
    assert bool(frame["MARKET_CODE"].notna().all())
    assert gateway.unfixtured == []


def test_V04_其余列与左连接的行序不变(tmp_path):
    """修的只是 MARKET_CODE 一列：只有交易日、没状态行的那天照样留下，状态列是 NaN。"""
    root = str(tmp_path) + "/"
    with open_sdk(_status_reply) as (ad, _gateway):
        frame = ad.InfoData().get_history_stock_status([STATUS_CODE], root, False)[STATUS_CODE]
    assert len(frame) == len(STATUS_DAYS) + 1, "left join：多出来的那一天也要留下"
    assert list(frame["TRADE_DATE"])[:3] == list(reversed(STATUS_DAYS))
    preclose = dict(zip(frame["TRADE_DATE"], frame["PRECLOSE"]))
    assert [preclose[day] for day in reversed(STATUS_DAYS)] == [3.0, 2.0, 1.0]
    assert list(frame["PRECLOSE"].isna()) == [False, False, False, True]
    assert list(frame["MARKET_CODE"]) == [STATUS_CODE] * 4


# --------------------------------------------------------------------------
# V05：命中缓存与否返回同一个顺序
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", list(WEIGHTS))
def test_V05_冷跑与热跑返回同一个顺序(tmp_path, method):
    """.pyc 实测：冷跑 `20251231…`、热跑 `20240101…`——厂商落盘的是排序前那张表。"""
    fid, code_param, sub_dir, code, server_columns, out_columns = WEIGHTS[method]
    root = str(tmp_path) + "/"
    with open_sdk(_weight_reply) as (ad, gateway):
        cold = getattr(ad.InfoData(), method)([code], root, False)
        warm = getattr(ad.InfoData(), method)([code], root, True)
    assert not gateway.unfixtured
    assert list(cold[code].columns) == list(out_columns)
    dates = list(cold[code]["TRADE_DATE"])
    assert len(dates) > 2, "夹具要能分得出倒序还是升序"
    assert dates == sorted(dates, reverse=True)
    assert list(warm[code]["TRADE_DATE"]) == dates
    assert list(warm[code].columns) == list(out_columns)


@pytest.mark.parametrize("method", list(WEIGHTS))
def test_V05_落盘的表是排序后的(tmp_path, method):
    """缓存文件名与 key 都不动，只是内容换成了倒序那张。"""
    fid, code_param, sub_dir, code, server_columns, out_columns = WEIGHTS[method]
    root = str(tmp_path) + "/"
    with open_sdk(_weight_reply) as (ad, _gateway):
        cold = getattr(ad.InfoData(), method)([code], root, False)
    path = _cache_path(tmp_path, sub_dir, code)
    assert path.is_file()
    stored = pd.read_hdf(str(path))
    assert list(stored.columns) == list(out_columns)
    assert list(stored["TRADE_DATE"]) == list(cold[code]["TRADE_DATE"])


# --------------------------------------------------------------------------
# V06：网关没给这只代码时不丢键、不抛错
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", list(WEIGHTS))
def test_V06_网关说成功却没有数据时留空表(tmp_path, method):
    """.pyc 实测：行业权重返回的 dict 里少这个键，指数权重抛
    `KeyError: 'TRADE_DATE'`。自研包两支都给一只列齐全的空表。"""
    fid, code_param, sub_dir, code, server_columns, out_columns = WEIGHTS[method]
    root = str(tmp_path) + "/"
    with open_sdk(lambda r: _weight_reply(r, empty=True)) as (ad, gateway):
        out = getattr(ad.InfoData(), method)([code], root, False)
    assert code in out
    frame = out[code]
    assert frame.empty
    assert list(frame.columns) == list(out_columns)
    assert not _cache_path(tmp_path, sub_dir, code).exists(), "空表不落盘"
    assert gateway.unfixtured == []


@pytest.mark.parametrize("method", list(WEIGHTS))
def test_V06_没有日期窗口可发时留空表(tmp_path, method):
    """区间倒置时厂商一个请求都不发，本地表是空的。厂商在这一支丢掉键、在另一支
    抛 KeyError；自研包统一给空表。修正前这里行业权重返回 `{}`、指数权重抛
    `KeyError: 'TRADE_DATE'`。"""
    fid, code_param, sub_dir, code, server_columns, out_columns = WEIGHTS[method]
    root = str(tmp_path) + "/"
    with open_sdk(_weight_reply) as (ad, gateway):
        out = getattr(ad.InfoData(), method)([code], root, False,
                                            begin_date=20251231, end_date=20250101)
    assert code in out
    assert out[code].empty
    assert list(out[code].columns) == list(out_columns)
    assert gateway.requests == [], "没有窗口就不该发请求"


# --------------------------------------------------------------------------
# V07：国债收益率冷热返回同一个形状
# --------------------------------------------------------------------------


def test_V07_冷跑与热跑返回同一个形状(tmp_path):
    """.pyc 实测：冷跑 `['YIELD']`（index 是 DATE），热跑 `['YIELD', 'DATE']`。"""
    root = str(tmp_path) + "/"
    with open_sdk(_treasury_reply) as (ad, gateway):
        cold = ad.InfoData().get_treasury_yield([TREASURY_CODE], root, False)
        warm = ad.InfoData().get_treasury_yield([TREASURY_CODE], root, True)
    assert not gateway.unfixtured
    assert list(cold[TREASURY_CODE].columns) == ["YIELD"]
    assert list(warm[TREASURY_CODE].columns) == ["YIELD"]
    assert list(warm[TREASURY_CODE].index) == list(cold[TREASURY_CODE].index)
    assert list(cold[TREASURY_CODE].index) == list(reversed(TREASURY_DAYS))
    assert list(warm[TREASURY_CODE]["YIELD"]) == list(cold[TREASURY_CODE]["YIELD"])


def test_V07_落盘的表仍带DATE列(tmp_path):
    """缓存留着 DATE 列：下一次的增量起点还从 `DATE` 的最大值推。"""
    root = str(tmp_path) + "/"
    with open_sdk(_treasury_reply) as (ad, _gateway):
        ad.InfoData().get_treasury_yield([TREASURY_CODE], root, False)
    stored = pd.read_hdf(str(_cache_path(tmp_path, "treasury_yield", TREASURY_CODE)))
    assert list(stored.columns) == ["YIELD", "DATE"]
    assert list(stored["DATE"]) == list(reversed(TREASURY_DAYS))


def test_读到厂商写的升序旧缓存也按日期倒序返回(tmp_path):
    """V05：厂商 SDK 落盘的是排序前（升序）的表，自研包读到时摆成倒序。"""
    import pandas as pd

    folder = tmp_path / "infodata" / "index_weight"
    folder.mkdir(parents=True)
    old = pd.DataFrame({
        "INDEX_CODE": ["000300.SH"] * 3, "CON_CODE": ["600519.SH"] * 3,
        "TRADE_DATE": ["20250101", "20250102", "20250103"],
        "TOTAL_SHARE": 1.0, "FREE_SHARE_RATIO": 1.0, "CALC_SHARE": 1.0,
        "WEIGHT_FACTOR": 1.0, "WEIGHT": 1.0, "CLOSE": 1.0,
    })
    old.to_hdf(str(folder / "000300.SH.h5"), key="000300.SH", mode="w")

    with open_sdk(lambda request: None) as (ad, gateway):
        got = ad.InfoData().get_index_weight(["000300.SH"], str(tmp_path) + "/", True)
    assert not gateway.requests
    assert list(got["000300.SH"]["TRADE_DATE"]) == ["20250103", "20250102", "20250101"]


@pytest.mark.parametrize("method", ["get_index_weight", "get_industry_weight"])
def test_开始日期晚于结束日期时立即返回空表(tmp_path, method):
    """V11：厂商在这里一个请求不发、却去等回包，永不返回。"""
    code = "000300.SH" if method == "get_index_weight" else "801030.SI"
    with open_sdk(lambda request: None) as (ad, gateway):
        got = getattr(ad.InfoData(), method)([code], str(tmp_path) + "/", True,
                                             begin_date=20251231, end_date=20250101)
    assert not gateway.requests
    assert list(got) == [code] and got[code].empty
