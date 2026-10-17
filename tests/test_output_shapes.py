"""输出形状与复权的验收测试。

两件事在这里钉死：

1. **口径是 SKILL.md，不是参照实现。** 参照对 16 个没有代码维度的方法吐裸数组
   `[records...]`（参照实现的输出实测），我们分组成
   `{code: [records...]}`。fixture 抄的是那份实测输出的字段清单与样例值，
   断言「拆开再拼回去 == 参照的裸数组」——分组只许改容器，不许改内容。
2. **复权的价格要真的对。** 1410.89 × (8.446992 / 8.646881) = 1378.274611，
   是参照 CLI stdout 里的 close 逐位对出来的值（reference-output-shapes.md 1.5）。

分组列的逐方法依据在 `cli.CATEGORIES` 的 `group_column` 上，这里对账一遍，
免得后人改了某一列的归属却没改这里。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy import cli  # noqa: E402

CLI = ROOT / "src" / "tgwpy" / "cli.py"

pd = pytest.importorskip("pandas")


# --------------------------------------------------------------------------
# 16 个方法的分组键：逐方法显式写出来，不靠猜
# --------------------------------------------------------------------------

#: method -> (分组列, 参照实现的样例 record)
#: record 抄 reference-output-shapes.md 2.1 各方法的「record 字段」列，
#: 值用该节给出的真实样例（代码、期限、日期都是文档里出现过的）。
SHAPES: dict[str, tuple[str, list[dict]]] = {
    "stock_basic": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "SECURITY_NAME": "贵州茅台",
         "LISTDATE": 20010827, "IS_LISTED": 1},
        {"index": 1, "MARKET_CODE": "000858.SZ", "SECURITY_NAME": "五粮液",
         "LISTDATE": 19981127, "IS_LISTED": 1},
    ]),
    "bj_code_mapping": ("NEW_CODE", [
        # LISTING_DATE 在参照输出里是 JSON 数字，不是字符串（2.1 / 3.6）
        {"index": 0, "OLD_CODE": "400001", "NEW_CODE": "830799",
         "SECURITY_NAME": "艾融软件", "LISTING_DATE": 20201022},
        {"index": 1, "OLD_CODE": "400002", "NEW_CODE": "430139",
         "SECURITY_NAME": "华岭股份", "LISTING_DATE": 20140912},
    ]),
    "profit_express": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "REPORTING_PERIOD": "20260331",
         "ANN_DATE": "20260420", "TOTAL_ASSETS": 26800000000.0},
        {"index": 1, "MARKET_CODE": "000858.SZ", "REPORTING_PERIOD": "20260331",
         "ANN_DATE": "20260421", "TOTAL_ASSETS": 27100000000.0},
    ]),
    "profit_notice": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "REPORTING_PERIOD": "20251231",
         "ANN_DATE": "20260115", "P_CHANGE_MAX": 15.2},
        {"index": 1, "MARKET_CODE": "000858.SZ", "REPORTING_PERIOD": "20251231",
         "ANN_DATE": "20260116", "P_CHANGE_MAX": 8.4},
    ]),
    "top10": ("MARKET_CODE", [
        {"index": 0, "ANN_DATE": "20260331", "MARKET_CODE": "600519.SH",
         "HOLDER_ENDDATE": "20260331", "HOLDER_NAME": "香港中央结算有限公司",
         "HOLDER_PCT": 8.21},
        {"index": 1, "ANN_DATE": "20260331", "MARKET_CODE": "000858.SZ",
         "HOLDER_ENDDATE": "20260331", "HOLDER_NAME": "宜宾市国有资本经营有限公司",
         "HOLDER_PCT": 54.36},
    ]),
    "holder_num": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "ANN_DT": "20260420",
         "HOLDER_ENDDATE": "20260331", "HOLDER_NUM": 192134, "HOLDER_TOTAL_NUM": 1256196000},
        {"index": 1, "MARKET_CODE": "000858.SZ", "ANN_DT": "20260420",
         "HOLDER_ENDDATE": "20260331", "HOLDER_NUM": 154870, "HOLDER_TOTAL_NUM": 3882251800},
    ]),
    "equity_structure": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "ANN_DATE": "20260420",
         "CHANGE_DATE": "20260420", "CURRENT_SIGN": 1, "TOT_SHARE": 1256196000.0},
        {"index": 1, "MARKET_CODE": "000858.SZ", "ANN_DATE": "20260420",
         "CHANGE_DATE": "20260420", "CURRENT_SIGN": 1, "TOT_SHARE": 3882251800.0},
    ]),
    "dividend": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "DIV_PROGRESS": "实施",
         "DVD_PER_SHARE_STK": 30.0, "DATE_EX": "20260620"},
        {"index": 1, "MARKET_CODE": "000858.SZ", "DIV_PROGRESS": "实施",
         "DVD_PER_SHARE_STK": 12.0, "DATE_EX": "20260620"},
    ]),
    "right_issue": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "000002.SZ", "PROGRESS": "实施",
         "PRICE": 12.8, "RATIO": 3.0, "EX_DIVIDEND_DATE": "20260420"},
    ]),
    "industry_info": ("INDEX_CODE", [
        {"index": 0, "INDEX_CODE": "801030.SI", "INDUSTRY_CODE": "801031",
         "LEVEL_TYPE": 2, "LEVEL1_NAME": "有色金属", "LEVEL2_NAME": "稀土"},
        {"index": 1, "INDEX_CODE": "801040.SI", "INDUSTRY_CODE": "801041",
         "LEVEL_TYPE": 2, "LEVEL1_NAME": "钢铁", "LEVEL2_NAME": "普钢"},
    ]),
    "margin_summary": ("EXCHANGE", [
        {"index": 0, "TRADE_DATE": "20260424", "EXCHANGE": "SSE",
         "SUM_BORROW_MONEY_BALANCE": 1234567890.0},
        {"index": 1, "TRADE_DATE": "20260424", "EXCHANGE": "SZSE",
         "SUM_BORROW_MONEY_BALANCE": 987654321.0},
    ]),
    "long_hu_bang": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "600519.SH", "TRADE_DATE": "20260424",
         "SECURITY_NAME": "贵州茅台", "REASON_TYPE_NAME": "日涨幅偏离值达7%"},
        {"index": 1, "MARKET_CODE": "000858.SZ", "TRADE_DATE": "20260424",
         "SECURITY_NAME": "五粮液", "REASON_TYPE_NAME": "日跌幅偏离值达7%"},
    ]),
    "block_trading": ("MARKET_CODE", [
        {"index": 0, "TRADE_DATE": "20260424", "MARKET_CODE": "600519.SH",
         "SECURITY_NAME": "贵州茅台", "B_SHARE_PRICE": 1410.89, "B_SHARE_VOLUME": 10000.0},
        {"index": 1, "TRADE_DATE": "20260424", "MARKET_CODE": "000858.SZ",
         "SECURITY_NAME": "五粮液", "B_SHARE_PRICE": 120.5, "B_SHARE_VOLUME": 200000.0},
    ]),
    "option_basic_info": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "10011425.SH", "CONTRACT_FULL_NAME": "贵州茅台2026年7月500ETF期权",
         "CONTRACT_TYPE": 1, "EXPIRY_DATE": "20260722", "EXERCISE_PRICE": 1500.0},
    ]),
    "kzz_issuance": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "113008.SH", "STOCK_CODE": "600519.SH",
         "CRNCY_CODE": "CNY", "ANN_DT": "20260101", "LISTED_ANN_DATE": "20260102"},
    ]),
    "kzz_conv": ("MARKET_CODE", [
        {"index": 0, "MARKET_CODE": "113008.SH", "ANN_DATE": "20260102",
         "CONV_CODE": "600519.SH", "CONV_NAME": "贵州茅台", "CONV_PRICE": 168.0},
    ]),
}

SPEC_OF = {m: s for cat in cli.CATEGORIES.values() for m, s in cat.items()}


def _spec(method: str):
    return SPEC_OF[method]


def _flatten(payload: dict) -> list:
    """把 {code: [records]} 拆回裸数组，用来跟参照的输出逐条对账。"""
    return [record for bucket in payload.values() for record in bucket]


# --------------------------------------------------------------------------
# 一、16 个方法：分组列登记在案，形状与参照等价
# --------------------------------------------------------------------------

def test_正好16个方法需要分组():
    assert len(SHAPES) == 16
    assert set(SHAPES) == {m for m, s in SPEC_OF.items() if s.group_column}, \
        "SHAPES 与 CATEGORIES.group_column 必须一一对应"


@pytest.mark.parametrize("method", sorted(SHAPES))
def test_分组列与登记一致(method):
    assert SPEC_OF[method].group_column == SHAPES[method][0]


@pytest.mark.parametrize("method", sorted(SHAPES))
def test_不再多包一层方法名(method):
    """16 个方法都吐过 `{"<method>": [...]}`，现在必须没有那层壳。"""
    _, records = SHAPES[method]
    out = cli.normalize(_spec(method), pd.DataFrame(records))
    assert isinstance(out, dict)
    assert method not in out, "多包了一层方法名键"


@pytest.mark.parametrize("method", sorted(SHAPES))
def test_拆开拼回去等于参照的裸数组(method):
    """分组只改容器，不改内容：record 一条不少、一条不多、顺序不变。"""
    _, records = SHAPES[method]
    out = cli.normalize(_spec(method), pd.DataFrame(records))
    assert _flatten(out) == json.loads(pd.DataFrame(records).to_json(orient="records"))


@pytest.mark.parametrize("method", sorted(SHAPES))
def test_每个值都是record列表(method):
    _, records = SHAPES[method]
    out = cli.normalize(_spec(method), pd.DataFrame(records))
    assert out, f"{method} 分组后为空"
    for key, bucket in out.items():
        assert isinstance(key, str) and key
        assert isinstance(bucket, list) and bucket
        assert all(isinstance(r, dict) for r in bucket)


def test_按MARKET_CODE分组的13个():
    """13 个的键就该是 record 里的 MARKET_CODE，一个不多一个不少。"""
    by_market_code = [m for m, (col, _) in SHAPES.items() if col == "MARKET_CODE"]
    assert len(by_market_code) == 13
    for method in by_market_code:
        _, records = SHAPES[method]
        out = cli.normalize(_spec(method), pd.DataFrame(records))
        assert set(out) == {r["MARKET_CODE"] for r in records}


def test_三个自然键各按各的列分组():
    """bj_code_mapping / industry_info / margin_summary 的键不是代码。"""
    assert set(cli.normalize(_spec("bj_code_mapping"),
                          pd.DataFrame(SHAPES["bj_code_mapping"][1]))) == {"830799", "430139"}
    assert set(cli.normalize(_spec("industry_info"),
                          pd.DataFrame(SHAPES["industry_info"][1]))) == {"801030.SI", "801040.SI"}
    assert set(cli.normalize(_spec("margin_summary"),
                          pd.DataFrame(SHAPES["margin_summary"][1]))) == {"SSE", "SZSE"}


# --------------------------------------------------------------------------
# 二、分组键的形态：带不带后缀的规则
# --------------------------------------------------------------------------

def test_键保留交易所后缀():
    """MARKET_CODE 是什么就写什么，`600519.SH` 不裁成 `600519`。

    依据：参照实现在本来就返回 {code: [...]} 的 16 个方法里，键也是带后缀的
    （`600519.SH` / `000300.SH` / `801030.SI` / `510050.SH`），
    见 reference-output-shapes.md 2.1。裁了就跟它们对不上。
    """
    out = cli.normalize(_spec("holder_num"), pd.DataFrame(SHAPES["holder_num"][1]))
    assert list(out) == ["600519.SH", "000858.SZ"]


def test_裸代码不补后缀():
    """键是列值原样。没带后缀的列不会被我们猜一个交易所补上去。"""
    frame = pd.DataFrame([
        {"index": 0, "MARKET_CODE": "600519", "HOLDER_NUM": 192134},
        {"index": 1, "MARKET_CODE": "600519", "HOLDER_NUM": 190000},
        {"index": 2, "MARKET_CODE": "000858", "HOLDER_NUM": 154870},
    ])
    out = cli.normalize(_spec("holder_num"), frame)
    assert list(out) == ["600519", "000858"]
    assert len(out["600519"]) == 2


def test_已有后缀与裸代码是两个不同的桶():
    out = cli.normalize(_spec("holder_num"), pd.DataFrame([
        {"index": 0, "MARKET_CODE": "600519.SH", "HOLDER_NUM": 1},
        {"index": 1, "MARKET_CODE": "600519", "HOLDER_NUM": 2},
    ]))
    assert set(out) == {"600519.SH", "600519"}


def test_缺分组列的记录进方法名桶并告警(capsys):
    """自然键对不上时告警，不悄悄把 record 弄丢。stdout 那条链路另测。"""
    frame = pd.DataFrame([
        {"index": 0, "MARKET_CODE": "600519.SH", "HOLDER_NUM": 1},
        {"index": 1, "MARKET_CODE": None, "HOLDER_NUM": 2},
    ])
    out = cli.normalize(_spec("holder_num"), frame)
    # record 本身不动，只是换了桶——分组列留个 null，不替它编值
    assert out["holder_num"] == [{"index": 1, "MARKET_CODE": None, "HOLDER_NUM": 2}]
    assert "MARKET_CODE" in capsys.readouterr().err


def test_空表分成空对象():
    assert cli.normalize(_spec("holder_num"), pd.DataFrame(
        [{"index": 0, "MARKET_CODE": "600519.SH", "HOLDER_NUM": 1}])[0:0]) == {}


def test_日期列该是数字的仍是数字():
    """bj_code_mapping.LISTING_DATE 在参照 stdout 里是 JSON 数字（2.1 / 3.6）。"""
    out = cli.normalize(_spec("bj_code_mapping"),
                        pd.DataFrame(SHAPES["bj_code_mapping"][1]))
    assert out["830799"][0]["LISTING_DATE"] == 20201022
    assert isinstance(out["830799"][0]["LISTING_DATE"], int)


def test_索引不连续也原样保留():
    """index 不保证连续（3.7：index weight 首条是 297），分组不该动它。"""
    out = cli.normalize(_spec("block_trading"), pd.DataFrame([
        {"index": 297, "MARKET_CODE": "600519.SH", "TRADE_DATE": "20260424"},
        {"index": 272, "MARKET_CODE": "600519.SH", "TRADE_DATE": "20260423"},
    ]))
    assert [r["index"] for r in out["600519.SH"]] == [297, 272]


# --------------------------------------------------------------------------
# 三、snapshot：保持 {code: [records]}，与参照不同是有意的
# --------------------------------------------------------------------------

def test_snapshot_拍平成code到records():
    spec, _ = cli.run(["market", "snapshot", "--codes", "600519.SH",
                       "--begin", "20260423", "--end", "20260424"])
    result = {
        20260423: {"600519.SH": pd.DataFrame([{"last_price": 1700.0, "code": "600519.SH"}])},
        20260424: {"600519.SH": pd.DataFrame([{"last_price": 1702.5, "code": "600519.SH"}])},
    }
    assert cli.normalize(spec, result) == {
        "600519.SH": [{"last_price": 1700.0, "code": "600519.SH"},
                      {"last_price": 1702.5, "code": "600519.SH"}],
    }


def test_snapshot_多只代码各自成桶():
    spec, _ = cli.run(["market", "snapshot", "--codes", "600519.SH,000858.SZ",
                       "--begin", "20260424", "--end", "20260424"])
    result = {20260424: {
        "600519.SH": pd.DataFrame([{"last_price": 1700.0}]),
        "000858.SZ": pd.DataFrame([{"last_price": 120.5}]),
    }}
    out = cli.normalize(spec, result)
    assert set(out) == {"600519.SH", "000858.SZ"}


def test_snapshot_不再拆2元组():
    """query_snapshot 只有一个出口（RETURN_VALUE @ 1946，全函数零个 BUILD_TUPLE），
    曾经那个 2-tuple 分支没有任何证据支撑，删掉了。这里钉住，免得又加回来。"""
    spec, _ = cli.run(["market", "snapshot", "--codes", "600519.SH",
                       "--begin", "20260424", "--end", "20260424"])
    with pytest.raises(AttributeError):
        cli.normalize(spec, ([], {20260424: {"600519.SH": pd.DataFrame([{"x": 1.0}])}}))


# --------------------------------------------------------------------------
# 四、apply_adjust：四处缺陷
# --------------------------------------------------------------------------

#: 复权因子的真实阶梯序列。值抄 reference-output-shapes.md 1.4 的逐点对账表。
FACTOR_DATES = ["2001-08-27", "2002-07-25", "2003-07-14", "2026-04-20", "2026-06-26"]
FACTOR_VALUES = [1.0, 1.11828, 1.239853, 8.446992, 8.646881]


def _factor_table(codes=("600519.SH",)):
    """宽表 {code: Series}，索引 = 交易日，列名 = 带后缀的代码。"""
    index = pd.to_datetime(FACTOR_DATES)
    return {code: pd.Series(FACTOR_VALUES, index=index) for code in codes}


def _kline(close=1410.89, times=None, code="600519.SH"):
    """参照输出的字段全小写（2.1 market 段），时间列叫 kline_time。"""
    times = times or ["2026-04-20"]
    return pd.DataFrame({
        "index": range(len(times)),
        "code": [code] * len(times),
        "kline_time": pd.to_datetime(times),
        "open": [close - 5.0] * len(times),
        "high": [close + 8.0] * len(times),
        "low": [close - 9.0] * len(times),
        "close": [close] * len(times),
        "volume": [12345.0] * len(times),
        "amount": [12345.0 * close] * len(times),
    })


class _FakeBase:
    def __init__(self, factors):
        self._factors = factors

    def get_backward_factor(self, codes):
        return self._factors


class _FakeLocalTable:
    """模拟自研包的整表快照语义。

    `is_local=True` 读本地整表，末尾按 `code_list` 取列——缺一只抛
    `KeyError`（厂商只打印一句就原样返回）；`is_local=False` 是补下载
    那一路，把整表返回去。
    """

    def __init__(self, local, fetched):
        self.local, self.fetched = local, fetched
        self.calls = []

    def get_backward_factor(self, code_list, local_path=None, is_local=True):
        self.calls.append((list(code_list), is_local))
        table = self.local if is_local else self.fetched
        if not is_local:
            return dict(table)
        return {c: table[c] for c in code_list}      # 缺一只就 KeyError


class _FakeAD:
    def __init__(self, factors, base=None):
        self._base = base if base is not None else _FakeBase(factors)

    def BaseData(self):
        return self._base


# 缺陷 1：只乘 close，open/high/low 还是原价
def test_四个价格列都复权():
    out = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": _kline()}, "hfq")
    row = out["600519.SH"].iloc[0]
    assert row["close"] == round(1410.89 * 8.446992, 6)
    assert row["open"] == round(1405.89 * 8.446992, 6)
    assert row["high"] == round(1418.89 * 8.446992, 6)
    assert row["low"] == round(1401.89 * 8.446992, 6)
    assert row["volume"] == 12345.0, "量额不参与复权"


# 缺陷 2：qfq 少除末值，价格是错的
def test_前复权除末值且价格对得上():
    """1410.89 × (8.446992 / 8.646881) = 1378.274611，与参照 CLI 的 stdout 逐位相等。"""
    out = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": _kline()}, "qfq")
    assert out["600519.SH"].iloc[0]["close"] == 1378.274611


def test_后复权不除末值():
    """hfq 直接乘累积因子本身，不做末值归一，所以量级差一截。"""
    hfq = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": _kline()},
                           "hfq")["600519.SH"].iloc[0]["close"]
    qfq = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": _kline()},
                           "qfq")["600519.SH"].iloc[0]["close"]
    assert hfq == round(1410.89 * 8.446992, 6)
    assert qfq == 1378.274611
    assert hfq > qfq


def test_前复权在末值那天等于原价():
    """末值的因子除以自身就是 1.0，末根 K 线的 qfq 价必须等于原始价。"""
    times = ["2026-04-20", "2026-06-26"]
    frame = _kline(times=times)
    frame["close"] = [1410.89, 1500.0]
    rows = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": frame},
                            "qfq")["600519.SH"]
    assert rows.iloc[0]["close"] == 1378.274611
    assert rows.iloc[1]["close"] == 1500.0, "末值那天的 qfq 价就是原价"


# 缺陷 3：缺因子就抛错，整条命令炸掉
def test_因子表里没有这只代码时跳过并告警(capsys):
    """以前是 RuntimeFailure 抛出去（退出码 3，整条命令没输出），现在跳过 + 告警。"""
    factors = _factor_table(codes=("000858.SZ",))
    out = cli.apply_adjust(_FakeAD(factors), {"600519.SH": _kline()}, "qfq")
    assert out == {}, "匹配不上的代码整只跳过"
    assert "600519.SH" in capsys.readouterr().err


def test_部分K线缺因子时原样保留(capsys):
    times = ["2026-04-20", "2026-04-21"]        # 4/21 不在因子表的交易日里
    out = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": _kline(times=times)},
                           "qfq")
    rows = out["600519.SH"]
    assert rows.iloc[0]["close"] == 1378.274611
    assert rows.iloc[1]["close"] == 1410.89, "缺因子的那根必须保持原价"
    assert "缺复权因子" in capsys.readouterr().err


def test_一根都匹配不上时跳过():
    out = cli.apply_adjust(_FakeAD(_factor_table()), {"600519.SH": _kline(times=["1999-01-04"])},
                           "qfq")
    assert out == {}


# 缺陷 4：因子表按 {code: DataFrame} 判断，实际是宽表 {code: Series}
def test_因子表是宽表列而不是两列DataFrame():
    """get_backward_factor 返回 {code: Series}，索引 = 交易日。
    旧实现要求带 'factor' 列的 DataFrame，对 Series 恒判不符 → --adjust 一次都跑不通。"""
    factors = _factor_table()
    assert isinstance(factors["600519.SH"], pd.Series)
    out = cli.apply_adjust(_FakeAD(factors), {"600519.SH": _kline()}, "qfq")
    assert out["600519.SH"].iloc[0]["close"] == 1378.274611


def test_多只代码各用各的因子():
    factors = _factor_table(codes=("600519.SH", "000858.SZ"))
    factors["000858.SZ"] = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-04-20", "2026-06-26"]))
    klines = {"600519.SH": _kline(code="600519.SH"),
              "000858.SZ": _kline(close=120.5, code="000858.SZ")}
    out = cli.apply_adjust(_FakeAD(factors), klines, "qfq")
    assert out["600519.SH"].iloc[0]["close"] == 1378.274611
    assert out["000858.SZ"].iloc[0]["close"] == round(120.5 * 1.0 / 2.0, 6)


def test_非DataFrame的K线原样放行():
    payload = {"600519.SH": "not-a-frame"}
    assert cli.apply_adjust(_FakeAD(_factor_table()), payload, "qfq") == payload


def test_本地整表缺这只时先补下载再复权(capsys):
    """整表快照语义：本地表在但缺某只，只打印一句就原样返回。

    分批同步每批代码不同，不补下载的话只有缓存命中的那几只拿得到复权价，
    其余静默消失——exit 0、错误表为空。补下载走业务层自己的
    `is_local=False`，CLI 不拼 `local_path`，数据根由它自己定。
    """
    fetched = _factor_table()
    base = _FakeLocalTable(local={}, fetched=fetched)
    out = cli.apply_adjust(_FakeAD(None, base=base), {"600519.SH": _kline()}, "qfq")
    assert out["600519.SH"].iloc[0]["close"] == 1378.274611
    assert [is_local for _codes, is_local in base.calls] == [True, False], \
        "先读本地，KeyError 之后才补下载"
    assert "已按需补下载" not in capsys.readouterr().err, "补齐了就不该再报缺"


def test_补下载仍拿不到就整只跳过并可数(capsys):
    fetched = _factor_table(codes=("000858.SZ",))
    base = _FakeLocalTable(local={}, fetched=fetched)
    out = cli.apply_adjust(_FakeAD(None, base=base), {"600519.SH": _kline()}, "qfq")
    assert out == {}
    err = capsys.readouterr().err
    assert "已按需补下载" in err and "600519.SH" in err
    assert "复权未完成：1/1" in err, "补不回来也要有可数的汇总"


def test_取因子失败仍是运行期错误():
    class Boom(_FakeAD):
        def __init__(self):
            super().__init__({})

        def BaseData(self):
            base = _FakeBase({})

            def _boom(codes):
                raise OSError("网关不可达")

            base.get_backward_factor = _boom
            return base

    with pytest.raises(cli.RuntimeFailure) as err:
        cli.apply_adjust(Boom(), {"600519.SH": _kline()}, "qfq")
    assert "取复权因子失败" in str(err.value)


# --------------------------------------------------------------------------
# 五、stdout 严格只有 JSON，告警走 stderr
# --------------------------------------------------------------------------

def _fake_ad(method, frame):
    owner = SimpleNamespace(**{method: lambda *a, **k: frame})
    return SimpleNamespace(InfoData=lambda: owner)


@pytest.mark.parametrize("category,method", [
    ("base", "stock_basic"), ("shareholder", "dividend"), ("index", "industry_info"),
    ("other", "margin_summary"), ("other", "kzz_conv"),
])
def test_stdout只有一个JSON值(monkeypatch, capsys, category, method):
    spec = cli.CATEGORIES[category][method]
    api_method = spec.api.split(".")[1]
    frame = pd.DataFrame(SHAPES[method][1])
    monkeypatch.setattr(cli, "load_amazingdata", lambda: _fake_ad(api_method, frame))

    args = ["--codes", "600519.SH"] if "codes" in spec.options else []
    if "date" in spec.options:
        args = ["--date", "20260424"]
    assert cli.main([category, method, *args]) == cli.EXIT_OK

    captured = capsys.readouterr()
    assert captured.err == "", f"正常路径不该有 stderr：{captured.err!r}"
    assert json.loads(captured.out), "stdout 必须整份是一个 JSON 值"
    assert isinstance(json.loads(captured.out), dict)


def test_告警只走stderr(monkeypatch, capsys):
    """缺分组列的记录要告警，但 stdout 仍是一份完整 JSON。"""
    frame = pd.DataFrame([
        {"index": 0, "MARKET_CODE": "600519.SH", "HOLDER_NUM": 1},
        {"index": 1, "MARKET_CODE": None, "HOLDER_NUM": 2},
    ])
    monkeypatch.setattr(cli, "load_amazingdata",
                        lambda: _fake_ad("get_holder_num", frame))
    assert cli.main(["shareholder", "holder_num", "--codes", "600519.SH"]) == cli.EXIT_OK

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload) == {"600519.SH", "holder_num"}
    assert "MARKET_CODE" in captured.err


def test_子进程里stdout也只有JSON():
    """真实跑一次子进程，确认没有隐形的 print 混进去。"""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp",
           "TGW_HOST": "127.0.0.1", "TGW_PORT": "1",
           "TGW_USER": "u", "TGW_PASSWORD": "p"}
    proc = subprocess.run(
        [sys.executable, str(CLI), "--help"], capture_output=True, text=True,
        env=env, check=False,
    )
    assert proc.returncode == cli.EXIT_OK
    assert proc.stdout.strip()
    assert proc.stderr == ""


# --------------------------------------------------------------------------
# 六、code_list 仍是字符串数组
# --------------------------------------------------------------------------

def test_code_list_仍返回字符串数组():
    spec, _ = cli.run(["base", "code_list"])
    assert cli.normalize(spec, ["600519.SH", "000858.SZ"]) == ["600519.SH", "000858.SZ"]


def test_返回dict的方法不受分组改动影响():
    """16 个本来就吐 {code: [...]} 的方法，形状保持原样（3.2：无改动）。"""
    spec, _ = cli.run(["finance", "income", "--codes", "600519.SH"])
    result = {"600519.SH": pd.DataFrame([{"REPORTING_PERIOD": 20251231, "OPERA_REV": 1700.0}])}
    out = cli.normalize(spec, result)
    assert list(out) == ["600519.SH"]
    assert out["600519.SH"][0]["OPERA_REV"] == 1700.0
