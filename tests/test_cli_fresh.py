"""U1：CLI 默认每次向服务端现取，不读本地缓存；`--use-cache` 才沿用厂商的缓存语义。

自研包的承诺是与厂商 1.1.15 一致，包括 `is_local=True` 时先读缓存——那是业务层
的语义，不动。CLI 是交给下游（每日同步写库）的那一层，结果要么是服务端当前的
完整数据，要么明确失败。所以 CLI 每次调用给业务层一个空的临时数据根并传
`is_local=False`（`cli._call_root` / `cli._cache_kwargs`）。

本文件随公开仓发布：只用 `open_harness` 的内联假网关，不读接口规范、不碰发行物。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pytest
from open_harness import open_sdk, request_params

from tgwpy import cli

FUND_SHARE_FID = "A010090002"
INDEX_MEMBERS_FID = "A010200002"
INDUSTRY_MEMBERS_FID = "A010200003"


def _fund_share_row(day: str, share: float) -> dict:
    return {"FUND_SHARE": share, "CHANGE_REASON": "申购", "MERGEDSHARES_OR_NOT": 1,
            "MARKET_CODE": "510300.SH", "ANN_DATE": day, "UNIT_TOTAL": share,
            "CHANGE_DATE": day, "FLOW_SHARE": share}


def _members_rows(index_code: str) -> list[dict]:
    return [{"INDEX_CODE": index_code, "CON_CODE": con, "CON_INDATE": "20200101",
             "CON_OUTDATE": None, "INDEXNAME": "测试指数"}
            for con in ("600519.SH", "000001.SZ")]


def _reply(request: dict):
    params = request_params(request)
    fid = params.get("function_id")
    if fid == FUND_SHARE_FID:
        return [_fund_share_row("20251215", 999.0)], 0      # 服务端的「新」数据
    if fid in (INDEX_MEMBERS_FID, INDUSTRY_MEMBERS_FID):
        return _members_rows(params["index_code"]), 0
    return None


def _opts(argv: list[str]) -> tuple[cli.MethodSpec, argparse.Namespace]:
    return cli.run(argv)


def _fids(gateway) -> list[str]:
    return [request_params(r).get("function_id") for r in gateway.requests]


def test_U1_有旧缓存时CLI仍向网关现取并返回新数据(tmp_path: Path, monkeypatch):
    stale_root = str(tmp_path / "user-cache") + "/"
    with open_sdk(_reply) as (ad, gateway):
        # 先造一份旧缓存，并证明业务层的厂商语义确实会直接返回它、不发请求
        ad.InfoData().get_fund_share(["510300.SH"], stale_root, False)
        stale_file = Path(stale_root, "infodata", "fund_share", "510300.SH.h5")
        assert stale_file.is_file()
        pd.DataFrame([{**_fund_share_row("20200101", 1.0)}]).rename(columns={
            "MERGEDSHARES_OR_NOT": "IS_CONSOLIDATED_DATA", "UNIT_TOTAL": "TOTAL_SHARE",
            "FLOW_SHARE": "FLOAT_SHARE"}).to_hdf(stale_file, key="510300.SH", mode="w")
        gateway.requests.clear()
        cached = ad.InfoData().get_fund_share(["510300.SH"], stale_root, True)
        assert list(cached["510300.SH"]["FUND_SHARE"]) == [1.0]
        assert gateway.requests == [], "厂商语义：有缓存就一个请求都不发"

        spec, opts = _opts(["other", "fund_share", "--codes", "510300.SH"])
        seen: list[dict] = []
        real = cli._cache_kwargs
        monkeypatch.setattr(cli, "_cache_kwargs",
                            lambda fn, root: seen.append(real(fn, root)) or seen[-1])
        result = cli.dispatch(ad, spec, opts)

    assert FUND_SHARE_FID in _fids(gateway), "CLI 必须向网关发请求"
    assert list(result["510300.SH"]["FUND_SHARE"]) == [999.0], "返回的是网关的新数据"
    (kwargs,) = seen
    assert kwargs["is_local"] is False
    from OpenAmazingData._local import DEFAULT_LOCAL_PATH

    assert kwargs["local_path"] not in (stale_root, DEFAULT_LOCAL_PATH)
    assert not Path(kwargs["local_path"]).exists(), "临时数据根用完即删"


@pytest.mark.parametrize("argv,fid,code", [
    (["index", "constituent", "--codes", "000300.SH"], INDEX_MEMBERS_FID, "000300.SH"),
    (["index", "industry_constituent", "--codes", "801120.SI"], INDUSTRY_MEMBERS_FID,
     "801120.SI"),
])
def test_U1_没有缓存时成分类方法发请求并返回数据(argv, fid, code):
    with open_sdk(_reply) as (ad, gateway):
        spec, opts = _opts(argv)
        result = cli.dispatch(ad, spec, opts)
    assert fid in _fids(gateway)
    assert sorted(result[code]["CON_CODE"]) == ["000001.SZ", "600519.SH"]


def test_U1_use_cache沿用业务层缺省():
    spec, opts = _opts(["other", "fund_share", "--codes", "510300.SH", "--use-cache"])
    assert opts.use_cache is True
    assert cli._cache_kwargs(lambda code_list, local_path="x", is_local=True: None, None) == {}


def test_U1_use_cache下成分类缺缓存的代码现取():
    """厂商 `Members.query` 在 `is_local=True` 时只读本地，缺的代码直接跳过。"""
    calls: list[tuple] = []

    class _Info:
        def get_index_constituent(self, code_list, local_path="d", is_local=True):
            calls.append((list(code_list), is_local))
            return {} if is_local else {c: pd.DataFrame({"CON_CODE": ["600519.SH"]})
                                        for c in code_list}

    class _AD:
        def InfoData(self):
            return _Info()

    spec, opts = _opts(["index", "constituent", "--codes", "000300.SH", "--use-cache"])
    result = cli.dispatch(_AD(), spec, opts)
    assert calls == [(["000300.SH"], True), (["000300.SH"], False)]
    assert list(result) == ["000300.SH"]


def test_U1_缺省时复权因子也从空数据根现取():
    calls: list[dict] = []

    class _Base:
        def get_backward_factor(self, code_list, local_path="d", is_local=True):
            calls.append({"local_path": local_path, "is_local": is_local})
            return {c: pd.Series([1.0, 2.0], index=pd.to_datetime(["2025-12-01", "2025-12-02"]))
                    for c in code_list}

    class _AD:
        def BaseData(self):
            return _Base()

    kline = {"600519.SH": pd.DataFrame({"code": ["600519.SH"],
                                         "kline_time": pd.to_datetime(["2025-12-02"]),
                                         "open": [1.0], "high": [1.0], "low": [1.0],
                                         "close": [1.0], "volume": [1], "amount": [1.0]})}
    with cli._call_root(False) as root:
        out = cli.apply_adjust(_AD(), kline, "hfq", root=root)
    assert calls == [{"local_path": root, "is_local": True}], "空目录里 is_local=True 也只能现取"
    assert out["600519.SH"].iloc[0]["close"] == 2.0
