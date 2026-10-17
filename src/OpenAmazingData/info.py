"""资讯族查询：`InfoData`。

55 个方法按缓存形态分成五族，每族一个引擎，方法差异写进表项：

* `PerCode`   —— 按代码一个缓存文件，增量下载，返回 `{代码: DataFrame}`。
* `WholeTable`—— 全部代码合在一个缓存文件，增量下载，返回过滤后的单张表。
* `Snapshot`  —— 没有日期维度的整表（可转债、期权），刷新传入的代码，返回单张表。
* `Members`   —— 成分股，按代码一个文件，只写不增量。
* 单张全市场表（北交所代码对照、行业分类、融资融券汇总）与无缓存的证券基础信息。

目录、文件名、增量列、批量、改名表、列清单都从厂商 .pyc 字节码抽出；引擎的
逐步行为在私有记录仓里由等价门禁逐项核对。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from . import _gateway as gw
from . import _store as store
from ._local import DEFAULT_LOCAL_PATH

FIRST_DAY = "19900101"
LAST_DAY = "20980101"

Frame = pd.DataFrame
Hook = Callable[[Frame], Frame]


def _same(frame: Frame) -> Frame:
    return frame


def _start_after(last_value: Any) -> str:
    """本地最后一条的日期 → 增量请求的 start_date。

    厂商的写法是 `str(datetime_to_int(date_to_datetime(max)))`：变量名叫
    next_day，实际没加一天，增量请求从最后一天本身开始，重叠那天靠
    `drop_duplicates` 去掉。列值不是 8 位日期串会在 `strptime` 抛错，照样抛。
    """
    return str(int(datetime.strptime(last_value, "%Y%m%d").strftime("%Y%m%d")))


def _window(kwargs: dict, end_default: str) -> tuple[str | None, str]:
    """`begin_date` 与 `end_date` 都给了才算指定区间，否则走本地增量。"""
    if "begin_date" in kwargs and "end_date" in kwargs:
        return str(kwargs["begin_date"]), str(kwargs["end_date"])
    return None, end_default


def _conform(frame: Frame, columns: tuple[str, ...]) -> Frame:
    """补齐缺的列（NaN），再按列清单取列。"""
    missing = [c for c in columns if c not in list(frame.columns)]
    frame[missing] = np.nan
    return frame[list(columns)]


@dataclass(frozen=True)
class Table:
    """一个资讯方法的静态事实，全部来自接口规范。"""

    attr: str                          # 实例上缓存结果的属性名
    function_id: str
    dirs: tuple[str, ...]              # 缓存目录，相对本地数据根
    label: str                         # 日志里的中文名
    columns: tuple[str, ...]           # 输出列清单（字节码 columns_list）
    batch_len: int = 200
    code_param: str = "market_code"
    inc_col: str = ""                  # 增量列
    file: str = ""                     # 整表文件名；按代码存的留空
    start_default: str = FIRST_DAY
    end_default: str = LAST_DAY
    prepare: Hook = _same              # 每只代码的回包在拼表前做的变换
    finish: Hook = _same               # 选列之后的变换
    read_columns: tuple[str, ...] | None = None  # 读本地时取的列，缺省同 columns

    def directory(self, local_path: str) -> str:
        return store.table_dir(local_path, *self.dirs)


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------


class PerCode:
    """按代码一个文件、按增量列续传。"""

    @staticmethod
    def download(t: Table, local_path: str, codes: list, **kwargs) -> dict:
        begin, end = _window(kwargs, t.end_default)
        path = t.directory(local_path)
        local: dict[Any, Frame] = {}
        start: dict[Any, str] = {}
        for code in codes:
            if begin is not None:
                local[code], start[code] = pd.DataFrame(), begin
                continue
            try:
                local[code] = store.read_table(path, code)
                start[code] = _start_after(local[code][t.inc_col].max())
            except FileNotFoundError:
                print(f"{path}  本地无{code} “{t.label}”")
                local[code], start[code] = pd.DataFrame(), t.start_default
        fetched = gw.fetch_each(codes, lambda c: (
            ("function_id", t.function_id), (t.code_param, c),
            ("start_date", start[c]), ("end_date", end)), t.batch_len)
        print(f"{t.label} 下载完成")
        result = {}
        for code in codes:
            frame = _conform(t.prepare(fetched[code]), t.columns)
            merged = pd.concat([local[code], frame]).drop_duplicates()
            merged = t.finish(merged)
            result[code] = merged
            if not merged.empty and begin is None:
                store.save_table(path, code, merged)
        return result

    @staticmethod
    def query(obj, t: Table, codes: list, local_path: str, is_local: bool, kwargs: dict) -> dict:
        if "begin_date" in kwargs:
            value = PerCode.download(t, local_path, codes, **kwargs)
        elif is_local:
            try:
                cache = getattr(obj, t.attr)
                path = t.directory(local_path)
                for code in codes:
                    cache[code] = store.read_table(path, code)
                return cache
            except FileNotFoundError:
                value = PerCode.download(t, local_path, codes)
        else:
            value = PerCode.download(t, local_path, codes)
        setattr(obj, t.attr, value)
        return value


class WholeTable:
    """所有代码合在一个文件、按增量列续传。"""

    @staticmethod
    def download(t: Table, local_path: str, codes: list, **kwargs) -> Frame:
        begin, end = _window(kwargs, t.end_default)
        path = t.directory(local_path)
        local = pd.DataFrame()
        if begin is None:
            try:
                local = store.read_table(path, t.file)
            except FileNotFoundError:
                print(path + f"   本地无 “{t.label}”")
        groups = dict(tuple(local.groupby("MARKET_CODE"))) if not local.empty else {}
        start: dict[Any, str] = {}
        for code in codes:
            if begin is not None:
                start[code] = begin
            elif code in groups:
                start[code] = _start_after(groups[code][t.inc_col].max())
            else:
                start[code] = t.start_default
        fetched = gw.fetch_each(codes, lambda c: (
            ("function_id", t.function_id), (t.code_param, c),
            ("start_date", start[c]), ("end_date", end)), t.batch_len)
        print(f"{t.label} 下载完成")
        frames = [t.prepare(f) for f in fetched.values()]
        merged = pd.concat([local] + frames).drop_duplicates()
        merged = t.finish(_conform(merged, t.columns))
        if not merged.empty and begin is None:
            store.save_table(path, t.file, merged)
        return merged[merged["MARKET_CODE"].isin(codes)]

    @staticmethod
    def query(obj, t: Table, codes: list, local_path: str, is_local: bool, kwargs: dict) -> Frame:
        if "begin_date" in kwargs:
            value = WholeTable.download(t, local_path, codes, **kwargs)
        elif is_local:
            try:
                frame = store.read_table(t.directory(local_path), t.file)
                frame = frame[list(t.read_columns or t.columns)]
                value = frame[frame["MARKET_CODE"].isin(codes)]
            except FileNotFoundError:
                value = WholeTable.download(t, local_path, codes)
        else:
            value = WholeTable.download(t, local_path, codes)
        setattr(obj, t.attr, value)
        return value


class Snapshot:
    """没有日期维度的整表：本地表去掉这批代码，重新下载后拼回去。"""

    @staticmethod
    def download(t: Table, local_path: str, codes: list) -> Frame:
        path = t.directory(local_path)
        local = pd.DataFrame()
        try:
            local = store.read_table(path, t.file)
        except FileNotFoundError:
            print(path + f"   本地无 “ {t.label}”")
        if not local.empty:
            local = local[~local["MARKET_CODE"].isin(codes)]
        fetched = gw.fetch_each(codes, lambda c: (
            ("function_id", t.function_id), (t.code_param, c)), t.batch_len)
        print(f"{t.label} 下载完成")
        frames = [t.prepare(f) for f in fetched.values()]
        merged = pd.concat([local] + frames).drop_duplicates()
        merged = _conform(merged, t.columns)
        if not merged.empty:
            # V02：厂商 download_kzz_put_explanation 读的是自己的表，落盘却写
            # 兄弟表 kzz_put_call_item.h5（recovered/.../download_info_data.py
            # 3485 行，save_data_to_hdf5 传的是 LocalDataFolder.Kzz_Put_Call_Item）。
            # 那次下载把回售赎回条款那张表覆盖成 15 列的回售执行说明，读的一侧
            # 又按 kzz_put_explanation.h5 找——厂商自己从来不写那个文件。
            # 与它自己「一张表一个文件」的意图矛盾，写的一侧跟着读的一侧走。
            store.save_table(path, t.file, merged)
        return merged[merged["MARKET_CODE"].isin(codes)]

    @staticmethod
    def query(obj, t: Table, codes: list, local_path: str, is_local: bool) -> Frame:
        # V01：厂商的 get_kzz_put_call_item / get_kzz_put_explanation /
        # get_kzz_call_explanation 读本地分支取的是 LocalDataFolder 上不存在的
        # 成员（Kzz_Put_call_item / Kzz_Put_explanation / Kzz_Call_explanation，
        # 枚举里的实名是 Kzz_Put_Call_Item / Kzz_Put_Explanation /
        # Kzz_Call_Explanation），is_local=True 必然 AttributeError，一个请求
        # 也发不出去。同一族的 get_kzz_call 等九个方法读的是自己的表、读不到就
        # 回落下载。这里照那九个方法走：读各方法自己的缓存文件，读不到回落下载。
        try:
            if not is_local:
                value = Snapshot.download(t, local_path, codes)
            else:
                frame = store.read_table(t.directory(local_path), t.file)
                frame = frame[list(t.columns)]
                value = frame[frame["MARKET_CODE"].isin(codes)]
        except FileNotFoundError:
            value = Snapshot.download(t, local_path, codes)
        setattr(obj, t.attr, value)
        return value


class OptionSnapshot:
    """期权三表：只下载本地还没有的代码，返回整张表（不按代码过滤）。"""

    @staticmethod
    def download(t: Table, local_path: str, codes: list) -> Frame:
        path = t.directory(local_path)
        local = pd.DataFrame()
        try:
            local = store.read_table(path, t.file)
            have = list(local["MARKET_CODE"].unique())
        except FileNotFoundError:
            print(path + f"   本地无 “{t.label}”")
            have = []
        # 厂商写的是 list(set(...) - set(...))，顺序随哈希种子；只影响发送顺序。
        todo = list(set(codes) - set(have))
        fetched = gw.fetch_each(todo, lambda c: (
            ("function_id", t.function_id), (t.code_param, c)), t.batch_len)
        print(f"{t.label} 下载完成")
        frames = [f if f.empty else t.prepare(f) for f in fetched.values()]
        merged = pd.concat([local] + frames).drop_duplicates()
        merged = _conform(merged, t.columns)
        if not merged.empty:
            store.save_table(path, t.file, merged)
        return merged

    @staticmethod
    def query(obj, t: Table, codes: list, local_path: str, is_local: bool) -> Frame:
        try:
            if not is_local:
                value = OptionSnapshot.download(t, local_path, codes)
            else:
                frame = store.read_table(t.directory(local_path), t.file)
                frame = frame[list(t.columns)]
                value = frame[frame["MARKET_CODE"].isin(codes)]
        except FileNotFoundError:
            value = OptionSnapshot.download(t, local_path, codes)
        setattr(obj, t.attr, value)
        return value


class Members:
    """成分股：按代码一个文件，每次全量覆盖。读本地时缺的代码直接跳过，
    **不回落到下载**（厂商行为，`capability_spec.EXPECT` 有登记）。"""

    @staticmethod
    def download(t: Table, local_path: str, codes: list) -> dict:
        path = t.directory(local_path)
        fetched = gw.fetch_each(codes, lambda c: (
            ("function_id", t.function_id), (t.code_param, c)), t.batch_len)
        print(f"{t.label} 下载完成")
        result = {}
        for code in codes:
            frame = _conform(t.prepare(fetched[code]), t.columns)
            result[code] = frame
            if not frame.empty:
                store.save_table(path, code, frame)
        return result

    @staticmethod
    def query(obj, t: Table, codes: list, local_path: str, is_local: bool) -> dict:
        if is_local:
            cache = getattr(obj, t.attr)
            path = t.directory(local_path)
            for code in codes:
                frame = store.try_read_table(path, code)
                if frame is not None:
                    cache[code] = frame
            return cache
        value = Members.download(t, local_path, codes)
        setattr(obj, t.attr, value)
        return value


# --------------------------------------------------------------------------
# 方法表
# --------------------------------------------------------------------------


def _rename(mapping: dict[str, str]) -> Hook:
    return lambda frame: frame.rename(columns=mapping)


def _chain(*hooks: Hook) -> Hook:
    def run(frame: Frame) -> Frame:
        for hook in hooks:
            frame = hook(frame)
        return frame
    return run


def _option_contract_type(frame: Frame) -> Frame:
    frame["CONTRACT_TYPE"] = frame["CONTRACT_TYPE"].astype(str)
    frame["CONTRACT_TYPE"] = frame["CONTRACT_TYPE"].replace("708002000", "P")
    frame["CONTRACT_TYPE"] = frame["CONTRACT_TYPE"].replace("708001000", "C")
    return frame


from ._info_tables import TABLES  # noqa: E402  方法表依赖上面的 Table 与钩子


class InfoData:
    """资讯数据。每个方法的签名与厂商 `InfoData` 相同。"""

    def __init__(self) -> None:
        for entry in TABLES.values():
            empty = {} if entry.engine in (PerCode, Members) else pd.DataFrame()
            setattr(self, entry.table.attr, empty)
        from ._info_special import init_state

        init_state(self)


def _bind(name: str, t) -> None:
    engine = t.engine
    if engine in (PerCode, WholeTable):
        def method(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
            return engine.query(self, t.table, code_list, local_path, is_local, kwargs)
    else:
        def method(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True):
            return engine.query(self, t.table, code_list, local_path, is_local)
    method.__name__ = name
    method.__qualname__ = f"InfoData.{name}"
    method.__doc__ = f"{t.table.label}（{t.table.function_id}）。"
    setattr(InfoData, name, method)


for _name, _entry in TABLES.items():
    _bind(_name, _entry)

from ._info_special import SPECIAL_METHODS  # noqa: E402

for _name, _fn in SPECIAL_METHODS.items():
    setattr(InfoData, _name, _fn)
