"""基础数据：`BaseData`——交易日历、代码表、历史代码表、复权因子、ETF 申赎。

十个方法按「数据从哪来」分四组：

* 交易日历（`get_calendar`）：**按时间窗往回分段拼**，不照搬厂商一次查
  19900101 的写法。服务端单次最多回 1000 行且 `offset` 无效，一次查的结果
  止于 1994 年，日历一短，`get_backward_factor` 拿它去 reindex 因子表就整片
  取空。分段规则与两级缓存是 `tgwpy.patches.patch_calendar` 补丁后的行为，
  等价门禁的 .pyc 一侧套的正是那个补丁。
* 代码表（`get_code_list` / `get_option_code_list` / `get_future_code_list` /
  `get_code_info` / `get_future_code_info`）：`QuerySecuritiesInfo` 同步查每个
  市场，取 `security_code` 拼成带后缀的代码，再按 `security_type` 的正则过滤。
* 历史代码表（`get_hist_code_list`）：按交易日逐日下载，落 `basedata/hist_code_list/<日期>.pkl`。
* 复权因子（`get_backward_factor` / `get_adj_factor`）与 ETF 申赎
  （`get_etf_pcf`）。

厂商自身的怪行为（`is_local=True` 时因子表按 `code_list` 二次取列、
单次因子不 ffill、缺列的 KeyError 照抛）属于暴露面，原样复现，代码里标明。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pickle
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from tgwpy.compat import structs
from tgwpy.compat import tgw_module as tgw

from . import _clock
from . import _gateway as gw
from . import _store as store
from ._codes import SUFFIX_BY_MARKET, full_code, split_code
from ._gateway import RETRY_TIMES, is_failed
from ._local import DEFAULT_LOCAL_PATH, local_root
from .security_type import is_security_type

# --------------------------------------------------------------------------
# 交易日历：分段拼接，绕开服务端的单次行数上限
# --------------------------------------------------------------------------

CALENDAR_FUNCTION_ID = "A010061003"

#: 服务端单次响应的行数上限。实测取满 1000 就停，且 `offset` 无效
#: （`tgwpy.patches` 模块头，探针实发 66 号包）。
SERVER_ROW_LIMIT = 1000

#: 一个窗口几年。A 股一年约 242 个交易日，3 年约 726 行，留足余量。
SEGMENT_YEARS = 3

#: 最多回溯多少个窗口。1990 至今 36 年 / 3 = 12 个，留一倍余量。
MAX_SEGMENTS = 24

#: 日历起点。厂商把 `start_date` 写死成这个值（`get_calendar` 字节码里的
#: `LOAD_CONST '19900101'`），一次查必然被截断，这里只作为回溯终点。
CALENDAR_FIRST_DAY = "19900101"

#: `get_calendar` 的 `market` → 线上取值，抄自厂商自己字节码里的 `market_dict`。
MARKET_WIRE: dict[str, str] = {
    "SZ": "SZSE", "SH": "SSE", "BJ": "NEEQ",
    "SHF": "SHFE", "CFE": "CFFEX", "DCE": "DCE",
    "CZC": "CZCE", "INE": "INE",
    "SHN": "SHHK", "SZN": "SZHK",
}

#: 拼到起点还差 400 天以上就报错。1990-1991 上交所没开市，两年多是正常的；
#: 差得更多说明中间有整段没取到。
GAP_TOLERANCE_DAYS = 400

#: `TGW_CALENDAR_FIRST_DAY` 覆盖回溯起点，省请求用。代价是老区间的 K 线查不到。
FIRST_DAY_ENV = "TGW_CALENDAR_FIRST_DAY"

_calendar_cache: dict[tuple[str, str, str], list[int]] = {}


class IncompleteCalendar(RuntimeError):
    """日历取不全。

    单独立一个类型：批处理要能单独 catch 它并重试或跳过，不跟「网络断了」
    「参数错了」混在一起。
    """


def _first_day() -> str:
    return os.environ.get(FIRST_DAY_ENV) or CALENDAR_FIRST_DAY


def _fetch_window(market_wire: str, start: str, end: str) -> list[int]:
    """一个日期窗口的交易日。空列表表示该窗口无数据。"""
    task_id = tgw.GetTaskID()
    for key, value in (("function_id", CALENDAR_FUNCTION_ID),
                       ("start_date", start), ("end_date", end),
                       ("market", market_wire)):
        tgw.SetThirdInfoParam(task_id, key, value)
    frame, error = tgw.QueryThirdInfo(task_id)
    if error:
        # `status=-100` 是「这段区间没数据」，不是错误：1990 年 1 月上交所
        # 还没开市，头几个窗口空是正常的。参数非法走 301010 且必带中文 msg
        # （2026-10-11 真网关逐条复测），那种要报出来。
        if "-100" in str(error) or "DataEmpty" in str(error):
            return []
        raise IncompleteCalendar(f"交易日历 {start}~{end} 查询失败：{error}")
    if frame is None or frame.empty:
        return []
    return [int(day) for day in frame["TRADE_DAYS"].astype(int)]


def _calendar_path(market: str, first_day: str) -> str:
    """磁盘缓存路径。跟着本地数据根走，日历换根不用改第二处。"""
    return os.path.join(local_root(), "calendar", f"{market}-{first_day}.json")


def _read_calendar_disk(market: str, first_day: str, end: int) -> list[int] | None:
    """读磁盘缓存，够不到 `end` 就返回 None。

    缓存键**不含 end**：日历只往历史里长，写进去的只会比要的新鲜。键里带上
    end 的话每天换一次文件名，缓存等于没有。
    """
    path = _calendar_path(market, first_day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            days = [int(day) for day in json.load(handle)]
    except (OSError, ValueError, TypeError):
        return None      # 缓存坏了就当没有，重拼一遍而已
    if not days or days[-1] < end:
        return None      # 跨天了，缓存不够新鲜，重拼
    return days


def _write_calendar_disk(market: str, first_day: str, days: list[int]) -> None:
    """落盘。写失败不影响主流程——缓存是加速器，不是必需品。"""
    path = _calendar_path(market, first_day)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(days, handle)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"警告：交易日历缓存写不进去（{exc}），下个进程要重拼")


def full_calendar(market: str, end: str, first_day: str) -> list[int]:
    """`first_day` 到 `end` 的完整交易日升序列表。

    **必须一路拼回 `first_day`**：只拼最近一个窗口的话，老日期区间的 K 线
    会静默返回空 dict。窗口取满 `SERVER_ROW_LIMIT` 行说明它被截断，继续往前
    取；走到 `first_day` 或 `MAX_SEGMENTS` 用完为止，后者抛 `IncompleteCalendar`
    ——不把截断当完整。

    两级缓存：进程内按 `(market, end, first_day)` 记（日历在一次进程里会被
    `MarketData` 构造和因子 reindex 反复要），磁盘按 `(market, first_day)` 记
    （分批任务跨进程复用）。
    """
    key = (market, str(end)[:8], first_day)
    cached = _calendar_cache.get(key)
    if cached is not None:
        return cached

    market_wire = MARKET_WIRE.get(market)
    if market_wire is None:
        raise IncompleteCalendar(f"未知市场 {market!r}，可选项 {sorted(MARKET_WIRE)}")
    end_day = dt.datetime.strptime(str(end)[:8], "%Y%m%d").date()
    start_day = dt.datetime.strptime(first_day, "%Y%m%d").date()
    if start_day > end_day:
        return []

    on_disk = _read_calendar_disk(market, first_day, int(end_day.strftime("%Y%m%d")))
    if on_disk is not None:
        _calendar_cache[key] = on_disk
        return on_disk

    days: list[int] = []
    cursor_end = end_day
    for _ in range(MAX_SEGMENTS):
        cursor_start = max(start_day, cursor_end - dt.timedelta(days=365 * SEGMENT_YEARS))
        window = _fetch_window(market_wire, cursor_start.strftime("%Y%m%d"),
                               cursor_end.strftime("%Y%m%d"))
        days.extend(window)
        if cursor_start <= start_day:
            break
        # 窗口没取满说明这个窗口本身是全的，不是「后面没数据了」——继续往前拼。
        cursor_end = cursor_start - dt.timedelta(days=1)
    else:
        raise IncompleteCalendar(
            f"交易日历取不全：已回溯 {MAX_SEGMENTS} 个 {SEGMENT_YEARS} 年窗口，"
            f"仍没走到 {first_day}。服务端单次上限 {SERVER_ROW_LIMIT} 行，"
            f"请调大 MAX_SEGMENTS 或调小 SEGMENT_YEARS。"
        )

    uniq = sorted(set(days))
    if not uniq:
        raise IncompleteCalendar(
            f"交易日历为空：{market} {first_day}~{end}，回溯的窗口都没有结果"
        )
    earliest = dt.datetime.strptime(str(uniq[0]), "%Y%m%d").date()
    if earliest > start_day and (earliest - start_day).days > GAP_TOLERANCE_DAYS:
        # 拼到起点了，最早的交易日还在它之后，中间有整段空窗：要么这个市场
        # 没数据，要么某个窗口被截断了。两种都值得报出来。
        raise IncompleteCalendar(
            f"交易日历最早只到 {uniq[0]}，比要求的 {first_day} 晚了 "
            f"{(earliest - start_day).days} 天，中间疑似整段没取到"
        )
    _calendar_cache[key] = uniq
    _write_calendar_disk(market, first_day, uniq)
    return uniq


# --------------------------------------------------------------------------
# 代码表
# --------------------------------------------------------------------------

#: 沪深北三个股票市场。厂商的遍历序是 SZSE → SSE → NEEQ，返回值按这个序拼。
_STOCK_MARKETS = (tgw.MarketType.kSZSE, tgw.MarketType.kSSE, tgw.MarketType.kNEEQ)

#: 五个期货交易所，遍历序 SHFE → CFFEX → DCE → CZCE → INE。
_FUTURE_MARKETS = (tgw.MarketType.kSHFE, tgw.MarketType.kCFFEX, tgw.MarketType.kDCE,
                   tgw.MarketType.kCZCE, tgw.MarketType.kINE)

#: 港股通三个类型走 `A010010011` 而不是逐市场查代码表。
_HKT_TYPES = ("EXTRA_HKT", "SH_HKT", "SZ_HKT")

#: 期权类型 → 要查的市场。缺省是「所有商品期权 + 股指 + ETF 期权」。
_OPTION_MARKETS: dict[str, tuple] = {
    "EXTRA_ETF_OP": (tgw.MarketType.kSZSE, tgw.MarketType.kSSE),
    "EXTRA_CF_OP": (tgw.MarketType.kSHFE, tgw.MarketType.kDCE,
                    tgw.MarketType.kCZCE, tgw.MarketType.kINE),
    "ZJ_OPTION": (tgw.MarketType.kCFFEX,),
    "EXTRA_INDEX_OP": (tgw.MarketType.kCFFEX, tgw.MarketType.kSZSE,
                       tgw.MarketType.kSSE),
    "EXTRA_OP": (tgw.MarketType.kCFFEX, tgw.MarketType.kSZSE, tgw.MarketType.kSSE,
                 tgw.MarketType.kSHFE, tgw.MarketType.kDCE, tgw.MarketType.kCZCE,
                 tgw.MarketType.kINE),
}

#: 期货合约在代码表里的 `security_type` 取值，代码表方法先按它过一遍。
_FUTURE_ROW_TYPE = "06007"

#: 北交所代码表里 `english_name` 是 20 个空格的行是老代码，只留 920 开头的新码。
_BLANK_ENGLISH = " " * 20

#: `get_code_info` / `get_future_code_info` 的输出列。价格四列都除以 1e6
#: （线上定点整数），`pre_close_price` 改名 `pre_close`。
_INFO_COLUMNS = ("symbol", "security_status", "pre_close_price", "high_limited",
                 "low_limited", "price_tick", "list_day")
_FUTURE_INFO_COLUMNS = ("symbol", "pre_close_price", "high_limited", "low_limited",
                        "price_tick", "list_day")
_PRICE_COLUMNS = ("pre_close_price", "high_limited", "low_limited", "price_tick")
_PRICE_SCALE = 1_000_000


def _code_table(market: int) -> pd.DataFrame:
    """一个市场的代码表。厂商丢掉 `error` 直接用返回值，失败留给下游炸。"""
    item = structs.SubCodeTableItem()
    item.market = market
    item.security_code = ""
    frame, _error = tgw.QuerySecuritiesInfo(item)
    return frame


def _codes_of(frame: pd.DataFrame, market: int, security_type: str) -> list[str]:
    """代码表 → 带后缀的代码，按证券类型过滤。`unique()` 保持服务端行序。"""
    codes = [full_code(market, code) for code in frame["security_code"].unique()]
    return [code for code in codes if is_security_type(code, security_type)]


def _neeq_kept(frame: pd.DataFrame) -> pd.DataFrame:
    """北交所：丢掉 `english_name` 是 20 个空格的行，除非代码是 920 开头。"""
    blank = frame["english_name"] == _BLANK_ENGLISH
    keep = (frame["english_name"] != _BLANK_ENGLISH) | (
        blank & frame["security_code"].str.startswith("920"))
    return frame[keep]


def _hkt_codes(security_type: str, window: tuple[int, int] | None = None) -> list[str]:
    """港股通代码表（`A010010011`）。

    `window` 给了就按 `ENTRY_DATE < end` 且 `REMOVE_DATE > start` 截区间，
    并把空的 `REMOVE_DATE` 填成 `20990101`（厂商 `fillna('20990101')`，
    公开接口规范里记的日期常量）。`TYPE_CODE_INT`：1 深、2 沪、3 两个都出。
    """
    task_id = tgw.GetTaskID()
    tgw.SetThirdInfoParam(task_id, "function_id", "A010010011")
    frame, _error = tgw.QueryThirdInfo(task_id)
    if window is not None:
        start_date, end_date = window
        frame = frame.fillna("20990101")
        frame = frame[(frame["ENTRY_DATE"] < str(end_date))
                      & (frame["REMOVE_DATE"] > str(start_date))]
    if security_type in ("SZ_HKT", "SH_HKT"):
        frame = frame[frame["TYPE_CODE_INT"].isin([1, 3]) & (frame["CURRENT_SIGN"] == 1)]
    else:
        frame = frame[frame["CURRENT_SIGN"] == 1]
    codes: list[str] = []
    for _, row in frame.iterrows():
        bare = row["SECURITY_CODE"][:-3]
        kind = row["TYPE_CODE_INT"]
        if kind == 1:
            codes.append(bare + ".SZ")
        elif kind == 2:
            codes.append(bare + ".SH")
        elif kind == 3:
            codes += [bare + ".SZ", bare + ".SH"]
    return codes


def _code_info(markets: tuple[int, ...], security_type: str,
               columns: tuple[str, ...], futures: bool = False) -> pd.DataFrame:
    """代码表 → 证券信息表。逐市场查、逐市场过滤，纵向拼起来。

    `futures` 为真时先按 `security_type == '06007'` 过一遍——期货合约的代码表
    里混着期权和其他品种。`code_market` 去重在过滤之前，index 是带后缀的
    代码，**不重置**。
    """
    parts = []
    for market in markets:
        frame = _code_table(market)
        if futures:
            frame = frame[frame["security_type"] == _FUTURE_ROW_TYPE]
        frame = frame.assign(code_market=[full_code(market, code)
                                          for code in frame["security_code"]])
        frame = frame.assign(is_security_type=[is_security_type(code, security_type)
                                               for code in frame["code_market"]])
        frame = frame.drop_duplicates(subset="code_market").set_index("code_market")
        parts.append(frame[frame["is_security_type"]])
    result = pd.concat(parts)[list(columns)]
    result[list(_PRICE_COLUMNS)] = result[list(_PRICE_COLUMNS)].div(_PRICE_SCALE)
    return result.rename(columns={"pre_close_price": "pre_close"})


# --------------------------------------------------------------------------
# 历史代码表
# --------------------------------------------------------------------------

HIST_CODE_LIST_FUNCTION_ID = "A010010009_NEW"

#: 一批并发多少个交易日。厂商 `QueryPara.req_hist_code_list_len`。
HIST_BATCH = 30

#: 日历往前截到哪一年。厂商写死的下界，2010 年之前的历史代码表查不到。
HIST_FIRST_DAY = 20100101


def _read_pkl(path: str, name: str) -> Any:
    with open(path + name + ".pkl", "rb") as handle:
        return pickle.load(handle)


def _write_pkl(path: str, name: str, data: Any) -> None:
    if not os.path.exists(path):
        os.makedirs(path)
    with open(path + name + ".pkl", "wb") as handle:
        pickle.dump(data, handle)


def _download_hist_code_list(path: str, days: list[int]) -> None:
    """逐个交易日下载当日全市场代码表，存 `<日期>.pkl`（内容是一个 set）。

    目录里已有的日期跳过：一次 `get_hist_code_list` 要 261 个交易日，
    增量靠文件名。
    """
    try:
        have = [int(name[:8]) for name in os.listdir(path)]
    except FileNotFoundError:
        print(path + "   本地无 “历史代码列表”")
        have = []
    todo = [day for day in days if day not in have]
    fetched = gw.fetch_each(
        todo, lambda day: (("function_id", HIST_CODE_LIST_FUNCTION_ID),
                           ("start_date", str(day)), ("end_date", str(day))),
        HIST_BATCH)
    for day, frame in fetched.items():
        if not list(frame):
            continue
        _write_pkl(path, str(day), set(list(frame["MARKET_CODE"])))
    print("历史代码列表 下载完成")


# --------------------------------------------------------------------------
# 复权因子
# --------------------------------------------------------------------------

#: 一批并发多少只代码。厂商 `QueryPara.req_backward_factor_len`。
FACTOR_BATCH = 300


def _ex_factor_round(codes: list[str], frames: dict[str, Any]) -> list[str]:
    """一轮 `QueryExFactorTable`：等齐回包写进 `frames`，返回失败的代码。

    判失败的规则与资讯族同源（`_gateway.is_failed`）。**用异步形态而不是同步**：
    同步形态在 `data is None` 时一律转成失败文案，于是「有请求但无数据」
    （`kDataEmpty`）会被误判成失败，白重试三轮再抛「查询失败」。
    """
    done = threading.Event()
    lock = threading.Lock()
    left = [len(codes)]
    failed: list[str] = []

    def on_response(code: str) -> Callable[[Any, Any], None]:
        def callback(data: Any, status: Any) -> None:
            with lock:
                if data is not None:
                    frames[code] = data
                if is_failed(status):
                    failed.append(code)
                left[0] -= 1
                if left[0] == 0:
                    done.set()
        return callback

    for code in codes:
        _market, security_code = split_code(code)
        tgw.QueryExFactorTable(security_code, query_spi=on_response(code))
    done.wait()
    return failed


def _ex_factors(codes: list[str]) -> dict[str, Any]:
    """分批发复权因子，失败的最多重发 `RETRY_TIMES` 轮，仍失败抛「查询失败」。

    抛的是 `Exception` 本身：异常类型是暴露面的一部分，门禁记的是类型名。
    """
    frames: dict[str, Any] = {}
    for start in range(0, len(codes), FACTOR_BATCH):
        batch = codes[start:start + FACTOR_BATCH]
        failed = _ex_factor_round(batch, frames)
        for i in range(RETRY_TIMES):
            if not failed:
                break
            print("retry ", str(i))
            failed = _ex_factor_round(failed, frames)
        if failed:
            raise Exception("查询失败")
    return frames


def _download_factor(code_list, calendar: list[int], path: str, name: str,
                     factor_column: str, label: str, use_local: bool,
                     forward_fill: bool) -> pd.DataFrame:
    """下载复权因子，按交易日历 reindex 成整表，落 `basedata/<name>/<name>.h5`。

    `use_local` 时把本地表已有的代码一并带上重下——厂商 `download_backward_factor`
    无条件读本地，`download_adj_factor` 则跟着 `is_local` 走。
    `forward_fill` 只对后复权因子开：厂商两张表的填充口径不一样，单次因子
    `fillna(1)` 之前不 ffill，两次除权之间的空档会填成 1 而不是前值。
    """
    local_codes: list[str] = []
    if use_local:
        try:
            local_codes = list(store.read_table(path, name).columns)
        except FileNotFoundError:
            print(path + f"   本地无 “{label}”")
    # 厂商写的是 list(set(...))：代码顺序随哈希种子变，返回值的列序跟着变。
    codes = list(set(list(code_list) + local_codes))
    frames = _ex_factors(codes)

    parts = []
    for code, frame in frames.items():
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame({}, columns=[factor_column, "ex_date"])
        frame = frame.set_index("ex_date").sort_index()
        if frame.empty:
            parts.append(pd.DataFrame(np.nan, columns=[code], index=calendar))
            continue
        column = pd.DataFrame(frame[factor_column])
        column.columns = [code]
        parts.append(column)
    table = pd.concat(parts, axis=1)

    # 日历上有、因子表里没有的日期，补一段 NaN 再整体排序。
    missing = list(set(calendar) - set(table.index))
    table = pd.concat([table, pd.DataFrame(np.nan, columns=table.columns, index=missing)])
    table.sort_index(inplace=True)
    table.replace([np.inf, 0], np.nan, inplace=True)
    if forward_fill:
        table.ffill(inplace=True)
    table.fillna(1, inplace=True)
    table = table.loc[calendar, :]
    table.index = pd.Series([dt.datetime.strptime(str(day), "%Y%m%d")
                             for day in table.index])
    store.save_table(path, name, table)
    return table


# --------------------------------------------------------------------------
# ETF 申赎
# --------------------------------------------------------------------------

#: 两组列名抄自厂商 `base_data.py` 字节码里的两个 `LOAD_CONST` 元组
#: （`base_data.dis.txt:200` 与 `:208`）。回包里缺的列补 NaN。
_ETF_INFO_COLUMNS = ("creation_redemption_unit", "max_cash_ratio", "publish",
                     "creation", "redemption", "creation_redemption_switch",
                     "record_num", "total_record_num", "estimate_cash_component",
                     "trading_day", "pre_trading_day", "cash_component",
                     "nav_per_cu", "nav", "symbol", "fund_management_company",
                     "underlying_security_id", "underlying_security_id_source",
                     "dividend_per_cu", "creation_limit", "redemption_limit",
                     "creation_limit_per_user", "redemption_limit_per_user",
                     "net_creation_limit", "net_redemption_limit")
_ETF_CONSTITUENT_COLUMNS = ("underlying_symbol", "component_share", "substitute_flag",
                            "premium_ratio", "discount_ratio",
                            "creation_cash_substitute", "redemption_cash_substitute",
                            "substitution_cash_amount", "underlying_security_id")

#: 申赎单位与最大现金替代比例的定点除数。
_ETF_UNIT_SCALE = 100
_ETF_CASH_RATIO_SCALE = 10_000


# --------------------------------------------------------------------------
# 对外类
# --------------------------------------------------------------------------


class BaseData:
    """基础数据。每个方法的签名与厂商 `BaseData` 相同。"""

    def __init__(self) -> None:
        self.calendar: list = []
        self.stock_list: list = []
        self.code_list_hist: list = []
        self.block_trading: dict = {}

    # -- 交易日历 ----------------------------------------------------------

    def get_calendar(self, data_type="str", market="SH", date=None):
        """交易日历，`data_type='datetime'` 出 datetime 列表，否则出 int 列表。

        `date` 是**结束日期**，不是「查某一天的日历」——`MarketData` 构造和
        复权因子都传当天。缺省取 `_clock.today()`。
        """
        end = str(date)[:8] if date else _clock.today()
        days = full_calendar(market, end, _first_day())
        if data_type == "datetime":
            self.calendar = [dt.datetime.strptime(str(day), "%Y%m%d") for day in days]
        else:
            self.calendar = days
        return self.calendar

    # -- 代码表 -----------------------------------------------------------

    def get_code_list(self, security_type="EXTRA_STOCK_A_SH_SZ"):
        """沪深北每日最新代码表，返回 `沪 + 深 + 北` 顺序的代码列表。

        港股通三个类型走 `A010010011`，不查逐市场代码表。
        """
        if security_type in _HKT_TYPES:
            self.stock_list = _hkt_codes(security_type)
            return self.stock_list
        by_market = {}
        for market in _STOCK_MARKETS:
            frame = _code_table(market)
            if market == tgw.MarketType.kNEEQ:
                frame = _neeq_kept(frame)
            by_market[market] = _codes_of(frame, market, security_type)
        self.stock_list = (by_market[tgw.MarketType.kSSE]
                           + by_market[tgw.MarketType.kSZSE]
                           + by_market[tgw.MarketType.kNEEQ])
        return self.stock_list

    def get_option_code_list(self, security_type="EXTRA_ETF_OP"):
        """期权每日最新代码表。市场按 `security_type` 选，认不出的类型返回空列表。"""
        codes: list[str] = []
        for market in _OPTION_MARKETS.get(security_type, ()):
            codes += _codes_of(_code_table(market), market, security_type)
        return codes

    def get_future_code_list(self, security_type="EXTRA_FUTURE"):
        """期货每日最新代码表，返回 `上期所 + 中金所 + 大商所 + 郑商所 + 能源中心`。"""
        by_market = {}
        for market in _FUTURE_MARKETS:
            frame = _code_table(market)
            frame = frame[frame["security_type"] == _FUTURE_ROW_TYPE]
            by_market[market] = _codes_of(frame, market, security_type)
        self.stock_list = (by_market[tgw.MarketType.kSHFE]
                           + by_market[tgw.MarketType.kCFFEX]
                           + by_market[tgw.MarketType.kDCE]
                           + by_market[tgw.MarketType.kCZCE]
                           + by_market[tgw.MarketType.kINE])
        return self.stock_list

    def get_code_info(self, security_type="EXTRA_STOCK_A"):
        """沪深北每日最新证券信息，index 是带后缀的代码。"""
        return _code_info(_STOCK_MARKETS, security_type, _INFO_COLUMNS)

    def get_future_code_info(self, security_type="EXTRA_FUTURE"):
        """期货每日最新证券信息。厂商手册没有这个函数，规格来自字节码。"""
        return _code_info(_FUTURE_MARKETS, security_type, _FUTURE_INFO_COLUMNS,
                          futures=True)

    # -- 历史代码表 --------------------------------------------------------

    def get_hist_code_list(self, security_type="EXTRA_STOCK_A_SH_SZ",
                           start_date=20240101, end_date=20240701,
                           local_path=DEFAULT_LOCAL_PATH):
        """历史代码表：`[start_date, end_date]` 区间内出现过的代码。

        区间按交易日历切（只取 2010 年之后），逐日下载到
        `basedata/hist_code_list/<日期>.pkl`，再把所有日期的代码并起来过滤。
        返回值是 `list(set(...))` 的顺序——厂商这么写，顺序随哈希种子变，
        等价测试把 `PYTHONHASHSEED` 钉成 0 才比得了。
        """
        if security_type in _HKT_TYPES:
            self.code_list_hist = list(set(
                _hkt_codes(security_type, window=(start_date, end_date))))
            return self.code_list_hist
        path = store.table_dir(local_path, "basedata", "hist_code_list")
        calendar = [day for day in self.get_calendar(market="SH")
                    if HIST_FIRST_DAY < day <= end_date and day >= start_date]
        _download_hist_code_list(path, calendar)
        codes: set = set()
        for day in calendar:
            try:
                codes = codes.union(_read_pkl(path, str(day)))
            except FileNotFoundError as exc:
                # 某天回包为空就没有 pkl。厂商的处理在循环里（`base_data.pyc`
                # 偏移 1640 起，except 后 `JUMP_BACKWARD` 回循环头）：缺的是区间
                # 最后一天就拿当天的实时代码表补上（最新一天的历史表常常还没
                # 生成），否则打印异常跳过这天。还原源码把这段印成了循环外的
                # 死代码，看上去像「吞掉异常返回 None」，实际不是。
                if day == calendar[-1]:
                    codes = codes.union(self.get_code_list(security_type=security_type))
                else:
                    print(exc)
        self.code_list_hist = [code for code in codes
                               if is_security_type(code, security_type)]
        return self.code_list_hist

    # -- 复权因子 ----------------------------------------------------------

    def get_backward_factor(self, code_list, local_path=DEFAULT_LOCAL_PATH,
                            is_local=True):
        """后复权因子（`cum_factor` 列），index 是交易日 datetime。

        `is_local=True` 读本地 `basedata/backward_factor/backward_factor.h5`，
        文件不在就下载并写回；`is_local=False` 直接下载。返回值最后按
        `code_list` 取列——厂商在 `is_local=True` 时取了两次，缺一只代码
        会在最后一行抛 `KeyError`（中间的 `try` 把它咽下去只打印一句）。
        """
        calendar = self.get_calendar(market="SH")
        path = store.table_dir(local_path, "basedata", "backward_factor")
        try:
            if is_local:
                factor = store.read_table(path, "backward_factor")
            else:
                factor = _download_factor(code_list, calendar, path,
                                          "backward_factor", "cum_factor",
                                          "后复权因子", True, True)
            try:
                factor = factor[code_list]
            except Exception:
                print(code_list, "  not in local backward_factor")
        except FileNotFoundError:
            factor = _download_factor(code_list, calendar, path, "backward_factor",
                                      "cum_factor", "后复权因子", True, True)[code_list]
        return factor[code_list] if is_local else factor

    def get_adj_factor(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True):
        """单次复权因子（`ex_factor` 列）。只 `fillna(1)`、不前向填充，与后复权因子不同。"""
        calendar = self.get_calendar(market="SH")
        path = store.table_dir(local_path, "basedata", "adj_factor")
        try:
            if is_local:
                factor = store.read_table(path, "adj_factor")
            else:
                factor = _download_factor(code_list, calendar, path, "adj_factor",
                                          "ex_factor", "前复权因子", is_local, False)
            factor = factor[code_list]
        except FileNotFoundError:
            factor = _download_factor(code_list, calendar, path, "adj_factor",
                                      "ex_factor", "前复权因子", is_local, False)[code_list]
        return factor[code_list] if is_local else factor

    # -- ETF 申赎 ---------------------------------------------------------

    def get_etf_pcf(self, code_list):
        """ETF 申赎信息与成分股，返回 `(申赎信息表, {ETF 代码: 成分股表})`。

        **未验证**：线上 `ReqGetETFCodeTableList` 对本账号恒回 `-82`
        （流量超限），成功回包的形状没见过。下面的分帧与补列照厂商字节码写，
        真拿到数据时才能确认对不对。
        """
        items = []
        for code in code_list:
            market, security_code = split_code(code)
            item = structs.SubCodeTableItem()
            item.market = market
            item.security_code = security_code
            items.append(item)
        pairs, _error = tgw.QueryETFInfo(items)

        info_frames, constituents = [], {}
        for pair in pairs:
            info, part = pair[0], pair[1]
            info_frames.append(info)
            code = full_code(info["market_type"].iloc[0], info["security_code"].iloc[0])
            part = part.assign(code=[full_code(market, security)
                                     for market, security
                                     in zip(part["market_type"], part["security_code"])])
            part = part.set_index("code")
            _fill_missing(part, _ETF_CONSTITUENT_COLUMNS)
            constituents[code] = part[list(_ETF_CONSTITUENT_COLUMNS)]

        table = pd.concat(info_frames)
        table["code"] = [full_code(market, security) for market, security
                         in zip(table["market_type"], table["security_code"])]
        table["underlying_security_id_source"] = [
            SUFFIX_BY_MARKET.get(source)
            for source in table["underlying_security_id_source"]]
        table = table.set_index("code")
        _fill_missing(table, _ETF_INFO_COLUMNS)
        table = table[list(_ETF_INFO_COLUMNS)]
        table["creation_redemption_unit"] = table["creation_redemption_unit"].div(
            _ETF_UNIT_SCALE)
        table["max_cash_ratio"] = table["max_cash_ratio"].div(_ETF_CASH_RATIO_SCALE)
        return table, constituents


def _fill_missing(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    """缺的列补 NaN，原地改。"""
    missing = [column for column in columns if column not in list(frame.columns)]
    if missing:
        frame[missing] = np.nan
