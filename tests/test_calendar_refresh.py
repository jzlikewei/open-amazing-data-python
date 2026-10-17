"""U3：交易日历按需、增量刷新；`-89` 暂时性错误退避重试。

背景（下游集成方反馈 U3）：磁盘缓存只存到最近一个交易日，旧判据是「缓存末日
早于今天就不新鲜」，于是周末、节假日的每次调用和每天第一次调用都整份往回拼到
1990 年（约 12 次请求）；`_fetch_window` 对 `-89` 不重试，2026-10-17（周六）实测
7 次拼接 4 次失败。

本文件随公开仓发布：只用 `open_harness` 的内联假网关。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from open_harness import open_sdk, request_params

CALENDAR_FID = "A010061003"
FIRST_DAY = "20240101"


def _weekdays(begin: str, end: str) -> list[int]:
    day = dt.datetime.strptime(begin, "%Y%m%d").date()
    stop = dt.datetime.strptime(end, "%Y%m%d").date()
    out = []
    while day <= stop:
        if day.weekday() < 5:
            out.append(int(day.strftime("%Y%m%d")))
        day += dt.timedelta(days=1)
    return out


class _Calendar:
    """假网关的日历接口。`fail_first` 次回 -89，之后正常。"""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_left = fail_first
        self.windows: list[tuple[str, str]] = []

    def __call__(self, request: dict):
        params = request_params(request)
        if params.get("function_id") != CALENDAR_FID:
            return None
        self.windows.append((params["start_date"], params["end_date"]))
        if self.fail_left:
            self.fail_left -= 1
            return None, "status=-89"
        days = _weekdays(params["start_date"], params["end_date"])
        return ([{"TRADE_DAYS": d} for d in days], 0) if days else (None, "status=-100")


@pytest.fixture
def cal_env(tmp_path: Path, monkeypatch):
    import OpenAmazingData.base as base

    monkeypatch.setenv("TGW_LOCAL_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("TGW_CALENDAR_FIRST_DAY", FIRST_DAY)
    monkeypatch.setenv("TGW_BACKOFF_BASE", "0")
    base._calendar_cache.clear()
    yield tmp_path
    base._calendar_cache.clear()


def _new_process():
    """另起一个进程：进程内缓存清掉，磁盘缓存还在。"""
    import OpenAmazingData.base as base

    base._calendar_cache.clear()


def test_U3_缓存末日是周五_周六只补查一次_再查零次(cal_env):
    friday, saturday = "20261016", "20261017"
    server = _Calendar()
    with open_sdk(server, today=friday) as (ad, _gw):
        ad.BaseData().get_calendar("str", "SH", friday)
    assert server.windows == [(FIRST_DAY, friday)], "第一次没有缓存，整份往回拼到起点"

    _new_process()
    server.windows.clear()
    with open_sdk(server, today=saturday) as (ad, _gw):
        days = ad.BaseData().get_calendar("str", "SH", saturday)
    assert server.windows == [(saturday, saturday)], "只补查缓存之后的那一段"
    assert days[-1] == int(friday)

    _new_process()
    server.windows.clear()
    with open_sdk(server, today=saturday) as (ad, _gw):
        ad.BaseData().get_calendar("str", "SH", saturday)
    assert server.windows == [], "今天已经核实过，零次请求"


def test_U3_查询区间已被缓存覆盖时零次请求(cal_env):
    server = _Calendar()
    with open_sdk(server, today="20261016") as (ad, _gw):
        ad.BaseData().get_calendar("str", "SH", "20261016")
    _new_process()
    server.windows.clear()
    with open_sdk(server, today="20261020") as (ad, _gw):
        days = ad.BaseData().get_calendar("str", "SH", "20250630")
    assert server.windows == []
    assert days[-1] == 20250630


def test_U3_老格式缓存按末日当核实日增量补查(cal_env):
    path = Path(cal_env, "calendar", f"SH-{FIRST_DAY}.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_weekdays(FIRST_DAY, "20261016")))
    server = _Calendar()
    with open_sdk(server, today="20261019") as (ad, _gw):
        days = ad.BaseData().get_calendar("str", "SH", "20261019")
    assert server.windows == [("20261017", "20261019")]
    assert days[-1] == 20261019
    saved = json.loads(path.read_text())
    assert saved["through"] == 20261019


def test_U3_未来的交易日不当作已核实(cal_env):
    """`end` 在今天之后：返回给调用方，但磁盘只记到今天。"""
    server = _Calendar()
    with open_sdk(server, today="20261016") as (ad, _gw):
        days = ad.BaseData().get_calendar("str", "SH", "20261023")
    assert days[-1] == 20261023
    saved = json.loads(Path(cal_env, "calendar", f"SH-{FIRST_DAY}.json").read_text())
    assert saved["through"] == 20261016 and saved["days"][-1] == 20261016


def test_U3_前几次回89之后成功能拿到日历(cal_env):
    server = _Calendar(fail_first=2)
    with open_sdk(server, today="20261016") as (ad, _gw):
        days = ad.BaseData().get_calendar("str", "SH", "20261016")
    assert days[-1] == 20261016
    assert server.windows[0] == server.windows[1] == server.windows[2], "同一窗口重试"


def test_U3_一直回89时按原语义失败(cal_env, monkeypatch):
    from OpenAmazingData.base import IncompleteCalendar

    monkeypatch.setenv("TGW_TRANSIENT_RETRIES", "2")
    server = _Calendar(fail_first=99)
    with open_sdk(server, today="20261016") as (ad, _gw):
        with pytest.raises(IncompleteCalendar) as err:
            ad.BaseData().get_calendar("str", "SH", "20261016")
    assert "查询失败" in str(err.value) and "-89" in str(err.value)
    assert len(server.windows) == 3, "1 次 + 2 次重试"


def test_U3_CLI的行情日历只要到查询的end():
    from tgwpy import cli

    asked = []

    class _Base:
        def get_calendar(self, data_type, market, date):
            asked.append(date)
            return []

    class _AD:
        def BaseData(self):
            return _Base()

        def MarketData(self, calendar):
            return calendar

    cli._market_data(_AD(), 20250630)
    cli._market_data(_AD(), 20991231)
    assert asked == ["20250630", cli.today_str()], "历史区间不必核实到今天；未来截到今天"
