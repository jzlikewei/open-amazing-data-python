"""自研包单元测试的夹具本身能用：登录、发一条资讯请求、拿回 DataFrame。"""

from __future__ import annotations

from open_harness import open_sdk, request_params


def test_自研包经假网关取回一张表(tmp_path):
    def reply(request):
        params = request_params(request)
        if params.get("function_id") == "A010010001":
            return [{"MARKET_CODE": params["market_code"], "SECURITY_NAME": "茅台"}], 0
        return None

    with open_sdk(reply) as (ad, gateway):
        frame = ad.InfoData().get_stock_basic(["600519.SH"])
    assert list(frame["MARKET_CODE"]) == ["600519.SH"]
    assert frame["SECURITY_NAME"].iloc[0] == "茅台"
    assert len(gateway.requests) == 1 and not gateway.unfixtured
