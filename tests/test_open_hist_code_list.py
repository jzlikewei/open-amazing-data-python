"""`BaseData.get_hist_code_list` 在某天没有落盘时的行为。

逐交易日下载历史代码表，回包为空的那天不落 pkl。厂商 1.1.15 的处理在循环里
（`base_data.pyc` 偏移 1640 起，except 之后跳回循环头）：缺的是区间最后一天，
就拿当天的实时代码表补上；否则打印那个 `FileNotFoundError` 并跳过这天。自研包
照此实现，这些用例钉住它。

这不是缺陷修正：还原源码曾把这段 except 印到循环外，看上去像「吞掉异常返回
None」，实测 .pyc 返回的是列表。
"""

from __future__ import annotations

import pytest
from open_harness import open_sdk, request_params

CALENDAR_FUNCTION_ID = "A010061003"
HIST_FUNCTION_ID = "A010010009_NEW"

#: 查询区间的五个交易日。`get_hist_code_list` 只取日历上落在闭区间里的日子。
DAYS = ("20250303", "20250304", "20250305", "20250306", "20250307")
START, END = int(DAYS[0]), int(DAYS[-1])

#: 每个非空交易日都回这几只，都是沪深 A 股（`EXTRA_STOCK_A_SH_SZ` 命中）。
CODES = ("600519.SH", "000858.SZ", "300750.SZ")


@pytest.fixture
def 隔离的数据根(tmp_path, monkeypatch):
    """日历的磁盘缓存跟着 `TGW_LOCAL_DATA_ROOT` 走，别写进用户目录。"""
    monkeypatch.setenv("TGW_LOCAL_DATA_ROOT", str(tmp_path) + "/")
    monkeypatch.setenv("TGW_CALENDAR_FIRST_DAY", "20250101")
    import OpenAmazingData.base as base

    base._calendar_cache.clear()
    yield tmp_path
    base._calendar_cache.clear()


def _回包(state: dict):
    """假网关。`state['empty']` 里的日子回「成功、零行」，其余日子回 CODES。"""
    def reply(request):
        params = request_params(request)
        function_id = params.get("function_id")
        if function_id == CALENDAR_FUNCTION_ID:
            days = [day for day in DAYS
                    if params["start_date"] <= day <= params["end_date"]]
            if not days:
                return None, "status=-100"     # 这段区间没有交易日
            return [{"TRADE_DAYS": day} for day in days], 0
        if function_id == HIST_FUNCTION_ID:
            if params["start_date"] in state["empty"]:
                return [], 0                  # 成功、零行：当天没有代码
            return [{"MARKET_CODE": code, "SECURITY_NAME": code} for code in CODES], 0
        return None

    return reply


def _历史代码表(empty_days, data_root):
    """按给定的空回包日集合查一次，返回 `(结果, 落盘的 pkl 文件名)`。"""
    with open_sdk(_回包({"empty": set(empty_days)})) as (ad, gateway):
        base = ad.BaseData()
        result = base.get_hist_code_list("EXTRA_STOCK_A_SH_SZ", START, END,
                                         str(data_root) + "/")
        assert not gateway.unfixtured
    folder = data_root / "basedata" / "hist_code_list"
    landed = tuple(sorted(p.name for p in folder.glob("*.pkl"))) if folder.is_dir() else ()
    return result, landed


def test_某天回包为空时跳过那天照常返回列表(隔离的数据根):
    result, landed = _历史代码表({DAYS[2]}, 隔离的数据根)

    assert isinstance(result, list)
    assert sorted(result) == sorted(CODES)
    # 缺的那天没有落盘，其余四天照落。
    assert f"{DAYS[2]}.pkl" not in landed
    assert len(landed) == len(DAYS) - 1


def test_本地已有部分日期时新缺的一天照样跳过(隔离的数据根):
    """第二次查询区间变宽：本地有的日子直接读，新增的那天为空就不落盘。"""
    state = {"empty": set()}

    with open_sdk(_回包(state)) as (ad, gateway):
        base = ad.BaseData()
        root = str(隔离的数据根) + "/"
        first = base.get_hist_code_list("EXTRA_STOCK_A_SH_SZ", START,
                                        int(DAYS[1]), root)
        state["empty"] = {DAYS[3]}
        gateway.requests.clear()
        second = base.get_hist_code_list("EXTRA_STOCK_A_SH_SZ", START, END, root)
        assert not gateway.unfixtured
        asked = [request_params(r)["start_date"] for r in gateway.requests
                 if request_params(r).get("function_id") == HIST_FUNCTION_ID]

    assert sorted(first) == sorted(CODES)
    # 只补下新加的三天，本地有的两天不重下。
    assert sorted(asked) == sorted(DAYS[2:])
    assert isinstance(second, list)
    assert sorted(second) == sorted(CODES)
    folder = 隔离的数据根 / "basedata" / "hist_code_list"
    assert not (folder / f"{DAYS[3]}.pkl").exists()


def test_最后一天没有落盘时拿实时代码表补上(隔离的数据根, monkeypatch):
    """区间内每天都为空：前几天打印后跳过，最后一天并入实时代码表。"""
    import OpenAmazingData.base as base_mod

    asked = []

    def live(self, security_type="EXTRA_STOCK_A_SH_SZ"):
        asked.append(security_type)
        return ["601318.SH"]

    monkeypatch.setattr(base_mod.BaseData, "get_code_list", live)
    result, landed = _历史代码表(DAYS, 隔离的数据根)

    assert landed == ()
    assert asked == ["EXTRA_STOCK_A_SH_SZ"]
    assert result == ["601318.SH"]


def test_没有空回包时并集不变(隔离的数据根):
    """天天有数据时就是各天的并集。"""
    result, landed = _历史代码表((), 隔离的数据根)

    assert len(landed) == len(DAYS)
    assert sorted(result) == sorted(CODES)
