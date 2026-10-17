"""可转债三条方法的修正（V01、V02）——正向断言修正后的行为。

两条缺陷都是厂商 1.1.15 自己的：

* **V01** `get_kzz_put_call_item` / `get_kzz_put_explanation` /
  `get_kzz_call_explanation` 的读本地分支取 `LocalDataFolder` 上不存在的成员
  （`Kzz_Put_call_item` 等，枚举里的实名是 `Kzz_Put_Call_Item`），
  `is_local=True` 必然 `AttributeError`，一个请求也发不出去。自研包改成与同族
  其余九个方法一致：读各方法自己的缓存文件，读不到回落下载。
* **V02** `get_kzz_put_explanation` 下载后把结果写进兄弟表
  `kzz_put_call_item.h5`，覆盖掉回售赎回条款那张表，读的一侧却按
  `kzz_put_explanation.h5` 找（厂商从来不写那个文件）。自研包写自己的文件。

读本地这条路要按输出列清单取列，所以夹具里的缓存表列要齐。列清单从一次真实
下载的返回值取，避免在这里抄一遍列名。

本文件随公开仓发布，不 import 厂商发行物，不读接口规范。
"""

from __future__ import annotations

import pandas as pd
import pytest
from open_harness import open_sdk, request_params

CODE = "113050.SH"
OTHER_CODE = "113051.SH"

#: 方法名 → (function_id, 缓存文件名, 网关回包里的厂商原列名, 输出列名)
METHODS = {
    "get_kzz_put_call_item": ("A010400005", "kzz_put_call_item", "MAND_PUT_PERIOD", "MAND_PUT_PERIOD"),
    "get_kzz_put_explanation": ("A010400010", "kzz_put_explanation", "REPURCHASE_DATE", "PUT_FUND_ARRIVAL_DATE"),
    "get_kzz_call_explanation": ("A010400011", "kzz_call_explanation", "REDEMPTION_DATE", "CALL_DATE"),
}


def _reply():
    """回包只给 MARKET_CODE 与该表的标识列，其余列由实现补 NaN。"""
    def reply(request):
        params = request_params(request)
        for function_id, _name, raw_col, _out_col in METHODS.values():
            if params.get("function_id") == function_id:
                return [{"MARKET_CODE": params["market_code"], raw_col: "20250101"}], 0
        return None
    return reply


def _seed(local_root, name: str, frame: pd.DataFrame) -> None:
    directory = local_root / "infodata" / "kzz"
    directory.mkdir(parents=True, exist_ok=True)
    frame.to_hdf(str(directory) + f"/{name}.h5", key=name, mode="w", complevel=0)


def _download_all(ad, root: str, codes: list[str]) -> dict[str, pd.DataFrame]:
    """三个方法各下一次载，返回各自的结果。"""
    return {method: getattr(ad.InfoData(), method)(codes, root, False)
            for method in METHODS}


# --------------------------------------------------------------------------
# V01：读本地读自己的表，读不到回落下载
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", list(METHODS))
def test_V01_本地无缓存时回落下载(tmp_path, method):
    function_id, name, _raw_col, out_col = METHODS[method]
    root = str(tmp_path) + "/"
    with open_sdk(_reply()) as (ad, gateway):
        frame = getattr(ad.InfoData(), method)([CODE], root, True)
    assert list(frame["MARKET_CODE"]) == [CODE]
    assert frame[out_col].tolist() == ["20250101"]
    assert not gateway.unfixtured
    assert [r["function_id"] for r in gateway.requests] == [function_id]
    assert (tmp_path / "infodata" / "kzz" / f"{name}.h5").is_file()


def test_V01_读本地读各自的表且不发请求(tmp_path):
    """三张表都在，取值各不相同：读错文件就会取到别的值。"""
    root = str(tmp_path) + "/"
    with open_sdk(_reply()) as (ad, gateway):
        downloaded = _download_all(ad, root, [CODE])
    for method, (_fid, name, _raw_col, out_col) in METHODS.items():
        marker = downloaded[method].copy()
        marker[out_col] = method
        _seed(tmp_path, name, marker)
    with open_sdk(_reply()) as (ad, gateway):
        for method, (_fid, _name, _raw_col, out_col) in METHODS.items():
            frame = getattr(ad.InfoData(), method)([CODE], root, True)
            assert frame[out_col].tolist() == [method]
    assert gateway.requests == []


def test_V01_读本地按传入代码过滤(tmp_path):
    root = str(tmp_path) + "/"
    with open_sdk(_reply()) as (ad, _gateway):
        downloaded = _download_all(ad, root, [CODE, OTHER_CODE])
    for method, (_fid, name, _raw_col, _out_col) in METHODS.items():
        _seed(tmp_path, name, downloaded[method])
    with open_sdk(_reply()) as (ad, gateway):
        for method in METHODS:
            frame = getattr(ad.InfoData(), method)([CODE], root, True)
            assert list(frame["MARKET_CODE"]) == [CODE]
    assert gateway.requests == []


# --------------------------------------------------------------------------
# V02：回售执行说明写自己的表，不动兄弟表
# --------------------------------------------------------------------------


def test_V02_回售执行说明写进自己的文件(tmp_path):
    root = str(tmp_path) + "/"
    with open_sdk(_reply()) as (ad, gateway):
        ad.InfoData().get_kzz_put_explanation([CODE], root, False)
    assert (tmp_path / "infodata" / "kzz" / "kzz_put_explanation.h5").is_file()
    assert not (tmp_path / "infodata" / "kzz" / "kzz_put_call_item.h5").exists()
    assert not gateway.unfixtured


def test_V02_回售执行说明不覆盖回售赎回条款那张表(tmp_path):
    root = str(tmp_path) + "/"
    with open_sdk(_reply()) as (ad, _gateway):
        put_call_item = ad.InfoData().get_kzz_put_call_item([CODE], root, False)
        ad.InfoData().get_kzz_put_explanation([CODE], root, False)
    stored = pd.read_hdf(str(tmp_path) + "/infodata/kzz/kzz_put_call_item.h5")
    assert list(stored.columns) == list(put_call_item.columns)
    assert "PUT_FUND_ARRIVAL_DATE" not in stored.columns
    assert stored["MAND_PUT_PERIOD"].tolist() == ["20250101"]


def test_V02_下载后读本地拿回同一份数据(tmp_path):
    """写读两侧指着同一个文件：下载完再读本地，行、列、值都一致。"""
    root = str(tmp_path) + "/"
    with open_sdk(_reply()) as (ad, gateway):
        first = ad.InfoData().get_kzz_put_explanation([CODE], root, False)
        second = ad.InfoData().get_kzz_put_explanation([CODE], root, True)
    pd.testing.assert_frame_equal(second, first)
    assert len(gateway.requests) == 1, "读本地不该再发请求"
