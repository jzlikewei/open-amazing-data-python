"""U4：资讯族按 offset 翻页取全；服务端忽略 offset 时明确失败。

请求里的 `count` 缺省 1000（原生库缺省），服务端单次最多回 `count` 行，「还有
下一页」只由「本页行数 == count」表达（2026-10-11 网关调研 R1，探针实测）。
厂商原生库只发一页，满 1000 行的结果被静默截断。翻页做在替身层
（`tgwpy.compat.tgw_module._Pager`），业务层看到的仍是一次请求、一次回调。

本文件随公开仓发布：只用 `open_harness` 的内联假网关。
"""

from __future__ import annotations

import pytest
from tgwpy.status import DataEmpty
from open_harness import open_sdk, request_params

FUND_SHARE_FID = "A010090002"
CODE = "510300.SH"


def _rows(n: int) -> list[dict]:
    out = []
    for i in range(n):
        day = f"2{i // 372:03d}{(i // 31) % 12 + 1:02d}{i % 31 + 1:02d}"
        out.append({"FUND_SHARE": float(i), "CHANGE_REASON": "申购", "MERGEDSHARES_OR_NOT": 1,
                    "MARKET_CODE": CODE, "ANN_DATE": day, "UNIT_TOTAL": float(i),
                    "CHANGE_DATE": day, "FLOW_SHARE": float(i)})
    return out


class _Paged:
    """单次最多回 `count` 行（请求里的 count），按 offset 切片。"""

    def __init__(self, total: int, ignore_offset: bool = False) -> None:
        self.data = _rows(total)
        self.ignore_offset = ignore_offset
        self.pages: list[tuple[int, int]] = []

    def __call__(self, request: dict):
        if request_params(request).get("function_id") != FUND_SHARE_FID:
            return None
        offset = 0 if self.ignore_offset else int(request.get("offset", 0))
        count = int(request.get("count", 1000))
        self.pages.append((int(request.get("offset", 0)), count))
        page = self.data[offset:offset + count]
        return (page, 0) if page else (None, DataEmpty())


@pytest.mark.parametrize("total", [999, 1000, 2500])
def test_U4_超过单页上限时翻页取全且不重复(tmp_path, total):
    server = _Paged(total)
    with open_sdk(server) as (ad, _gw):
        out = ad.InfoData().get_fund_share([CODE], str(tmp_path) + "/", False)
    frame = out[CODE]
    assert len(frame) == total
    assert not frame.duplicated().any()
    expected_pages = total // 1000 + 1
    assert [o for o, _c in server.pages] == [1000 * i for i in range(expected_pages)]
    assert all(isinstance(o, int) for o, _c in server.pages)


def test_U4_服务端忽略offset时明确失败(tmp_path):
    server = _Paged(1500, ignore_offset=True)
    with open_sdk(server) as (ad, _gw):
        with pytest.raises(Exception) as err:
            ad.InfoData().get_fund_share([CODE], str(tmp_path) + "/", False)
    assert "查询失败" in str(err.value), "业务层按失败重试，最后抛查询失败，不交截断结果"
    assert len(server.pages) >= 2


def test_U4_翻页的offset写成整数():
    from tgwpy.frames.query import build_third_info_request

    env = build_third_info_request(1, [("function_id", "A"), ("offset", "1000"),
                                       ("count", "1000"), ("market_code", CODE)])
    assert env["offset"] == 1000 and env["count"] == 1000
    assert {"key": "market_code", "value": CODE} in env["item"]
