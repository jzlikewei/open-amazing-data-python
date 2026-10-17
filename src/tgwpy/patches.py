"""业务层与服务端现实不一致之处的修正。

这些不是协议层的错——协议层（`frames/`）已经按抓包原文实现并实测过。
是**业务层的假设和服务端实际行为对不上**，而且这些假设写在 1.1.15 的
`.pyc` 里改不动，只能在 import 之后替换掉。

每条都要写清楚：业务层为什么错、服务端实际是什么、证据从哪来。
没有证据的不要往这里塞。

## 1. 交易日历的 1000 行上限

`BaseData.get_calendar` 把 `start_date` 写死成 `19900101`（字节码常量
`LOAD_CONST '19900101'`，`get_calendar` 的 `LOAD_CONST` 序列里），
`end_date` 取传入的 `date`。而**服务端单次响应最多 1000 行**，
且 `offset` 参数对 `A010061003` 无效（实测 `offset=0/1000/2000` 返回相同内容）。

后果是连锁的：

- 日历止于 1994 年（实测全历史 → 1000 行止于 1994-11-22）
- `MarketData` 的 `date_list_split_len` 按日历切分，整段落在日历外
- `get_backward_factor` 拿日历去 `reindex` 因子表，因子表是 2001 年起的
- `query_kline` **静默返回 `{}` 并 exit 0**

静默是最糟的部分：数据少了但退出码是 0，批处理根本发现不了。

修法两件事：按时间窗分段拼出完整日历，以及**分段取满 1000 行就报错**，
不把截断当完整。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from typing import Any

#: 服务端单次响应的行数上限。实测 `A010061003` 取满 1000 就停。
SERVER_ROW_LIMIT = 1000

#: 分段窗口几年。A 股一年约 242 个交易日，3 年约 726 行，留足余量。
#: 窗口再大就有触发上限的风险，触发后只能靠报错让人知道。
SEGMENT_YEARS = 3

#: 最多回溯多少个窗口。1990 至今 36 年 / 3 = 12 个，留一倍余量。
MAX_SEGMENTS = 24

#: 日历起点。业务层原版写死这个值（`get_calendar` 字节码里的
#: `LOAD_CONST '19900101'`），但服务端单次只回 1000 行，所以拿它一次查
#: 必然被截断。
CALENDAR_FIRST_DAY = "19900101"

#: 回溯到哪一年为止，默认 `CALENDAR_FIRST_DAY`。可用
#: `TGW_CALENDAR_FIRST_DAY` 覆盖以省请求——但**老日期区间的 kline
#: 会查不到**，分批任务通常只需要近端，设之前想清楚。
CALENDAR_FIRST_DAY_DEFAULT = CALENDAR_FIRST_DAY

#: `BaseData.get_calendar` 的 `market` 参数 → 线上 `market` 取值。
#: 取自业务层自己的字节码常量（`get_calendar` 里 `market_dict` 的两个
#: 元组），这里只是把它抄出来供分段查询复用。
MARKET_WIRE: dict[str, str] = {
    "SZ": "SZSE", "SH": "SSE", "BJ": "NEEQ",
    "SHF": "SHFE", "CFE": "CFFEX", "DCE": "DCE",
    "CZC": "CZCE", "INE": "INE",
    "SHN": "SHHK", "SZN": "SZHK",
}

CALENDAR_FUNCTION_ID = "A010061003"


class IncompleteCalendar(RuntimeError):
    """日历取不全。

    单独立一个异常类型：批处理要能单独 catch 它并重试/跳过，
    而不是和「网络断了」「参数错了」混在一起。
    """


def _fetch_window(market_wire: str, start: str, end: str) -> list[int]:
    """一个日期窗口的交易日。返回空列表表示该窗口无数据。"""
    from tgwpy.compat import tgw_module as tm

    tid = tm.GetTaskID()
    for key, value in (
        ("function_id", CALENDAR_FUNCTION_ID),
        ("start_date", start),
        ("end_date", end),
        ("market", market_wire),
    ):
        tm.SetThirdInfoParam(tid, key, value)
    frame, err = tm.QueryThirdInfo(tid)
    if err:
        # `status=-100` = 这段区间查不到数据（`kDataEmpty` 类），不是错误。
        # 1990 年 1 月上交所还没开市，头几个窗口空是正常的。
        # 证据：网关实测 —— 参数非法走 301010
        # 且必带中文 msg，-100 是「无数据」。
        if "-100" in str(err) or "DataEmpty" in str(err):
            return []
        raise IncompleteCalendar(
            f"交易日历 {start}~{end} 查询失败：{err}"
        )
    if frame is None or getattr(frame, "empty", True):
        return []
    return [int(d) for d in frame["TRADE_DAYS"].astype(int)]


_CALENDAR_CACHE: dict[tuple[str, str, str], list[int]] = {}


def _cache_dir() -> str:
    """日历缓存目录。跟着本地数据根走，两处配置只改一个。"""
    return os.path.join(_local_root(), "calendar")


def _local_root() -> str:
    root = os.environ.get("TGW_LOCAL_DATA_ROOT") or DEFAULT_LOCAL_DATA_ROOT
    return os.path.abspath(os.path.expanduser(root))


def _disk_cache_path(market: str, first_day: str) -> str:
    return os.path.join(_cache_dir(), f"{market}-{first_day}.json")


def _read_disk_cache(market: str, first_day: str, end_dt) -> list[int] | None:
    """从磁盘读日历，够不到 `end` 就返回 None。

    缓存键**不含 end**：日历是只增的历史，写进去的只会比请求的更早。
    键里带 end 的话每天换一次文件名，缓存等于没有。
    """
    path = _disk_cache_path(market, first_day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            days = [int(d) for d in json.load(fh)]
    except (OSError, ValueError, TypeError):
        return None       # 缓存坏了就当没有，重拼一遍而已
    if not days or days[-1] < int(end_dt.strftime("%Y%m%d")):
        return None       # 缓存比要的新鲜度不够（跨天了），得重拼
    return days


def _write_disk_cache(market: str, first_day: str, days: list[int]) -> None:
    """落盘。写失败不影响主流程——缓存是加速器，不是必需品。"""
    path = _disk_cache_path(market, first_day)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(days, fh)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"警告：交易日历缓存写不进去（{exc}），下个进程要重拼", file=sys.stderr)


def full_calendar(market: str, end: str,
                  first_day: str = CALENDAR_FIRST_DAY_DEFAULT) -> list[int]:
    """从 `first_day` 到 `end` 的完整交易日历，按时间窗分段拼。

    **必须一路拼回 `first_day`。** 只拼最近一个窗口的话，老日期区间的
    `query_kline` 会静默返回 `{}`——正是这个补丁要消灭的那类静默截断。

    窗口取满 `SERVER_ROW_LIMIT` 行说明它被截断了，继续往前取；走到
    `first_day` 或 `MAX_SEGMENTS` 用完为止。后者会抛
    `IncompleteCalendar`，不把截断当完整。

    两级缓存：进程内 + 磁盘。磁盘那级是给分批任务准备的——一次
    `get_calendar` 要 12 个请求，105 批就是 1260 个，光日历就够把网关
    打到 404。跨天之后缓存的 `end` 不够新，会自动重拼。

    结果按 `(market, end, first_day)` 缓存于进程内——一次进程里
    `get_calendar` 会被多处调用（`MarketData` 构造、因子 reindex）。
    """
    key = (market, str(end)[:8], first_day)
    cached = _CALENDAR_CACHE.get(key)
    if cached is not None:
        return cached

    market_wire = MARKET_WIRE.get(market)
    if market_wire is None:
        raise IncompleteCalendar(
            f"未知市场 {market!r}，可选项 {sorted(MARKET_WIRE)}"
        )
    end_dt = _dt.datetime.strptime(str(end)[:8], "%Y%m%d").date()
    start_dt = _dt.datetime.strptime(first_day, "%Y%m%d").date()
    if start_dt > end_dt:
        return []

    on_disk = _read_disk_cache(market, first_day, end_dt)
    if on_disk is not None:
        _CALENDAR_CACHE[key] = on_disk
        return on_disk

    days: list[int] = []
    cursor_end = end_dt
    windows_with_data = 0
    for _ in range(MAX_SEGMENTS):
        cursor_start = max(
            start_dt,
            cursor_end - _dt.timedelta(days=365 * SEGMENT_YEARS),
        )
        window = _fetch_window(
            market_wire,
            cursor_start.strftime("%Y%m%d"),
            cursor_end.strftime("%Y%m%d"),
        )
        days.extend(window)
        if window:
            windows_with_data += 1
        if cursor_start <= start_dt:
            break
        # 窗口没取满（<1000 行）说明**这个窗口本身是全的**，不是「后面没数据了」。
        # 继续往前拼，一直拼到 first_day。
        cursor_end = cursor_start - _dt.timedelta(days=1)
    else:
        raise IncompleteCalendar(
            f"交易日历取不全：已回溯 {MAX_SEGMENTS} 个 {SEGMENT_YEARS} 年窗口，"
            f"仍没走到 {first_day}。服务端单次上限 {SERVER_ROW_LIMIT} 行，"
            f"请调大 MAX_SEGMENTS 或调小 SEGMENT_YEARS。"
        )

    uniq = sorted(set(days))
    if not uniq:
        raise IncompleteCalendar(
            f"交易日历为空：{market} {first_day}~{end}，"
            f"回溯了 {windows_with_data} 个有数据的窗口都没有结果"
        )
    earliest = _dt.datetime.strptime(str(uniq[0]), "%Y%m%d").date()
    if earliest > start_dt:
        # 拼到 first_day 了，但最早的交易日还在它之后——中间有整段空窗，
        # 要么市场没数据，要么某个窗口被截断了。两种都值得报出来。
        gap_days = (earliest - start_dt).days
        if gap_days > 400:
            raise IncompleteCalendar(
                f"交易日历最早只到 {uniq[0]}，比要求的 {first_day} 晚了 {gap_days} 天，"
                f"中间疑似整段没取到"
            )
    _CALENDAR_CACHE[key] = uniq
    _write_disk_cache(market, first_day, uniq)
    return uniq


def _shape(days: list[int], data_type: str) -> Any:
    """按业务层的约定出形状：`datetime` 走 datetime，其余出 int 列表。"""
    if data_type == "datetime":
        return [_dt.datetime.strptime(str(d), "%Y%m%d") for d in days]
    return days


def _first_day() -> str:
    return os.environ.get("TGW_CALENDAR_FIRST_DAY", CALENDAR_FIRST_DAY_DEFAULT)


def patch_calendar(ad: Any, *, end_today: str | None = None) -> None:
    """替换 `BaseData.get_calendar`，绕开 1000 行上限。

    替换的是**类上的方法**而不是调用点——业务层内部多处（`MarketData`
    构造、`get_backward_factor` 的 reindex）都调它，改调用点会漏。

    `end_today` 只给测试用，线上走当天。
    """
    original = ad.BaseData.get_calendar

    def get_calendar(self, data_type: str = "str", market: str = "SH",
                     date: int | None = None, *args: Any, **kwargs: Any) -> Any:
        # `date` 是**结束日期**，不是「查某一天的日历」——业务层
        # `MarketData` 构造和 `get_backward_factor` 都传当天。
        # 所以无论有没有传 date 都走分段查询，只有没传才用今天。
        end = str(date)[:8] if date else (end_today or _today())
        return _shape(full_calendar(market, end, _first_day()), data_type)

    get_calendar.__doc__ = (
        "交易日历（按时间窗分段，绕开服务端 1000 行上限）。\n\n"
        "替换 `BaseData.get_calendar` 原实现——原版把 start_date 写死成\n"
        "19900101，而服务端单次只回 1000 行且 offset 无效，日历会止于\n"
        "1994 年，进而让 query_kline 静默返回 {}。\n\n"
        "原实现: %r" % (original,)
    )
    ad.BaseData.get_calendar = get_calendar


def _today() -> str:
    import time
    return time.strftime("%Y%m%d")


#: 未设 `TGW_LOCAL_DATA_ROOT` 时的默认本地数据根。放在家目录而不是仓库里——
#: 业务层的默认值是 Windows 路径 `D://AmazingData_local_data//`，在 macOS
#: 上会原样落成仓库下的 `./D:/...` 目录，git status 一片脏。
DEFAULT_LOCAL_DATA_ROOT = "~/.amazingdata_local"


def patch_local_data_root(ad: Any, root: str | None = None) -> str:
    """把业务层的本地数据根从 Windows 默认值换成可用路径。

    优先级：显式参数 > `TGW_LOCAL_DATA_ROOT` > `DEFAULT_LOCAL_DATA_ROOT`。
    **不给 None**——默认值是 Windows 路径，不覆盖就会在仓库里生成
    `./D:/AmazingData_local_data/`。见模块开头第 2 节。
    """
    root = root or os.environ.get("TGW_LOCAL_DATA_ROOT") or DEFAULT_LOCAL_DATA_ROOT
    # 业务层是拿 local_path 直接拼 `basedata/...` 的（不加分隔符），
    # 所以这里必须自带结尾斜杠，否则会拼成 `/tmp/adlocalbasedata`。
    root = os.path.abspath(os.path.expanduser(root)).rstrip("/" + os.sep) + os.sep
    os.makedirs(root, exist_ok=True)
    for cls_name in ("BaseData", "InfoData"):
        cls = getattr(ad, cls_name, None)
        if cls is None:
            continue
        for meth_name in dir(cls):
            if not meth_name.startswith("get_"):
                continue
            meth = getattr(cls, meth_name, None)
            try:
                import inspect
                sig = inspect.signature(meth)
            except (TypeError, ValueError):
                continue
            if "local_path" not in sig.parameters:
                continue
            if sig.parameters["local_path"].default == root:
                continue
            setattr(cls, meth_name, _with_local_root(meth, root))
    return root


def _with_local_root(meth, root: str):
    """把 `local_path` 的默认值换成 `root`。"""
    import functools
    import inspect

    sig = inspect.signature(meth)
    params = list(sig.parameters.values())
    for i, p in enumerate(params):
        if p.name == "local_path" and p.default is not inspect.Parameter.empty:
            params[i] = p.replace(default=root)
    new_sig = sig.replace(parameters=params)

    @functools.wraps(meth)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "local_path" not in kwargs:
            bound = new_sig.bind_partial(*args)
            if "local_path" not in bound.arguments:
                kwargs["local_path"] = root
        return meth(*args, **kwargs)

    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    return wrapper


def apply_all(ad: Any, *, end_today: str | None = None) -> dict[str, Any]:
    """把 `patches.py` 里所有修正应用到刚 import 的业务层。

    调用点只有 `cli.load_amazingdata()` 一处。
    """
    applied: dict[str, Any] = {}
    patch_calendar(ad, end_today=end_today)
    applied["calendar"] = "分段拼接 + 取满 1000 行报错"
    root = patch_local_data_root(ad)
    if root:
        applied["local_data_root"] = root
    return applied
