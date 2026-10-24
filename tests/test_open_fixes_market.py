"""V10 的正向单元测试：缺省时窗按每个代码自己的交易所取。

厂商 1.1.15 把「时窗没给」这个判断写在代码循环里，判断一次之后
`begin_time` / `end_time` 就不再是 `None`，同一次调用里后面的代码一律沿用
第一个代码的时窗。混查股票与期货时，期货也拿日盘时窗（反过来一样）。自研包
改成每个代码各取各的，调用方显式给了时窗则照用。
"""

from __future__ import annotations

import contextlib

from open_harness import open_sdk, request_params

#: 日盘市场（SSE）与夜盘市场（CFFEX）。夜盘市场还有 SHFE / DCE / CZCE / INE。
STOCK, FUTURE = "600519.SH", "IF2512.CFE"

#: 一个三天的短日历。日线窗口 1500 天，三天只切出一批。
CALENDAR = [20251201, 20251202, 20251203]
FIRST_DAY, LAST_DAY = CALENDAR[0], CALENDAR[-1]
#: 调用方显式给的时窗。K 线写时分数，快照写时分秒毫秒。
GIVEN_KLINE, GIVEN_SNAPSHOT = (915, 1530), (91500000, 153000000)

#: 线上 `ReqGetSnapshot` 的回包是 36 段 CSV，末段空（行尾逗号）。
_LEVELS = "|".join(["0"] * 10)


def kline_row(code: str, day: int) -> str:
    """一条日线：裸码、市场码、8 位日期、四价、量、额。"""
    return f"{code},101,{day},1403000000,1413940000,1400000000,1410890000,364513900,512714604145000"


def snapshot_row(code: str, day: int) -> str:
    """一条快照。每只代码固定回一行，时窗落在哪一段都返回这一行——这组测试
    盯的是发出去的请求，不是回包。"""
    fields = [code, "101", f"{day}093000000", "S10",
              "1419000000", "0", "0", "0", "0", "0",
              _LEVELS, _LEVELS, _LEVELS, _LEVELS,
              "0", "0", "0", "0", "1560900000", "1277100000"]
    return ",".join(fields + ["0"] * 15 + [""])


def market_reply(request):
    """行情族的夹具：K 线与快照各回一行，别的接口没有。"""
    params = request_params(request)
    if request.get("method") == "ReqGetKline":
        return [kline_row(params["security_code"], day) for day in CALENDAR
                if int(params["begin_date"]) <= day <= int(params["end_date"])], 0
    if request.get("method") == "ReqGetSnapshot":
        return [snapshot_row(params["security_code"], FIRST_DAY)], 0
    return None


def spans(gateway) -> dict[str, tuple]:
    """发出去的请求按裸码归一份 `(begin_time, end_time)`。`request_params`
    把参数合成字符串表，这里取回整数。"""
    return {p["security_code"]: (int(p["begin_time"]), int(p["end_time"]))
            for p in map(request_params, gateway.requests)}


def ignore_future_snapshot_gap(call):
    """期货快照的回包在替身上换不成 `SnapshotFuture`——`settle_price` 这类列
    线上的 CSV 里没有，转换抛 `KeyError`。请求在转换之前就发出去了，这两条
    快照测试盯的是线上的时窗，不是回包。"""
    with contextlib.suppress(KeyError):
        call()


def test_kline_缺省时窗各按自己的交易所():
    with open_sdk(market_reply) as (ad, gateway):
        market = ad.MarketData(CALENDAR)
        for codes in ([STOCK, FUTURE], [FUTURE, STOCK]):
            gateway.requests.clear()
            market.query_kline(codes, FIRST_DAY, LAST_DAY,
                               ad.constant.Period.day.value)
            assert spans(gateway) == {"600519": (900, 1700), "IF2512": (2000, 1700)}


def test_snapshot_缺省时窗各按自己的交易所():
    with open_sdk(market_reply) as (ad, gateway):
        market = ad.MarketData(CALENDAR)
        for codes in ([STOCK, FUTURE], [FUTURE, STOCK]):
            gateway.requests.clear()
            ignore_future_snapshot_gap(
                lambda: market.query_snapshot(codes, FIRST_DAY, FIRST_DAY))
            assert spans(gateway) == {"600519": (90000000, 170000000),
                                      "IF2512": (200000000, 170000000)}


def test_显式给了时窗就照用():
    with open_sdk(market_reply) as (ad, gateway):
        market = ad.MarketData(CALENDAR)
        market.query_kline([STOCK, FUTURE], FIRST_DAY, LAST_DAY,
                           ad.constant.Period.day.value,
                           begin_time=GIVEN_KLINE[0], end_time=GIVEN_KLINE[1])
        assert spans(gateway) == {"600519": GIVEN_KLINE, "IF2512": GIVEN_KLINE}
        gateway.requests.clear()
        ignore_future_snapshot_gap(
            lambda: market.query_snapshot([STOCK, FUTURE], FIRST_DAY, FIRST_DAY,
                                          begin_time=GIVEN_SNAPSHOT[0],
                                          end_time=GIVEN_SNAPSHOT[1]))
        assert spans(gateway) == {"600519": GIVEN_SNAPSHOT,
                                  "IF2512": GIVEN_SNAPSHOT}
