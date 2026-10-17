"""InfoData：研报摘要与新闻。

两个数据族四个方法：研报摘要（查询 + 导出 md）、新闻（列表 + 正文 html）。

两族共同的形状（`download_report_abstract` / `download_news_list`）：

* 传给网关的代码是**去掉交易所后缀的六位码**——查询层用
  `{code[:6]: code}` 建字典，下载层拿字典的键发请求，回包字典再换回原代码；
* 没给 `begin_date` 时按增量列续传：本地表的增量列最大值就是下次的
  `start_date`，本地没有缓存就从 `20200101` 起；
* 回包是空表的代码整个不进返回字典。

研报摘要多一层：代码 `'0'` 不走「一只代码一条请求」，改成按天切窗、
每窗 5 天发一条 `symbol=0` 的请求（`download_report_abstract` 里
`all_code_list_symbol` 与 `if '0' in all_code_list` 两处）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import pandas as pd

from . import _gateway as gw
from . import _store as store
from ._local import DEFAULT_LOCAL_PATH
from .info import _conform, _start_after, _window

REPORT_FUNCTION_ID = "B010010001"
NEWS_LIST_FUNCTION_ID = "B010010002"
NEWS_DETAIL_FUNCTION_ID = "B010010003"

REPORT_DIR = ("infodata", "report_abstract")
NEWS_LIST_DIR = ("infodata", "news", "news_list")
NEWS_HTML_DIR = ("infodata", "news", "news_html")

#: 本地没有缓存时增量请求的起点。接口规范 `date_constants` 的 20200101。
FIRST_DAY = "20200101"

#: 四个 `QueryPara` 批量：研报按代码、研报按天、新闻列表按代码、新闻正文按 id。
REPORT_CODE_BATCH = 5
REPORT_DATE_BATCH = 5
NEWS_LIST_BATCH = 10
NEWS_ID_BATCH = 200

#: 两个 `columns_list`（字节码常量），回包缺的列补 NaN 后按它取列。
REPORT_COLUMNS = ("id", "title", "des", "date", "author", "source", "bcpj", "pjbh",
                  "symbol", "industry", "prmax", "prmin", "classify", "update_date")
NEWS_LIST_COLUMNS = ("news_id", "stock_code", "create_date", "data_status")


def _today() -> str:
    """请求里 `end_date` 的缺省值：厂商取 `datetime_to_int()`，即系统当天。

    刻意不走 `_clock.today()`：等价测试把 `_clock.fixed_today` 钉成 20251231，
    厂商 .pyc 那条链路走的是真实系统日期，而 `requests` 是逐字比较的。
    """
    return time.strftime("%Y%m%d")


def _derive_date(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    """`update_time` / `create_time` → `update_date` / `create_date`（8 位串）。"""
    frame[column + "_date"] = pd.to_datetime(frame[column + "_time"]).dt.strftime("%Y%m%d")
    return frame


def _local_starts(codes: list, path: str, label: str, begin: str | None,
                  inc_col: str) -> tuple[dict, dict]:
    """每只代码的本地表与增量起点。

    指定了区间就跳过本地（本地表置空、起点取 `begin_date`）；没指定就读缓存，
    起点取增量列的最大值。缓存不存在从 `FIRST_DAY` 起。
    """
    local: dict = {}
    start: dict = {}
    for code in codes:
        if begin is not None:
            local[code], start[code] = pd.DataFrame(), begin
            continue
        try:
            local[code] = store.read_table(path, code)
            start[code] = _start_after(local[code][inc_col].max())
        except FileNotFoundError:
            print(f"{path}  本地无{code} “{label}”")
            local[code], start[code] = pd.DataFrame(), FIRST_DAY
    return local, start


def _assemble(codes: list, local: dict, fetched: dict, columns: tuple[str, ...],
              derive: Callable[[pd.DataFrame], pd.DataFrame], path: str,
              save: bool) -> dict:
    """拼表：回包空 → 整个代码不进结果；有回包才与本地表去重拼接。

    `save` 为假（指定了区间）时不写缓存。厂商的 `result[code] = 本地表` 那一支
    不写盘，这里也跟着不写。
    """
    result: dict = {}
    for code in codes:
        if code not in fetched:
            result[code] = local[code]
            continue
        if fetched[code].empty:
            continue
        frame = _conform(derive(fetched[code]), columns)
        merged = pd.concat([local[code], frame]).drop_duplicates()
        result[code] = merged
        if save and not merged.empty:
            store.save_table(path, code, merged)
    return result


# --------------------------------------------------------------------------
# 研报摘要
# --------------------------------------------------------------------------


def _report_whole_market(begin: str, end: str) -> pd.DataFrame:
    """代码 `'0'` 那一支：按天切窗拉全市场，窗口 5 天，同步等回包。

    单窗回包是 `None` 就跳过这一窗，不重试（厂商闭包 `get_report_abstract_date`
    里只有一个 `if req_date_result is None: continue`）。`begin` 为 `None` 时
    `pd.date_range` 自己抛 `TypeError`，与厂商同。
    """
    days = pd.date_range(begin, end, freq="D").strftime("%Y%m%d").tolist()
    frames = []
    for i in range(0, len(days), REPORT_DATE_BATCH):
        window = days[i:i + REPORT_DATE_BATCH]
        got = gw.fetch_one((("function_id", REPORT_FUNCTION_ID), ("symbol", "0"),
                            ("start_date", window[0]), ("end_date", window[-1])))
        if got is None:
            continue
        frames.append(got)
    return pd.concat(frames)


def _download_report_abstract(local_path: str, codes: list, **kwargs) -> dict:
    begin, end = _window(kwargs, _today())
    path = store.table_dir(local_path, *REPORT_DIR)
    local, start = _local_starts(codes, path, "研报摘要数据", begin, "update_date")
    # 代码 '0' 留给按天分窗那一支，它不占「一只代码一条请求」的批次。
    symbols = [code for code in codes if code != "0"]
    fetched = gw.fetch_each(symbols, lambda code: (
        ("function_id", REPORT_FUNCTION_ID), ("symbol", code),
        ("start_date", start[code]), ("end_date", end)), REPORT_CODE_BATCH)
    if "0" in codes:
        fetched["0"] = _report_whole_market(begin, end)
    print("研报摘要数据 下载完成")
    return _assemble(codes, local, fetched, REPORT_COLUMNS,
                     lambda frame: _derive_date(frame, "update"), path, begin is None)


def get_report_abstract(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True,
                        **kwargs):
    """研报摘要（B010010001）。按六位码缓存，返回 `{原代码: DataFrame}`。"""
    full = {code[:6]: code for code in code_list}
    codes = list(full)
    if "begin_date" not in kwargs:
        try:
            if is_local:
                path = store.table_dir(local_path, *REPORT_DIR)
                for code in codes:
                    self.report_abstract[code] = store.read_table(path, code)
            else:
                self.report_abstract = _download_report_abstract(local_path, codes, **kwargs)
        except FileNotFoundError:
            self.report_abstract = _download_report_abstract(local_path, codes)
    else:
        self.report_abstract = _download_report_abstract(local_path, codes, **kwargs)
    self.report_abstract = {full[code]: self.report_abstract[code] for code in self.report_abstract}
    return self.report_abstract


#: md 里的元信息行，顺序与标签取自字节码的常量元组（info_data.pyc 的
#: `save_report_abstract_md`，源码行 1425）。
_MD_META = (("发布日期", "date"), ("作者", "author"), ("来源", "source"),
            ("行业", "industry"), ("标的", "symbol"), ("评级", "bcpj"),
            ("分类", "classify"), ("更新时间", "update_time"))


def _md_meta_line(label: str, value) -> str | None:
    """一条 meta 行。值是 `None` / 空串 / `'nan'` / `'None'` 的不写。

    厂商源码写的是 `if not str(value).strip() not in ('', 'nan', 'None'):
    continue`（字节码 info_data.pyc 第 1429 行的 `CONTAINS_OP 1` 加
    `POP_JUMP_IF_TRUE`），双重否定落在「命中空值集合就跳过」上。
    """
    if value is None:
        return None
    if str(value).strip() in ("", "nan", "None"):
        return None
    return f"- **{label!s}**：{value!s}"


def _is_blank(value) -> bool:
    return value is None or str(value).strip().lower() in ("", "nan", "none")


def save_report_abstract_md(self, report_abstract, local_path=DEFAULT_LOCAL_PATH):
    """把研报摘要写成 md，返回目录。文件名 `<id>.md`，已存在的跳过。"""
    # V08 厂商缺陷：厂商 `copy.copy` 浅拷贝字典，值还是调用方传进来的那批
    # DataFrame，于是下面加的 `file_name` 列留在调用方的表上。逐张拷一份，只动
    # 副本；返回值与落盘文件不变。
    data = {code: frame.copy() for code, frame in report_abstract.items()}
    md_dir = os.path.join(store.table_dir(local_path, *REPORT_DIR), "report_abstract_md")
    os.makedirs(md_dir, exist_ok=True)
    for code in data:
        data[code]["file_name"] = data[code]["id"].astype(str) + ".md"
        exists = data[code]["file_name"].map(
            lambda name: os.path.exists(os.path.join(md_dir, name)))
        data[code] = data[code][~exists]
        for _, row in data[code].iterrows():
            lines = ["# " + str(row.get("title", "")), ""]
            meta = []
            for label, key in _MD_META:
                line = _md_meta_line(label, row.get(key))
                if line is not None:
                    meta.append(line)
            if meta:
                lines += meta + [""]
            des = str(row.get("des", ""))
            if not _is_blank(des):
                lines += [des, ""]
            with open(os.path.join(md_dir, row["file_name"]), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
    return md_dir


# --------------------------------------------------------------------------
# 新闻
# --------------------------------------------------------------------------


def _download_news_list(local_path: str, codes: list, **kwargs) -> dict:
    begin, end = _window(kwargs, _today())
    path = store.table_dir(local_path, *NEWS_LIST_DIR)
    local, start = _local_starts(codes, path, "新闻数据", begin, "create_date")
    fetched = gw.fetch_each(codes, lambda code: (
        ("function_id", NEWS_LIST_FUNCTION_ID), ("stock_code", code),
        ("start_date", start[code]), ("end_date", end)), NEWS_LIST_BATCH)
    print("新闻列表数据 下载完成")
    return _assemble(codes, local, fetched, NEWS_LIST_COLUMNS,
                     lambda frame: _derive_date(frame, "create"), path, begin is None)


def get_news_list(self, code_list, local_path=DEFAULT_LOCAL_PATH, is_local=True, **kwargs):
    """新闻列表（B010010002）。入参是 `get_news_detail_html` 的前置。"""
    full = {code[:6]: code for code in code_list}
    codes = list(full)
    if "begin_date" not in kwargs:
        try:
            if is_local:
                path = store.table_dir(local_path, *NEWS_LIST_DIR)
                for code in codes:
                    self.news_list[code] = store.read_table(path, code)
            else:
                self.news_list = _download_news_list(local_path, codes, **kwargs)
        except FileNotFoundError:
            self.news_list = _download_news_list(local_path, codes)
    else:
        self.news_list = _download_news_list(local_path, codes, **kwargs)
    self.news_list = {full[code]: self.news_list[code] for code in self.news_list}
    return self.news_list


#: 新闻详情页的模板，逐字取自字节码常量（`download_news_detail` 的
#: `news_html_template`）。三处占位：`__TITLE__` / `__META_ITEMS__` / `__BODY__`。
_NEWS_HTML = (
    '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<title>__TITLE__</title>\n<style>\n'
    '  body { margin: 0; padding: 0; background: #f5f7fa; color: #2c3e50;\n'
    '         font-family: "PingFang SC", "Microsoft YaHei", sans-serif; }\n'
    '  .page { max-width: 760px; margin: 24px auto; padding: 0 16px; }\n'
    '  .meta-card { background: #ffffff; border-radius: 12px; padding: 24px 28px;\n'
    '                box-shadow: 0 1px 4px rgba(0,0,0,0.06); }\n'
    '  .meta-card h1 { margin: 0 0 12px; font-size: 22px; line-height: 1.5; color: #1a2b3c; }\n'
    '  .meta-list { list-style: none; margin: 0; padding: 0; font-size: 14px; }\n'
    '  .meta-list li { margin: 4px 0; color: #5a6b7b; }\n'
    '  .meta-list b { color: #34495e; font-weight: 600; }\n'
    '  .content-card { background: #ffffff; border-radius: 12px; padding: 28px;\n'
    '                   margin-top: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.06);\n'
    '                   font-size: 16px; line-height: 1.8; word-break: break-word; }\n'
    '  .content-card img { max-width: 100%; height: auto; }\n'
    '</style>\n</head>\n<body>\n<div class="page">\n  <div class="meta-card">\n'
    '    <h1>__TITLE__</h1>\n    <ul class="meta-list">__META_ITEMS__</ul>\n'
    '  </div>\n  <div class="content-card">\n__BODY__\n  </div>\n</div>\n'
    '</body>\n</html>\n')

#: `ranges` 列是数字枚举，模板里换成中文（字节码常量 `news_ranges_map`）。
_NEWS_RANGES = {0: "无", 1: "按全市场", 2: "按行业", 3: "按个股"}

#: 正文页上的元信息行，顺序与标签取自字节码常量（`meta_items` 那行拼接）。
_HTML_META = (("摘要", "summary"), ("来源", "source"), ("作者", "author"),
              ("发布时间", "publish_time"))


def _html_meta_line(label: str, value) -> str:
    if _is_blank(value):
        return ""
    return f"<li><b>{label}：</b>{str(value).strip()}</li>"


def _html_body(content) -> str:
    """只取 `<body>` 与 `</body>` 之间的正文；没有 `<body>` 就整段当正文。"""
    text = str(content)
    lower = text.lower()
    start = lower.find("<body")
    if start == -1:
        return text
    start = lower.find(">", start) + 1
    end = lower.find("</body>", start)
    return text[start:end] if end != -1 else text[start:]


def _ranges_text(row) -> str:
    """`ranges` 是数字枚举。转不成 int 就原样转字符串（厂商 except 分支）。"""
    try:
        return _NEWS_RANGES.get(int(row.get("ranges")), str(row.get("ranges")))
    except (TypeError, ValueError):
        return str(row.get("ranges"))


def _write_news_html(news_list: dict, local_path: str) -> str:
    """把每条新闻的正文写成 `<news_id>.html`，返回目录。已下过的 id 不发请求。"""
    html_dir = store.table_dir(local_path, *NEWS_HTML_DIR)
    os.makedirs(html_dir, exist_ok=True)
    existing = os.listdir(html_dir)
    local_ids = [os.path.splitext(name)[0] for name in existing
                 if os.path.isfile(os.path.join(html_dir, name))]
    for frame in news_list.values():
        ids = [news_id for news_id in frame["news_id"] if news_id not in local_ids]
        fetched = gw.fetch_each(ids, lambda news_id: (
            ("function_id", NEWS_DETAIL_FUNCTION_ID), ("news_id", news_id)), NEWS_ID_BATCH)
        for news_id in ids:
            detail = fetched[news_id]
            if detail.empty:
                continue
            row = detail.iloc[0]
            content = row.get("content")
            if _is_blank(content):
                continue
            file_name = f"{news_id}.html"
            if file_name in existing:
                continue
            meta = "".join([_html_meta_line(label, row.get(key)) for label, key in _HTML_META]
                           + [_html_meta_line("新闻范围", _ranges_text(row))])
            page = (_NEWS_HTML.replace("__TITLE__", str(row.get("title", news_id)))
                    .replace("__META_ITEMS__", meta)
                    .replace("__BODY__", _html_body(content)))
            with open(os.path.join(html_dir, file_name), "w", encoding="utf-8") as f:
                f.write(page)
    print("新闻详情数据 下载完成")
    return html_dir


def get_news_detail_html(self, news_list, local_path=DEFAULT_LOCAL_PATH):
    """新闻详情正文（B010010003）。入参是 `get_news_list` 的返回值。"""
    self.news_detail_html_dir = _write_news_html(news_list, local_path)
    return self.news_detail_html_dir


METHODS = {
    "get_report_abstract": get_report_abstract,
    "save_report_abstract_md": save_report_abstract_md,
    "get_news_list": get_news_list,
    "get_news_detail_html": get_news_detail_html,
}
