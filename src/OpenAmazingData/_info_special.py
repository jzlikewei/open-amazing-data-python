"""InfoData 里不归任何一族的方法。

本文件放两类：

* 单张全市场表：证券基础信息（不缓存）、融资融券汇总、北交所新旧代码对照、
  行业分类；
* 有专门流程的方法分在 `_info_series` / `_info_text` / `_info_announcement`，
  这里只汇总进方法表。
"""

from __future__ import annotations

import pandas as pd

from . import _gateway as gw
from . import _store as store
from ._local import DEFAULT_LOCAL_PATH
from .info import LAST_DAY, FIRST_DAY, _conform, _window

_STOCK_BASIC_COLUMNS = ("MARKET_CODE", "SECURITY_NAME", "COMP_NAME", "PINYIN", "COMP_NAME_ENG",
                        "LISTDATE", "DELISTDATE", "LISTPLATE_NAME", "COMP_SNAME_ENG", "IS_LISTED")
_MARGIN_SUMMARY_COLUMNS = ("TRADE_DATE", "EXCHANGE", "SUM_BORROW_MONEY_BALANCE",
                           "SUM_PURCH_WITH_BORROW_MONEY", "SUM_REPAYMENT_OF_BORROW_MONEY",
                           "SUM_SEC_LENDING_BALANCE", "SUM_SALES_OF_BORROWED_SEC",
                           "SUM_MARGIN_TRADE_BALANCE")

#: 实例上的结果属性。厂商 `InfoData.__init__` 建的，读本地分支会往里累积。
_STATE = {
    "stock_basic": pd.DataFrame, "margin_summary": dict, "bj_code_mapping": dict,
    "industry_base_info": dict, "history_stock_status": dict, "index_weight": dict,
    "industry_weight": dict, "industry_daily": dict, "treasury_yield": dict,
    "announcement_stock_list_df": pd.DataFrame, "announcement_stock_pdf_path": dict,
    "announcement_stock_list_tag_df": pd.DataFrame,
    "announcement_fund_list_df": pd.DataFrame, "announcement_fund_pdf_path": dict,
    "announcement_fund_list_tag_df": pd.DataFrame,
    "announcement_bond_list_df": pd.DataFrame, "announcement_bond_pdf_path": dict,
    "announcement_bond_list_tag_df": pd.DataFrame,
    "report_abstract": dict, "news_list": dict, "news_detail_html_dir": dict,
}


def init_state(obj) -> None:
    for name, kind in _STATE.items():
        setattr(obj, name, kind())


# --------------------------------------------------------------------------
# 单张全市场表
# --------------------------------------------------------------------------


def get_stock_basic(self, code_list):
    """证券基础信息（A010010001）。不读不写本地缓存。"""
    fetched = gw.fetch_each(code_list, lambda c: (
        ("function_id", "A010010001"), ("market_code", c)), 200)
    print("证券信息数据 下载完成")
    frame = pd.concat([fetched[c] for c in code_list if c in fetched], axis=0)
    self.stock_basic = _conform(frame, _STOCK_BASIC_COLUMNS)
    return self.stock_basic


def _download_margin_summary(local_path: str, **kwargs) -> pd.DataFrame:
    begin, end = _window(kwargs, LAST_DAY)
    path = store.table_dir(local_path, "infodata", "margin_summary")
    local = pd.DataFrame()
    if begin is None:
        try:
            local = store.read_table(path, "margin_summary")
        except FileNotFoundError:
            print(path + "   本地无 “融资融券成交汇总数据”")
    if begin is not None:
        start, kept = begin, pd.DataFrame()
    elif not local.empty:
        # 厂商取的是最早一天（min），每次都从头重拉一遍，靠去重合并。
        start, kept = local["TRADE_DATE"].min(), local
    else:
        start, kept = FIRST_DAY, pd.DataFrame()
    fresh = gw.fetch_one((("function_id", "A010080002"), ("start_date", start),
                          ("end_date", end)))
    fresh = fresh[list(_MARGIN_SUMMARY_COLUMNS)]
    print("融资融券成交汇总数据 下载完成")
    merged = pd.concat([kept, fresh]).drop_duplicates()
    if not merged.empty and begin is None:
        store.save_table(path, "margin_summary", merged)
    return merged


def get_margin_summary(self, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
    """融资融券成交汇总（A010080002）。全市场一张表，不分代码。"""
    if "begin_date" in kwargs:
        self.margin_summary = _download_margin_summary(local_path, **kwargs)
    elif is_local:
        try:
            self.margin_summary = store.read_table(
                store.table_dir(local_path, "infodata", "margin_summary"), "margin_summary")
        except FileNotFoundError:
            self.margin_summary = _download_margin_summary(local_path)
    else:
        self.margin_summary = _download_margin_summary(local_path)
    return self.margin_summary


def _whole_market(local_path: str, name: str, function_id: str, adjust=None) -> pd.DataFrame:
    """无参数的全市场表：拉一次，非空就整表覆盖缓存。"""
    frame = gw.fetch_one((("function_id", function_id),))
    if adjust is not None:
        frame = adjust(frame)
    if not frame.empty:
        store.save_table(store.table_dir(local_path, "basedata", name), name, frame)
    return frame


def _cached_whole_market(self, attr: str, local_path: str, is_local: bool, name: str,
                         function_id: str, adjust=None):
    value = None
    if is_local:
        value = store.try_read_table(store.table_dir(local_path, "basedata", name), name)
    if value is None:
        value = _whole_market(local_path, name, function_id, adjust)
    setattr(self, attr, value)
    return value


def get_bj_code_mapping(self, local_path=DEFAULT_LOCAL_PATH, is_local=True):
    """北交所新旧代码对照（A010010010）。"""
    return _cached_whole_market(self, "bj_code_mapping", local_path, is_local,
                                "bj_code_mapping", "A010010010")


def _industry_suffix(frame: pd.DataFrame) -> pd.DataFrame:
    # 服务端回的是裸码，业务层统一补 `.SI`（申万行业指数后缀）。
    frame["INDEX_CODE"] = frame["INDEX_CODE"] + ".SI"
    return frame


def get_industry_base_info(self, local_path=DEFAULT_LOCAL_PATH, is_local=True):
    """行业指数分类（A010200006）。"""
    return _cached_whole_market(self, "industry_base_info", local_path, is_local,
                                "industry_base_info", "A010200006", _industry_suffix)


# --------------------------------------------------------------------------
# 方法表：专门流程分在三个模块里，各自导出 METHODS
# --------------------------------------------------------------------------

from ._info_announcement import METHODS as _ANNOUNCEMENT  # noqa: E402
from ._info_series import METHODS as _SERIES  # noqa: E402
from ._info_text import METHODS as _TEXT  # noqa: E402

SPECIAL_METHODS = {
    "get_stock_basic": get_stock_basic,
    "get_margin_summary": get_margin_summary,
    "get_bj_code_mapping": get_bj_code_mapping,
    "get_industry_base_info": get_industry_base_info,
    **_SERIES, **_TEXT, **_ANNOUNCEMENT,
}
