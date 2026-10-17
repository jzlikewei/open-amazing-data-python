"""自研包对厂商缺陷的修正：V03 公告 PDF 空表分支、V08 研报 md 不改调用方的表。

这两条在厂商 1.1.15 上是缺陷，自研包改成修正后的行为，公开文档的
条目在 `docs/vendor-differences.md`。此处只断言修正后的行为。

夹具全部内联：不依赖厂商发行物，不依赖 `capability_spec`。
"""

from __future__ import annotations

import os

import pandas as pd

from open_harness import open_sdk, request_params

#: 公告清单的 9 列，字段与形状取自字节码的 columns_list。
_LIST_COLUMNS = ("MARKET_CODE", "SOURCE_ID", "TITLE", "TAG_ID", "TAG_NAME",
                 "ONLINE_STATUS", "PUBLISH_TIME", "DATA_STATUS", "COMPANY")


def _announcement_reply(request):
    """A020010001 按代码回一条公告，A020010004/5/6 回一条下载结果。"""
    params = request_params(request)
    fid = params.get("function_id")
    if fid == "A020010001":
        code = params["market_code"]
        return [{"MARKET_CODE": code, "SOURCE_ID": f"{code}-20250331", "TITLE": "年报",
                 "TAG_ID": "1|2", "TAG_NAME": "定期报告|公告", "ONLINE_STATUS": "1",
                 "PUBLISH_TIME": "2025-03-31 09:30:00", "DATA_STATUS": "1",
                 "COMPANY": "测试公司"}], 0
    if fid in ("A020010004", "A020010005", "A020010006"):
        return [{"source_id": params["source_id"], "status": 0}], 0
    return None


def test_空清单表照样返回股票自己的tag表():
    """V03：厂商这一支返回不存在的 announcement_bond_stock_tag_df，必然崩。"""
    with open_sdk(_announcement_reply) as (ad, gateway):
        info = ad.InfoData()
        paths, picked = info.get_announcement_stock(pd.DataFrame())
    assert picked is info.announcement_stock_list_tag_df
    assert isinstance(picked, pd.DataFrame) and picked.empty
    assert paths is info.announcement_stock_pdf_path and paths == {}
    assert not gateway.requests


def test_空清单表三支各回各的tag表():
    """V03：fund / bond 厂商没这个毛病，修正后行为不变，各回各的属性。"""
    with open_sdk(_announcement_reply) as (ad, _gateway):
        info = ad.InfoData()
        for kind in ("stock", "fund", "bond"):
            paths, picked = getattr(info, f"get_announcement_{kind}")(pd.DataFrame())
            tag_attr = getattr(info, f"announcement_{kind}_list_tag_df")
            pdf_attr = getattr(info, f"announcement_{kind}_pdf_path")
            assert picked is tag_attr, kind
            assert picked.empty and paths is pdf_attr, kind


def test_先查过PDF再传空表回的是已筛出的那张表(tmp_path):
    """V03：属性被填上之后再传空表，返回的就是那张表，不新建、不崩。"""
    with open_sdk(_announcement_reply) as (ad, _gateway):
        info = ad.InfoData()
        listing = info.get_announcement_stock_list(
            ["600519.SH"], str(tmp_path) + "/", False,
            begin_date=20250101, end_date=20251231)
        assert list(listing.columns) == list(_LIST_COLUMNS) and len(listing) == 1
        _, picked = info.get_announcement_stock(listing)
        assert list(picked.index) == ["600519.SH-20250331"]
        before = info.announcement_stock_list_tag_df
        paths, empty = info.get_announcement_stock(pd.DataFrame())
    assert empty is before is info.announcement_stock_list_tag_df
    assert len(empty) == 1
    assert "600519.SH-20250331" in paths


def test_导md不往调用方的表上加列(tmp_path):
    """V08：厂商浅拷贝字典，file_name 这一列留在调用方传进来的表上。"""
    frame = pd.DataFrame({
        "id": [1001.0, 1002.0],
        "title": ["研报一", "研报二"],
        "des": [float("nan"), "正文"],
        "author": ["张三", "李四"],
        "source": ["券商", None],
        "bcpj": ["买入", "增持"],
        "symbol": ["600519", "000858"],
        "industry": ["食品", "食品"],
        "classify": ["公司", "公司"],
    })
    data = {"600519.SH": frame}
    columns_before = list(frame.columns)
    rows_before = frame.to_dict("records")

    with open_sdk(lambda request: None) as (ad, gateway):
        info = ad.InfoData()
        md_dir = info.save_report_abstract_md(data, str(tmp_path) + "/")

    assert list(frame.columns) == columns_before
    assert frame.to_dict("records") == rows_before
    assert "file_name" not in frame.columns
    assert list(data) == ["600519.SH"]
    assert not gateway.requests
    # 落盘与返回值照旧：每个 id 一篇 md
    assert sorted(os.listdir(md_dir)) == ["1001.0.md", "1002.0.md"]
    assert md_dir == str(tmp_path) + "/infodata/report_abstract/report_abstract_md"
    body = open(os.path.join(md_dir, "1002.0.md"), encoding="utf-8").read()
    assert body.startswith("# 研报二")
    assert "- **作者**：李四" in body and "- **来源**" not in body
    assert body.endswith("正文\n")
    # des 是 NaN 的那篇没有正文段，源为空的那行不出
    blank = open(os.path.join(md_dir, "1001.0.md"), encoding="utf-8").read()
    assert blank == ("# 研报一\n\n- **作者**：张三\n- **来源**：券商\n- **行业**：食品\n"
                     "- **标的**：600519\n- **评级**：买入\n- **分类**：公司\n")


def test_重复导md不重写也不动调用方的表(tmp_path):
    """V08：落过的行不重写，调用方的表两次都还是入参那一张。"""
    frame = pd.DataFrame({"id": [1001.0], "title": ["研报一"], "author": ["张三"]})
    data = {"600519.SH": frame}
    with open_sdk(lambda request: None) as (ad, _gateway):
        info = ad.InfoData()
        first = info.save_report_abstract_md(data, str(tmp_path) + "/")
        again = info.save_report_abstract_md(data, str(tmp_path) + "/")
    assert first == again
    assert sorted(os.listdir(first)) == ["1001.0.md"]
    assert list(frame.columns) == ["id", "title", "author"]
