"""行情数据：`MarketData` 的两条历史查询。

`query_kline` / `query_snapshot` 不落盘，只发 `ReqGetKline` /
`ReqGetSnapshot`，再把回包整形成厂商那张表：列名、列序、dtype 全由
`constant.py` 的 pydantic 模型定。价格 /1e6、量 /1e2、额 /1e5，K 线时间
按周期解析（日线以上 8 位、分钟线 12 位）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pandas as pd

from tgwpy.compat import structs
from tgwpy.compat import tgw_module as tgw

from ._codes import full_code, split_code
from ._gateway import is_failed
from ._diagnostics import emit_failure, request_context
from .security_type import is_security_type
from .constant import (Kline, Period, Snapshot, SnapshotFuture, SnapshotHKT,
                       SnapshotIndex, SnapshotOption)

# --------------------------------------------------------------------------
# 批量参数：厂商 `environment.py` 的 `QueryPara`
# --------------------------------------------------------------------------

#: 一批并发多少条请求。快照 50、K 线 500。
SNAPSHOT_BATCH = 50
KLINE_BATCH = 500
#: 一批里有失败，重发几轮；仍有失败抛「查询失败」。厂商 `QueryPara.retry_times`。
RETRY_TIMES = 3

#: 一条 K 线请求覆盖多少个交易日，按周期查表。分钟线窗口短、日线以上一整段，
#: 厂商给的是 `QueryPara.req_kline_<period>_date_len`。
KLINE_WINDOW = {
    Period.min1.value: 10,
    Period.min3.value: 30,
    Period.min5.value: 50,
    Period.min10.value: 100,
    Period.min15.value: 120,
    Period.min30.value: 200,
    Period.min60.value: 400,
    Period.min120.value: 800,
    Period.day.value: 1500,
    Period.week.value: 1500,
    Period.month.value: 1500,
    Period.season.value: 1500,
    Period.year.value: 1500,
}

#: 有夜盘的交易所。品种落在这些市场，缺省时窗从 20:00 起。
_NIGHT_MARKETS = (tgw.MarketType.kSHFE, tgw.MarketType.kCFFEX, tgw.MarketType.kDCE,
                  tgw.MarketType.kCZCE, tgw.MarketType.kINE)

#: 缺省时窗。K 线写时分数（9 点整写作 900），快照写时分秒毫秒
#: （9 点整写作 90000000），日盘与夜盘只差起始那一头。
KLINE_SPAN = ((900, 1700), (2000, 1700))
SNAPSHOT_SPAN = ((90_000_000, 170_000_000), (200_000_000, 170_000_000))

#: 行情查询之间串行。厂商所有实例共用 `QueryLock.query_lock` 这一把
#: 可重入锁，这里同样放在模块级。
_QUERY_LOCK = threading.RLock()

# --------------------------------------------------------------------------
# 数值换算
# --------------------------------------------------------------------------

#: 线上报文是整数定点：价格带 6 位小数，量带 2 位，额带 5 位（千元）。
#: 逐项对照 `recovered/AmazingData/utils/convert.py` 的 convert_history_*。
_PRICE = 1_000_000
_VOLUME = 100
_AMOUNT = 100_000

#: 回包的时间戳格式：17 位，末 3 位是毫秒。
_STAMP = "%Y%m%d%H%M%S%f"
#: K 线时间：日线以上 8 位日期，分钟线 12 位到分钟。
_KLINE_STAMP_DATE = "%Y%m%d"
_KLINE_STAMP_MINUTE = "%Y%m%d%H%M"

#: 快照五档盘口。`convert_history_tick_*` 只读 1..5 档，兼容层多给的那一档没人用。
_BOOK_LEVELS = range(1, 6)


def _book(prefix: str) -> dict[str, tuple[str, int]]:
    """五档盘口列：输出名 ← (回包列名, 除数)。`prefix` 取 `ask` / `bid`。"""
    offer = "offer" if prefix == "ask" else "bid"
    columns: dict[str, tuple[str, int]] = {}
    for level in _BOOK_LEVELS:
        columns[f"{prefix}_price{level}"] = (f"{offer}_price{level}", _PRICE)
        columns[f"{prefix}_volume{level}"] = (f"{offer}_volume{level}", _VOLUME)
    return columns


#: 股票 / 债券 / ETF 快照（`convert.py::convert_history_tick_stock`）
_STOCK_FIELDS = {
    "pre_close": ("pre_close_price", _PRICE),
    "last": ("last_price", _PRICE),
    "open": ("open_price", _PRICE),
    "high": ("high_price", _PRICE),
    "low": ("low_price", _PRICE),
    "close": ("close_price", _PRICE),
    "volume": ("total_volume_trade", _VOLUME),
    "amount": ("total_value_trade", _AMOUNT),
    "num_trades": ("num_trades", 1),
    "high_limited": ("high_limited", _PRICE),
    "low_limited": ("low_limited", _PRICE),
    "iopv": ("IOPV", _PRICE),
    **_book("ask"),
    **_book("bid"),
}
#: `num_trades` 走「原样拷贝」而不是除以 1：厂商直接赋值，dtype 留在整数，
#: 过一次 `div` 就变成 float64 了。
_STOCK_COPIED = ("num_trades", "trading_phase_code")

#: 指数快照（`convert.py::convert_history_tick_index`）：没有盘口、没有涨跌停价。
_INDEX_FIELDS = {
    "last": ("last_index", _PRICE),
    "pre_close": ("pre_close_index", _PRICE),
    "open": ("open_index", _PRICE),
    "high": ("high_index", _PRICE),
    "low": ("low_index", _PRICE),
    "close": ("close_index", _PRICE),
    "volume": ("total_volume_trade", _VOLUME),
    "amount": ("total_value_trade", _AMOUNT),
}
_INDEX_COPIED = ("trading_phase_code",)

#: 港股通快照（`convert.py::convert_history_tick_stock_HKT`）：没有开收盘价，
#: 多四个限价字段。
_HKT_FIELDS = {
    "pre_close": ("pre_close_price", _PRICE),
    "nominal_price": ("nominal_price", _PRICE),
    "ref_price": ("ref_price", _PRICE),
    "bid_price_limit_up": ("bid_price_limit_up", _PRICE),
    "bid_price_limit_down": ("bid_price_limit_down", _PRICE),
    "offer_price_limit_up": ("offer_price_limit_up", _PRICE),
    "offer_price_limit_down": ("offer_price_limit_down", _PRICE),
    "last": ("last_price", _PRICE),
    "high": ("high_price", _PRICE),
    "low": ("low_price", _PRICE),
    "volume": ("total_volume_trade", _VOLUME),
    "amount": ("total_value_trade", _AMOUNT),
    "high_limited": ("high_limited", _PRICE),
    "low_limited": ("low_limited", _PRICE),
    **_book("ask"),
    **_book("bid"),
}
_HKT_COPIED = ("trading_phase_code",)

#: 期货快照（`convert.py::convert_history_tick_future`）
_FUTURE_FIELDS = {
    "pre_close": ("pre_close_price", _PRICE),
    "last": ("last_price", _PRICE),
    "open": ("open_price", _PRICE),
    "high": ("high_price", _PRICE),
    "low": ("low_price", _PRICE),
    "close": ("close_price", _PRICE),
    "settle": ("settle_price", _PRICE),
    "average_price": ("average_price", _PRICE),
    "pre_settle": ("pre_settle_price", _PRICE),
    "high_limited": ("high_limited", _PRICE),
    "low_limited": ("low_limited", _PRICE),
    "volume": ("total_volume_trade", _VOLUME),
    "amount": ("total_value_trade", _AMOUNT),
    "pre_open_interest": ("pre_open_interest", _VOLUME),
    "open_interest": ("open_interest", _VOLUME),
    **_book("ask"),
    **_book("bid"),
}
_FUTURE_COPIED = ("action_day", "trading_day")

#: 期权快照（`convert.py::convert_history_tick_option`）
_OPTION_FIELDS = {
    "pre_close": ("pre_close_price", _PRICE),
    "last": ("last_price", _PRICE),
    "open": ("open_price", _PRICE),
    "high": ("high_price", _PRICE),
    "low": ("low_price", _PRICE),
    "close": ("close_price", _PRICE),
    "settle": ("settle_price", _PRICE),
    "pre_settle": ("pre_settle_price", _PRICE),
    "ref_price": ("ref_price", _PRICE),
    "high_limited": ("high_limited", _PRICE),
    "low_limited": ("low_limited", _PRICE),
    "auction_price": ("auction_price", _PRICE),
    "exercise_price": ("exercise_price", _PRICE),
    "volume": ("total_volume_trade", _VOLUME),
    "amount": ("total_value_trade", _AMOUNT),
    "auction_volume": ("auction_volume", _VOLUME),
    "total_long_position": ("total_long_position", _VOLUME),
    **_book("ask"),
    **_book("bid"),
}
_OPTION_COPIED = ("contract_type", "expire_date", "underlying_security_code",
                  "trading_phase_code")

# --------------------------------------------------------------------------
# 回包整形
# --------------------------------------------------------------------------


def _reshape(code: str, frame: pd.DataFrame, model, fields: dict[str, tuple[str, int]],
             copied: tuple[str, ...], *, floor_second: bool) -> pd.DataFrame:
    """回包 DataFrame → 厂商那张表。列序取模型字段序。

    `floor_second` 对齐厂商的差别：股票 / 指数 / 港股通的时间戳截到秒，
    期货 / 期权不截（`convert.py` 里后两者少一个 `.dt.floor('s')`）。
    """
    out = pd.DataFrame(columns=list(model.model_fields))
    out["code"] = [code] * frame.shape[0]
    stamp = pd.to_datetime(frame["orig_time"].astype(str), format=_STAMP)
    out["trade_time"] = stamp.dt.floor("s") if floor_second else stamp
    for name, (column, divisor) in fields.items():
        out[name] = frame[column].div(divisor)
    for name in copied:
        out[name] = frame[name]
    return out


def _kline_time_format(period: int) -> str:
    """日线以上按 8 位日期解析，分钟线按 12 位到分钟解析。"""
    if period in (Period.day.value, Period.week.value, Period.month.value,
                  Period.season.value, Period.year.value):
        return _KLINE_STAMP_DATE
    return _KLINE_STAMP_MINUTE


def _kline_frame(code: str, period: int, parts: list[pd.DataFrame]) -> pd.DataFrame:
    """一批回包拼成一张表：按 K 线时间升序，index 重排。"""
    merged = (pd.concat(parts).sort_values(by="kline_time", ascending=True)
              .reset_index(drop=True))
    out = pd.DataFrame(columns=list(Kline.model_fields))
    out["code"] = [code] * merged.shape[0]
    out["kline_time"] = pd.to_datetime(merged["kline_time"].astype(str),
                                       format=_kline_time_format(period))
    out["open"] = merged["open_price"].div(_PRICE)
    out["high"] = merged["high_price"].div(_PRICE)
    out["low"] = merged["low_price"].div(_PRICE)
    out["close"] = merged["close_price"].div(_PRICE)
    # 量是「手」，整手报价下换算后取整；厂商的 `.astype(int)` 在此。
    out["volume"] = merged["volume_trade"].div(_VOLUME).astype(int)
    out["amount"] = merged["value_trade"].div(_AMOUNT)
    return out


def _stock_frame(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    return _reshape(code, frame, Snapshot, _STOCK_FIELDS, _STOCK_COPIED,
                    floor_second=True)


def _index_frame(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    return _reshape(code, frame, SnapshotIndex, _INDEX_FIELDS, _INDEX_COPIED,
                    floor_second=True)


def _hkt_frame(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    return _reshape(code, frame, SnapshotHKT, _HKT_FIELDS, _HKT_COPIED,
                    floor_second=True)


def _future_frame(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    return _reshape(code, frame, SnapshotFuture, _FUTURE_FIELDS, _FUTURE_COPIED,
                    floor_second=False)


def _option_frame(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    return _reshape(code, frame, SnapshotOption, _OPTION_FIELDS, _OPTION_COPIED,
                    floor_second=False)


#: 快照的换算表按品种选，顺序照厂商：指数、港股通、期货、期权，其余走股票。
_SNAPSHOT_CONVERTERS: tuple[tuple[str, Callable[[str, pd.DataFrame], pd.DataFrame]], ...] = (
    ("EXTRA_INDEX_A", _index_frame),
    ("EXTRA_HKT", _hkt_frame),
    ("EXTRA_FUTURE", _future_frame),
    ("EXTRA_OP", _option_frame),
)


def _convert_snapshot(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    for security_type, convert in _SNAPSHOT_CONVERTERS:
        if is_security_type(code, security_type):
            return convert(code, frame)
    return _stock_frame(code, frame)


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------


def _time_span(kwargs: dict) -> tuple[Any, Any]:
    """`begin_time` / `end_time` 必须成对给，只给一个按没给算。"""
    if "begin_time" in kwargs and "end_time" in kwargs:
        return kwargs["begin_time"], kwargs["end_time"]
    return None, None


def _time_window(begin_time: Any, end_time: Any, market: int,
                 spans: tuple[tuple[int, int], tuple[int, int]]) -> tuple[Any, Any]:
    """这条代码的时窗：调用方给了就用给的，没给按它自己的交易所取缺省值。

    V10 修正。厂商把「时窗没给」这个判断写在代码循环里
    （`recovered/AmazingData/query_api/market_data.py` 的
    `if begin_time is None and end_time is None`，`query_kline` 在偏移 173、
    `query_snapshot` 在偏移 40），判断一次之后 `begin_time` / `end_time` 就
    不再是 `None`，同一次调用里后面的代码一律沿用第一个代码的时窗。
    探针在厂商 .pyc 上实测：`query_kline` 查
    `["600519.SH", "IF2512.CFE"]` 两条请求的 `begin_time` 都是 `900`，
    查 `["IF2512.CFE", "600519.SH"]` 两条都是 `2000`；`query_snapshot`
    同样，`90000000` / `200000000`。
    """
    if begin_time is None and end_time is None:
        day, night = spans
        return night if market in _NIGHT_MARKETS else day
    return begin_time, end_time


def _kline_request(market: int, security_code: str, period: int, window: list[int],
                   begin_time: Any, end_time: Any) -> structs.ReqKline:
    req = structs.ReqKline()
    req.cq_flag = 0
    req.auto_complete = 1
    req.cyc_type = period
    req.begin_date = window[0]
    req.end_date = window[-1]
    req.begin_time = begin_time
    req.end_time = end_time
    req.market_type = market
    req.security_code = security_code
    return req


def _snapshot_request(market: int, security_code: str, date: int,
                      begin_time: Any, end_time: Any) -> structs.ReqDefault:
    req = structs.ReqDefault()
    req.market_type = market
    req.date = date
    req.begin_time = begin_time
    req.end_time = end_time
    req.security_code = security_code
    return req


# --------------------------------------------------------------------------
# 一批请求的收发
# --------------------------------------------------------------------------


def _send_round(requests: list, query, keep, *, attempt: int = 1) -> list:
    """发一批请求，等齐回包，回包交给 `keep` 收，返回失败的请求。

    `keep(req, data)` 在锁内调用：K 线把每张表追加到代码名下，快照把表
    写进 `(日期, 代码)` 的槽位。判失败的规则与资讯族同源，见
    `_gateway.is_failed`。
    """
    done = threading.Event()
    lock = threading.Lock()
    left = [len(requests)]
    failed: list = []

    def on_response(req) -> Callable[[Any, Any], None]:
        def callback(data: Any, status: Any) -> None:
            with lock:
                if data is not None:
                    keep(req, data)
                if is_failed(status):
                    failed.append(req)
                    emit_failure("query_failed", method=getattr(query, "__name__", "market"),
                                 attempt=attempt, max_attempts=RETRY_TIMES + 1, status=status,
                                 **request_context(req))
                left[0] -= 1
                if left[0] == 0:
                    done.set()
        return callback

    for req in requests:
        query(req, on_response(req))
    done.wait()
    return failed


def _send_batch(requests: list, query, keep) -> None:
    """发一批，失败的最多重发 `RETRY_TIMES` 轮；仍有失败抛「查询失败」。

    抛的是 `Exception` 本身而不是自定义子类：异常类型是暴露面的一部分，
    门禁记的是 `type(exc).__name__`。
    """
    failed = _send_round(requests, query, keep)
    for i in range(RETRY_TIMES):
        if not failed:
            break
        print("retry ", str(i))
        failed = _send_round(failed, query, keep, attempt=i + 2)
    if failed:
        raise Exception("查询失败")


def _split_batches(requests: list, size: int):
    return [requests[i:i + size] for i in range(0, len(requests), size)]


# --------------------------------------------------------------------------
# 对外方法
# --------------------------------------------------------------------------


class MarketData:
    """行情查询。日历是 8 位交易日的一列（`BaseData.get_calendar` 的返回）。"""

    def __init__(self, calendar):
        self.calendar = calendar
        self.last_kline_report = None

    def query_snapshot(self, code_list, begin_date, end_date, **kwargs):
        """历史 level-1 快照，返回 `{日期: {代码: DataFrame}}`。

        逐日逐码各一条请求，没有数据的槽位是 `None`。
        """
        begin_time, end_time = _time_span(kwargs)
        with _QUERY_LOCK:
            days = [d for d in self.calendar if begin_date <= d <= end_date]
            requests = []
            for day in days:
                for code in code_list:
                    market, security_code = split_code(code)
                    begin, end = _time_window(begin_time, end_time, market,
                                              SNAPSHOT_SPAN)
                    requests.append(_snapshot_request(market, security_code, day,
                                                      begin, end))
            result: dict = {}
            for batch in _split_batches(requests, SNAPSHOT_BATCH):
                slots = {(req.date, full_code(req.market_type, req.security_code)): None
                         for req in batch}

                # 回包在线程上回来，闭包按默认参数绑住本批的 `slots`。
                def keep(req, data, slots=slots):
                    slots[(req.date, full_code(req.market_type, req.security_code))] = data

                _send_batch(batch, tgw.QuerySnapshot, keep)
                for (day, code), frame in slots.items():
                    result.setdefault(day, {})[code] = frame
        for day in result:
            for code, frame in result[day].items():
                if frame is not None:
                    result[day][code] = _convert_snapshot(code, frame)
        return result

    def query_kline(self, code_list, begin_date=20240101, end_date=20991231,
                    period=Period.min1.value, **kwargs):
        """历史 K 线，返回 `{代码: DataFrame}`。没有数据的代码是 `None`。"""
        begin_time, end_time = _time_span(kwargs)
        with _QUERY_LOCK:
            days = [d for d in self.calendar if begin_date <= d <= end_date]
            if period == Period.day.value and days:
                from ._kline_recovery import query_daily
                return query_daily(self, code_list, days, period, begin_time, end_time)
            self.last_kline_report = None
            # 周期不在表里时厂商的窗口长度是 None，`range` 直接抛 TypeError。
            window = KLINE_WINDOW.get(period)
            windows = [days[i:i + window] for i in range(0, len(days), window)]
            requests = []
            for code in code_list:
                market, security_code = split_code(code)
                begin, end = _time_window(begin_time, end_time, market, KLINE_SPAN)
                for chunk in windows:
                    requests.append(_kline_request(market, security_code, period,
                                                   chunk, begin, end))
            result: dict = {}
            for batch in _split_batches(requests, KLINE_BATCH):
                parts: dict[str, list] = {}
                for req in batch:
                    parts.setdefault(full_code(req.market_type, req.security_code), [])

                def keep(req, data, parts=parts):
                    parts[full_code(req.market_type, req.security_code)].append(data)

                _send_batch(batch, tgw.QueryKline, keep)
                for code, frames in parts.items():
                    result.setdefault(code, []).extend(frames)
        for code in result:
            frames = result[code]
            result[code] = _kline_frame(code, period, frames) if frames else None
        return result
