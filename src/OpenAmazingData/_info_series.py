"""InfoData 里五个有专门流程的查询方法：历史证券状态、指数 / 行业权重、
行业日行情、国债收益率。逐步行为照厂商 .pyc 实现，修正的缺陷见
`docs/vendor-differences.md`。

这五个不归 `info.py` 的五族引擎：回包要先改名再拼表，行业日行情还把
`TRADE_DATE` 转成 datetime 当 index，指数与行业权重要按日期窗口切分。
共同的骨架——按代码读本地缓存定增量起点、按代码发请求、合并去重、非空落盘、
结果挂到实例属性上——收在 `_Series` / `_Plan` 与 `_query` 里，差异留在各自的
`_download_*`。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from . import _gateway as gw
from . import _store as store
from ._local import DEFAULT_LOCAL_PATH
from .info import FIRST_DAY, LAST_DAY, _conform, _same, _start_after, _window

Frame = pd.DataFrame

#: 历史证券信息是两张表：`A010060002` 给逐日状态，`A010060005` 只给交易日序列，
#: 两张按交易日 left join。两边的 `MARKET_CODE` 列名相同，merge 后变成
#: `MARKET_CODE_x` / `MARKET_CODE_y`，厂商就让列清单里的 `MARKET_CODE` 落到
#: `_conform` 的补列上——那一列整列是 NaN（厂商缺陷 V04，见 `_download_status`）。
_TRADE_DAY_ID = "A010060005"

#: 国债收益率只留这一天之后的行。厂商写死在字节码常量里。
_YIELD_FIRST_DAY = "20100101"


# --------------------------------------------------------------------------
# 静态事实
# --------------------------------------------------------------------------


def _resume_column(column: str) -> Callable[[Frame], str]:
    """本地表某一列的最大值 → 增量起点（先过一遍日期转换）。"""
    return lambda frame: _start_after(frame[column].max())


def _resume_index(frame: Frame) -> str:
    """行业日行情的增量起点读的是 `index.max()`，不是某一列。

    缓存里 index 就是 `TRADE_DATE` 转出来的 datetime，所以取它的 yyyymmdd。
    证据：`download_info_data.py` 的 `download_industry_daily`（第 2714 行）
    `industry_daily_end_date = industry_daily[code].index.max()`。
    """
    return str(int(frame.index.max().strftime("%Y%m%d")))


def _resume_raw(column: str) -> Callable[[Frame], str]:
    """国债收益率直接拿 `DATE` 的最大值当起点，没过日期转换
    （`download_treasury_yield`：`local_data_end_date[code] = str(next_day)`）。"""
    return lambda frame: str(frame[column].max())


def _index_by_date(frame: Frame) -> Frame:
    """国债收益率读本地时把 `DATE` 挪成 index，形状跟冷跑返回的一致。

    厂商缺陷 V07：`download_treasury_yield` 只在下载那条路上 `set_index('DATE')`，
    落盘的是 set_index 之前那张带 `DATE` 列的表，热跑读本地原样返回，于是冷跑
    `['YIELD']`（index 是 DATE）、热跑 `['YIELD', 'DATE']`，同一个查询两种形状。
    """
    return frame.set_index("DATE")


def _far_future() -> str:
    """`str(datetime_to_int() + 10000)`：整数加法，把 yyyymmdd 加一万，大致是一年后。

    厂商 `utils/data_transfer.py` 的 `datetime_to_int` 缺省参数在模块 import 时
    求值，取的是进程启动那天的系统日期——不是自研包里 `_clock.fixed_today` 钉的
    那个。等价测试给自研包钉 20251231，走 `_clock` 会让 end_date 与 .pyc 差一万。
    """
    return str(int(datetime.now().strftime("%Y%m%d")) + 10000)


@dataclass(frozen=True)
class _Series:
    """一个方法的静态事实，全部来自接口规范与厂商下载函数。"""

    attr: str                          # 实例上结果属性的名字
    function_id: str
    dirs: tuple[str, ...]              # 缓存目录，相对本地数据根
    label: str                         # 打印里的中文名
    columns: tuple[str, ...]           # 输出列清单
    code_param: str                    # 请求里放代码的键
    batch_len: int                     # 一次发多少只代码
    start_default: str                 # 本地无缓存时的增量起点
    resume: Callable[[Frame], str]     # 本地表 → 增量起点
    window_days: int = 0               # 按日期窗口切分时每个窗口的天数，0 表示不切
    end_default: Callable[[], str] = lambda: LAST_DAY
    renames: dict[str, str] = field(default_factory=dict)
    read_shape: Callable[[Frame], Frame] = _same  # 读本地那张表要摆成什么形状


@dataclass(frozen=True)
class _Plan:
    """一次下载的固定事实：区间、缓存目录、本地表、每只代码的增量起点。"""

    begin: str | None                  # None 表示调用方没指定区间，走本地增量
    end: str
    path: str
    local: dict[Any, Frame]
    start: dict[Any, str]


_STATUS = _Series(
    attr="history_stock_status", function_id="A010060002",
    dirs=("basedata", "hist_stock_status"), label="历史证券信息数据",
    columns=("MARKET_CODE", "TRADE_DATE", "PRECLOSE", "HIGH_LIMITED", "LOW_LIMITED",
             "PRICE_HIGH_LMT_RATE", "PRICE_LOW_LMT_RATE", "IS_ST_SEC", "IS_SUSP_SEC",
             "IS_WD_SEC", "IS_XR_SEC"),
    code_param="market_code", batch_len=200, start_default=FIRST_DAY,
    resume=_resume_column("TRADE_DATE"))

def _newest_first(frame: Frame) -> Frame:
    """权重表读本地时保证按 `TRADE_DATE` 倒序（V05）。

    自研包落盘的已经是倒序表，这里原样返回，热跑与冷跑逐行相同。厂商 SDK 写的
    旧缓存是升序（它落盘的是排序前那张表），读到时按稳定排序摆成倒序。
    """
    if frame.empty or frame["TRADE_DATE"].is_monotonic_decreasing:
        return frame
    return frame.sort_values("TRADE_DATE", ascending=False, kind="stable")


_INDEX_WEIGHT = _Series(
    attr="index_weight", function_id="A010200005",
    dirs=("infodata", "index_weight"), label="指数权重数据",
    columns=("INDEX_CODE", "CON_CODE", "TRADE_DATE", "TOTAL_SHARE", "FREE_SHARE_RATIO",
             "CALC_SHARE", "WEIGHT_FACTOR", "WEIGHT", "CLOSE"),
    code_param="index_code", batch_len=50, start_default="20050101",
    resume=_resume_column("TRADE_DATE"),
    window_days=50, end_default=_far_future,
    renames={"TOTAL_SHR": "TOTAL_SHARE", "FREE_SHR_RATIO": "FREE_SHARE_RATIO",
             "CALC_SHR": "CALC_SHARE"},
    read_shape=_newest_first)

_INDUSTRY_WEIGHT = _Series(
    attr="industry_weight", function_id="A010200008",
    dirs=("infodata", "industry_weight"), label="行业权重数据",
    columns=("WEIGHT", "CON_CODE", "TRADE_DATE", "INDEX_CODE"),
    code_param="index_code", batch_len=50, start_default="20050101",
    resume=_resume_column("TRADE_DATE"),
    window_days=50, end_default=_far_future,
    read_shape=_newest_first)

_INDUSTRY_DAILY = _Series(
    attr="industry_daily", function_id="A010200007",
    dirs=("infodata", "industry_daily"), label="行业日行情数据",
    columns=("OPEN", "HIGH", "CLOSE", "LOW", "AMOUNT", "VOLUME", "PB", "PE", "TOTAL_CAP",
             "INDEX_CODE", "A_FLOAT_CAP", "PRE_CLOSE", "TRADE_DATE"),
    code_param="index_code", batch_len=2, start_default=FIRST_DAY,
    resume=_resume_index,
    renames={"OPEN_PRICE": "OPEN", "HIGH_PRICE": "HIGH", "CLOSE_PRICE": "CLOSE",
             "LOW_PRICE": "LOW", "TRADING_AMOUNT": "AMOUNT", "TRADING_VOLUME": "VOLUME",
             "INDEX_PB_RATIO": "PB", "INDEX_PE_RATIO": "PE", "TOTAL_MV": "TOTAL_CAP",
             "A_SHARE_MV": "A_FLOAT_CAP", "CLOSE_PRICE_YES": "PRE_CLOSE"})

_TREASURY_YIELD = _Series(
    attr="treasury_yield", function_id="A010050009",
    dirs=("infodata", "treasury_yield"), label="国债收益率数据",
    columns=("YIELD", "DATE"),
    code_param="indi_type", batch_len=200, start_default=FIRST_DAY,
    resume=_resume_raw("DATE"),
    renames={"INDI_TYPE": "DURATION", "INDI_NUM": "YIELD", "DDLN_DATE": "DATE"},
    read_shape=_index_by_date)


# --------------------------------------------------------------------------
# 骨架
# --------------------------------------------------------------------------


def _plan(t: _Series, local_path: str, code_list: list, kwargs: dict) -> _Plan:
    """定区间、读本地缓存、算每只代码的增量起点。

    `begin_date` 与 `end_date` 都给了才算指定区间（`info._window`）；只给一个
    等于没给，此时 `begin` 是 None，本地表要读、结果也要落盘。
    """
    begin, end = _window(kwargs, t.end_default())
    path = store.table_dir(local_path, *t.dirs)
    local: dict[Any, Frame] = {}
    start: dict[Any, str] = {}
    for code in code_list:
        if begin is not None:
            local[code], start[code] = pd.DataFrame(), begin
            continue
        try:
            local[code] = store.read_table(path, code)
            start[code] = t.resume(local[code])
        except FileNotFoundError:
            print(f"{path}  本地无{code} “{t.label}”")
            local[code], start[code] = pd.DataFrame(), t.start_default
    return _Plan(begin, end, path, local, start)


def _fetch_by_code(t: _Series, code_list: list, start: dict, end: str) -> dict:
    return gw.fetch_each(code_list, lambda c: (
        ("function_id", t.function_id), (t.code_param, c),
        ("start_date", start[c]), ("end_date", end)), t.batch_len)


def _date_windows(start: str, end: str, days: int) -> list[tuple[str, str]]:
    """`date_split`：从 start 到 end 按自然日切片，每 `days` 天一个闭区间。

    参数 `split_num` 是**天数**，不是请求数——`req_index_weight_len` 的值 50
    在这里被当成每窗 50 天，所以 2005 年起的一段历史要切出上百个窗口。
    证据：`utils/data_transfer.py` 的 `date_split`。
    """
    cursor = datetime.strptime(str(start), "%Y%m%d")
    stop = datetime.strptime(str(end), "%Y%m%d")
    windows: list[tuple[str, str]] = []
    chunk: list[str] = []
    while cursor <= stop:
        chunk.append(cursor.strftime("%Y%m%d"))
        if len(chunk) == days:
            windows.append((chunk[0], chunk[-1]))
            chunk = []
        cursor += timedelta(days=1)
    if chunk:
        windows.append((chunk[0], chunk[-1]))
    return windows


def _shape(t: _Series, frame: Frame) -> Frame:
    """回包先按改名表改名，再补齐缺的列、按列清单取列。"""
    if t.renames:
        frame = frame.rename(columns=t.renames)
    return _conform(frame, t.columns)


def _descending(frame: Frame, column: str) -> Frame:
    """厂商的收尾排序：按日期倒序，不重置 index，所以返回值的 index 是乱序标签。"""
    return frame if frame.empty else frame.sort_values(column, ascending=False)


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------


def _download_status(t: _Series, local_path: str, code_list: list, **kwargs) -> dict:
    plan = _plan(t, local_path, code_list, kwargs)
    status = _fetch_by_code(t, code_list, plan.start, plan.end)
    trade_days = gw.fetch_each(code_list, lambda c: (
        ("function_id", _TRADE_DAY_ID), (t.code_param, c),
        ("start_date", plan.start[c]), ("end_date", plan.end)), t.batch_len)
    # 交易日序列那张表缺一天，这只代码就没有可拼的表。
    joined: dict[Any, Frame] = {}
    for code in code_list:
        if status[code].empty or trade_days[code].empty:
            continue
        days = trade_days[code]
        days["TRD_DATE"] = days["TRD_DATE"].astype(str)
        frame = pd.merge(days, status[code], left_on="TRD_DATE",
                         right_on="TRADE_DATE", how="left")
        # 厂商缺陷 V04：两边都有 MARKET_CODE，merge 后成了 MARKET_CODE_x /
        # MARKET_CODE_y，列清单里的 MARKET_CODE 落到 _conform 的补列上，整列 NaN
        # （.pyc 实测：dtype float64，60 行全 NaN）。这一趟问的就是这只代码，
        # 填它自己；列清单与其余列的顺序不动。
        frame["MARKET_CODE"] = code
        joined[code] = frame
    print(f"{t.label} 下载完成")
    result = {}
    for code in code_list:
        if code in joined:
            merged = pd.concat([plan.local[code], _shape(t, joined[code])]).drop_duplicates()
            merged = _descending(merged, "TRADE_DATE")
            result[code] = merged
            if not merged.empty and plan.begin is None:
                store.save_table(plan.path, code, merged)
            continue
        result[code] = _descending(plan.local[code], "TRADE_DATE")
    return result


def _download_weight(t: _Series, local_path: str, code_list: list, **kwargs) -> dict:
    plan = _plan(t, local_path, code_list, kwargs)
    fresh: dict[Any, Frame] = {}
    for code in code_list:
        windows = _date_windows(plan.start[code], plan.end, t.window_days)
        if not windows:
            continue
        # 一只代码的全部窗口一轮发完（厂商不分批），回包按窗口顺序拼。
        got = gw.fetch_each(windows, lambda w, code=code: (
            ("function_id", t.function_id), (t.code_param, code),
            ("start_date", w[0]), ("end_date", w[1])), len(windows))
        fresh[code] = pd.concat(list(got.values()))
    print(f"{t.label} 下载完成")
    result = {}
    for code in code_list:
        if code in fresh:
            merged = pd.concat([plan.local[code], _shape(t, fresh[code])]).drop_duplicates()
            # 稳定排序：同一 TRADE_DATE 内保留拼接顺序。厂商用的是缺省的不稳定排序，
            # 同日行序随 pandas 版本变（.pyc 自己在 2.3 / 3.0 下就不一样），自研包
            # 不跟这个偶然性，门禁对同日行序也不作判据（capability_spec.TIE_UNORDERED）。
            ordered = merged.sort_values("TRADE_DATE", ascending=False, kind="stable")
            result[code] = ordered
            if not ordered.empty and plan.begin is None:
                # 厂商缺陷 V05：`sort_values` 的结果只放进返回值，落盘那行传的仍是
                # 排序前的表，所以热跑读本地拿到的是未排序的表（.pyc 实测：冷跑
                # 20251231…、热跑 20240101…）。落盘排序后的表，冷热顺序才一致；
                # 文件名、key 都不动。
                store.save_table(plan.path, code, ordered)
            continue
        # 厂商缺陷 V06：网关没给这只代码，厂商两支各错一半——行业权重把键整个丢掉
        # （.pyc 实测：返回 dict 是空的），指数权重对连列都没有的空本地表 sort_values
        # 抛 `KeyError: 'TRADE_DATE'`。两种表现都不给调用方留下这只代码，统一给
        # 一张列按列清单齐全的空表。
        kept = plan.local[code]
        result[code] = (_shape(t, kept) if kept.empty
                        else kept.sort_values("TRADE_DATE", ascending=False, kind="stable"))
    return result


def _download_daily(t: _Series, local_path: str, code_list: list, **kwargs) -> dict:
    plan = _plan(t, local_path, code_list, kwargs)
    fetched = _fetch_by_code(t, code_list, plan.start, plan.end)
    print(f"{t.label} 下载完成")
    result = {}
    for code in code_list:
        fresh = _shape(t, fetched[code])
        # 厂商按固定系数放大：成交额 ×1000、成交量 ×100（`download_industry_daily`）。
        # 服务端给的原始单位未验证，这里只照系数复现。
        fresh["AMOUNT"] = fresh["AMOUNT"] * 1000
        fresh["VOLUME"] = fresh["VOLUME"] * 100
        fresh["TRADE_DATE"] = pd.to_datetime(fresh["TRADE_DATE"].astype(str), format="%Y%m%d")
        merged = pd.concat([plan.local[code], fresh.set_index("TRADE_DATE")]).drop_duplicates()
        if merged.empty:
            result[code] = plan.local[code]
            continue
        result[code] = merged
        if plan.begin is None:
            store.save_table(plan.path, code, merged)
    return result


def _download_yield(t: _Series, local_path: str, code_list: list, **kwargs) -> dict:
    plan = _plan(t, local_path, code_list, kwargs)
    fetched = _fetch_by_code(t, code_list, plan.start, plan.end)
    print(f"{t.label} 下载完成")
    result = {}
    for code in code_list:
        merged = pd.concat([plan.local[code], _shape(t, fetched[code])]).drop_duplicates()
        if merged.empty:
            result[code] = plan.local[code]
            continue
        merged = merged[merged["DATE"] >= _YIELD_FIRST_DAY]
        merged = merged.sort_values("DATE", ascending=False).drop_duplicates()
        # 返回值把 DATE 变成 index，落盘的表留着 DATE 列——读本地那侧由
        # `_index_by_date` 补上这一步，冷热返回的形状才一致（V07）。
        result[code] = merged.set_index("DATE")
        if plan.begin is None:
            store.save_table(plan.path, code, merged)
    return result


# --------------------------------------------------------------------------
# 对外方法
# --------------------------------------------------------------------------


def _query(self, t: _Series, code_list: list, local_path: str, is_local: bool,
           kwargs: dict, download) -> dict:
    if "begin_date" in kwargs:
        value = download(t, local_path, code_list, **kwargs)
    elif is_local:
        try:
            # 读本地往实例上那个 dict 里累积，不新建——厂商行为，同一个 InfoData
            # 上先查 A 再查 B，B 的返回值里带着 A。`read_shape` 把读到的表摆成
            # 冷跑返回的形状（国债收益率 V07、两张权重表 V05）。
            cache = getattr(self, t.attr)
            for code in code_list:
                cache[code] = t.read_shape(
                    store.read_table(store.table_dir(local_path, *t.dirs), code))
            return cache
        except FileNotFoundError:
            value = download(t, local_path, code_list)
    else:
        value = download(t, local_path, code_list)
    setattr(self, t.attr, value)
    return value


def get_history_stock_status(self, code_list, local_path=DEFAULT_LOCAL_PATH,
                             is_local=True, **kwargs):
    """历史证券信息（A010060002 + A010060005，按交易日 left join）。"""
    return _query(self, _STATUS, code_list, local_path, is_local, kwargs, _download_status)


def get_index_weight(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
    """交易所指数成分股日权重（A010200005），按日期窗口分批请求。"""
    return _query(self, _INDEX_WEIGHT, code_list, local_path, is_local, kwargs, _download_weight)


def get_industry_weight(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
    """行业指数成分股日权重（A010200008），按日期窗口分批请求。"""
    return _query(self, _INDUSTRY_WEIGHT, code_list, local_path, is_local, kwargs, _download_weight)


def get_industry_daily(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
    """行业指数日行情（A010200007），`TRADE_DATE` 转 datetime 当 index。"""
    return _query(self, _INDUSTRY_DAILY, code_list, local_path, is_local, kwargs, _download_daily)


def get_treasury_yield(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
    """国债收益率（A010050009），`indi_type` 放期限，只留 2010 年起。"""
    return _query(self, _TREASURY_YIELD, code_list, local_path, is_local, kwargs, _download_yield)


METHODS = {
    "get_history_stock_status": get_history_stock_status,
    "get_index_weight": get_index_weight,
    "get_industry_weight": get_industry_weight,
    "get_industry_daily": get_industry_daily,
    "get_treasury_yield": get_treasury_yield,
}
