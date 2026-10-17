"""InfoData：公告清单与公告 PDF 的两步链路。

第一步 `get_announcement_{stock,fund,bond}_list` 走网关取公告清单，整表一个缓存
文件，按 `PUBLISH_TIME` 增量；第二步 `get_announcement_{stock,fund,bond}` 拿
上一步的 DataFrame 当入参，逐条把 PDF 的落盘请求发给网关，返回
`({SOURCE_ID: 文件路径}, 筛过的清单)`。

第二步没有别的出口：PDF 正文是原生库按 `download_path` 写盘的，字节码里
业务层只发请求、自己不碰文件。这一步在真网关上带 `download_path` 跑通过没有，
未验证。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from . import _gateway as gw
from . import _store as store
from ._local import DEFAULT_LOCAL_PATH
from .info import FIRST_DAY, LAST_DAY, _conform, _window
from .security_type import is_security_type

#: 清单表列清单，取自字节码 `columns_list`（三支一样）。
_COLUMNS = ('MARKET_CODE', 'SOURCE_ID', 'TITLE', 'TAG_ID', 'TAG_NAME',
            'ONLINE_STATUS', 'PUBLISH_TIME', 'DATA_STATUS', 'COMPANY')

#: `download_announcement_*` 里「本地无缓存」那一行三个函数都打同一句
#: 「上市公司公告列表数据」——fund / bond 是从 stock 抄过来的，原样留着。
_MISSING_LABEL = "上市公司公告列表数据"

@dataclass(frozen=True)
class _Announcement:
    """一个品种的公告链路：两个 function_id、两个缓存目录、三组属性名。"""

    attr_list: str            # 清单表在实例上的属性名
    attr_tag: str             # 按 tag / 日期筛过的清单
    attr_pdf: str             # {SOURCE_ID: 文件路径}
    list_id: str              # 清单的 function_id
    pdf_id: str               # PDF 下载的 function_id
    cache_file: str           # 清单缓存文件名，也是 PDF 目录名
    security_type: str        # 清单只查这类代码，见 `security_type.py`
    done_label: str           # 下载完成那一行打的字


_STOCK = _Announcement(
    attr_list="announcement_stock_list_df", attr_tag="announcement_stock_list_tag_df",
    attr_pdf="announcement_stock_pdf_path", list_id="A020010001", pdf_id="A020010004",
    cache_file="announcement_stock", security_type="EXTRA_STOCK_A",
    done_label="上市公司公告列表")

_FUND = _Announcement(
    attr_list="announcement_fund_list_df", attr_tag="announcement_fund_list_tag_df",
    attr_pdf="announcement_fund_pdf_path", list_id="A020010002", pdf_id="A020010005",
    cache_file="announcement_fund", security_type="EXTRA_ETF",
    done_label="上市公司公告列表")  # 厂商 fund 这一支也打「上市」，抄来的

_BOND = _Announcement(
    attr_list="announcement_bond_list_df", attr_tag="announcement_bond_list_tag_df",
    attr_pdf="announcement_bond_pdf_path", list_id="A020010003", pdf_id="A020010006",
    cache_file="announcement_bond", security_type="EXTRA_KZZ",
    done_label="债券公告列表")


# --------------------------------------------------------------------------
# 第一步：公告清单
# --------------------------------------------------------------------------


def _table_dir(local_path: str) -> str:
    return store.table_dir(local_path, "infodata", "announcement")


def _resume_from(latest: str) -> str:
    """本地最后一条的 `PUBLISH_TIME` → 增量请求的 `start_date`。

    厂商是 `date_str_to_int(max[:10])`：取日期部分、去横线、转 int 再转回串。
    变量名叫 `next_day`，实际没加一天——增量请求从最后一天本身开始，重叠那
    行靠 `drop_duplicates` 去掉。
    """
    return str(int(latest[:10].replace("-", "")))


def _download_list(t: _Announcement, local_path: str, codes: list, **kwargs) -> pd.DataFrame:
    begin, end = _window(kwargs, LAST_DAY)
    path = _table_dir(local_path)
    local = pd.DataFrame()
    if begin is None:
        try:
            local = store.read_table(path, t.cache_file)
        except FileNotFoundError:
            print(path + f"   本地无 “{_MISSING_LABEL}”")
    groups = dict(tuple(local.groupby("MARKET_CODE"))) if not local.empty else {}
    start: dict[str, str] = {}
    for code in codes:
        if begin is not None:
            start[code] = begin
        elif code in groups:
            start[code] = _resume_from(groups[code]["PUBLISH_TIME"].max())
        else:
            start[code] = FIRST_DAY
    fetched = gw.fetch_each(codes, lambda c: (
        ("function_id", t.list_id), ("market_code", c),
        ("start_date", start[c]), ("end_date", end)), 200)
    print(f"{t.done_label} 下载完成")
    merged = pd.concat([local] + list(fetched.values())).drop_duplicates()
    merged = _conform(merged, _COLUMNS)
    if not merged.empty and begin is None:
        store.save_table(path, t.cache_file, merged)
    return merged[merged["MARKET_CODE"].isin(codes)]


def _list_query(self, t: _Announcement, code_list: list, local_path: str,
                is_local: bool, kwargs: dict) -> pd.DataFrame:
    # 厂商在这一层就把代码按证券类型筛掉，不是发出去再筛。
    codes = [code for code in code_list if is_security_type(code, t.security_type)]
    if "begin_date" in kwargs:
        value = _download_list(t, local_path, codes, **kwargs)
    elif is_local:
        try:
            frame = store.read_table(_table_dir(local_path), t.cache_file)
            frame = frame[list(_COLUMNS)]
            value = frame[frame["MARKET_CODE"].isin(codes)]
        except FileNotFoundError:
            value = _download_list(t, local_path, codes)
    else:
        value = _download_list(t, local_path, codes)
    setattr(self, t.attr_list, value)
    return value


# --------------------------------------------------------------------------
# 第二步：公告 PDF
# --------------------------------------------------------------------------


def _date_bound(value) -> str:
    """`str(date_to_datetime(str(x)))`，与 `PUBLISH_TIME` 同形。

    厂商把日期界也转成 `'YYYY-MM-DD HH:MM:SS'`，于是筛选退化成字符串比较。
    """
    return str(datetime.strptime(str(value), "%Y%m%d"))


def _pick_by_tag(frame: pd.DataFrame, tag_id_list: list) -> pd.DataFrame:
    """`TAG_ID` 是一串用 `|` 隔开的标签，`tag_id` 要落在串首或某个 `|` 之后。"""
    mask = pd.Series(False, index=frame.index)
    tags = frame["TAG_ID"].astype(str)
    for tag_id in tag_id_list:
        mask |= tags.str.contains("(?:^|\\|)" + str(tag_id))
    return frame[mask]


def _pdf(self, t: _Announcement, list_df: pd.DataFrame, tag_id_list: list | None,
         begin_date, end_date, local_path: str) -> tuple[dict, pd.DataFrame]:
    paths = getattr(self, t.attr_pdf)
    if list_df.empty:
        # V03 厂商缺陷：股票这一支的空表分支返回 `announcement_bond_stock_tag_df`
        # （`query_api/info_data.pyc:984`，`_list` 与 `_pdf` 的属性名混了），没人建
        # 过这个属性，传空表必然 AttributeError。照 fund / bond 两支返回自己的
        # `<品种>_list_tag_df`（那两支在 .pyc 的 1050 / 1116 行就是这么写的）。
        return (paths, getattr(self, t.attr_tag))
    picked = (_pick_by_tag(list_df, tag_id_list) if tag_id_list else list_df)
    begin, end = _date_bound(begin_date), _date_bound(end_date)
    picked = picked[(picked["PUBLISH_TIME"] >= begin) & (picked["PUBLISH_TIME"] <= end)]
    pdf_path = store.table_dir(local_path, "infodata", "announcement", t.cache_file + "_pdf")
    # 实例属性存的是换过索引的那张表，和返回值是同一个对象（厂商
    # `query_api/info_data.pyc` 把 set_index 的结果写回属性）。
    picked = picked.set_index("SOURCE_ID")
    setattr(self, t.attr_tag, picked)
    for source_id, code in dict(picked["MARKET_CODE"]).items():
        file_path = pdf_path + "/" + str(source_id) + "_" + code + ".pdf"
        print(file_path)
        # 目录尾巴已经带一个斜杠，厂商又拼一个，落盘路径上是双斜杠。
        if os.path.exists(file_path) and os.path.getsize(file_path) > 2:
            paths[str(source_id)] = file_path
            continue
        result = gw.fetch_one((("function_id", t.pdf_id), ("source_id", str(source_id)),
                               ("download_path", pdf_path)))
        if result is None:
            continue
        paths[str(source_id)] = file_path
    return (paths, picked)


# --------------------------------------------------------------------------
# 对外方法
# --------------------------------------------------------------------------


def get_announcement_stock_list(self, code_list, local_path=DEFAULT_LOCAL_PATH,
                                is_local=True, **kwargs):
    """上市公司公告列表（A020010001）。"""
    return _list_query(self, _STOCK, code_list, local_path, is_local, kwargs)


def get_announcement_stock(self, announcement_stock_list_df, tag_id_list=None,
                           begin_date=19900101, end_date=20980101,
                           local_path=DEFAULT_LOCAL_PATH):
    """上市公司公告 PDF（A020010004）。入参是 `get_announcement_stock_list` 的返回值。"""
    return _pdf(self, _STOCK, announcement_stock_list_df, tag_id_list, begin_date, end_date,
                local_path)


def get_announcement_fund_list(self, code_list, local_path=DEFAULT_LOCAL_PATH,
                               is_local=True, **kwargs):
    """基金公告列表（A020010002）。"""
    return _list_query(self, _FUND, code_list, local_path, is_local, kwargs)


def get_announcement_fund(self, announcement_fund_list_df, tag_id_list=None,
                          begin_date=19900101, end_date=20980101,
                          local_path=DEFAULT_LOCAL_PATH):
    """基金公告 PDF（A020010005）。入参是 `get_announcement_fund_list` 的返回值。"""
    return _pdf(self, _FUND, announcement_fund_list_df, tag_id_list, begin_date, end_date,
                local_path)


def get_announcement_bond_list(self, code_list, local_path=DEFAULT_LOCAL_PATH,
                               is_local=True, **kwargs):
    """债券公告列表（A020010003）。"""
    return _list_query(self, _BOND, code_list, local_path, is_local, kwargs)


def get_announcement_bond(self, announcement_bond_list_df, tag_id_list=None,
                          begin_date=19900101, end_date=20980101,
                          local_path=DEFAULT_LOCAL_PATH):
    """债券公告 PDF（A020010006）。入参是 `get_announcement_bond_list` 的返回值。"""
    return _pdf(self, _BOND, announcement_bond_list_df, tag_id_list, begin_date, end_date,
                local_path)


METHODS = {
    "get_announcement_stock_list": get_announcement_stock_list,
    "get_announcement_stock": get_announcement_stock,
    "get_announcement_fund_list": get_announcement_fund_list,
    "get_announcement_fund": get_announcement_fund,
    "get_announcement_bond_list": get_announcement_bond_list,
    "get_announcement_bond": get_announcement_bond,
}
