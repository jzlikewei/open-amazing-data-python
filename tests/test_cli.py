"""CLI 层单测：参数解析、分发目标、JSON 序列化、退出码、覆盖度对账。

测的都是不碰网络的部分。真实网关能不能通不在这里验证——那是
tgwpy.compat 的验收范围。
"""

from __future__ import annotations

import inspect
import json
import marshal
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy import cli  # noqa: E402

CLI = ROOT / "src" / "tgwpy" / "cli.py"
DOC = ROOT / "docs" / "skill-compat.md"
BUILD = ROOT / "build" / "AmazingData-1.1.15-cp313-none-any" / "AmazingData"
PYC = {
    "BaseData": BUILD / "query_api" / "base_data.pyc",
    "MarketData": BUILD / "query_api" / "market_data.pyc",
    "InfoData": BUILD / "query_api" / "info_data.pyc",
}

ALL_METHODS = [
    (cat, method, spec)
    for cat, spec_map in cli.CATEGORIES.items()
    for method, spec in spec_map.items()
]


# --------------------------------------------------------------------------
# 覆盖度
# --------------------------------------------------------------------------

def test_六个分类():
    assert cli.CATEGORY_NAMES == ("base", "market", "finance", "shareholder", "index", "other")


def test_共39个方法():
    assert len(ALL_METHODS) == 39


@pytest.mark.parametrize("category,method,spec", ALL_METHODS, ids=lambda v: getattr(v, "method", v))
def test_每个方法都能解析出正确的分发目标(category, method, spec):
    got, opts = cli.run([category, method] + _required_args(spec))
    assert got is spec
    assert got.api.split(".")[0] in {"BaseData", "MarketData", "InfoData"}


# --------------------------------------------------------------------------
# 分发：拿 .pyc 里的真实签名验调用绑不绑得上
# --------------------------------------------------------------------------

def _real_signature(cls_name: str, method: str) -> inspect.Signature:
    """从业务层 .pyc 读出方法真实签名（含 self、默认值与 **kwargs）。

    形参名和 **kwargs 在 code object 上；默认值只存在于类体字节码里，
    形如 LOAD_CONST <defaults> / LOAD_CONST <code> / MAKE_FUNCTION / STORE_NAME。
    """
    import dis

    code = marshal.loads(PYC[cls_name].read_bytes()[16:])
    for const in code.co_consts:
        if not (isinstance(const, types.CodeType) and const.co_name == cls_name):
            continue
        ins = list(dis.get_instructions(const))
        for i, inst in enumerate(ins):
            if inst.opname != "MAKE_FUNCTION" or i < 2:
                continue
            name = next(
                (x.argval for x in ins[i + 1:] if x.opname == "STORE_NAME"), None
            )
            if name != method:
                continue
            target = next(
                c for c in const.co_consts
                if isinstance(c, types.CodeType) and c.co_name == method
            )
            names = target.co_varnames[:target.co_argcount]
            raw = ins[i - 2].argval
            defaults = tuple(raw) if isinstance(raw, tuple) else ()
            # 默认值对齐尾部：前 n - len(defaults) 个形参没有默认值
            first_default = len(names) - len(defaults)
            params = [
                inspect.Parameter(
                    n, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=(defaults[k - first_default] if k >= first_default
                             else inspect.Parameter.empty),
                )
                for k, n in enumerate(names)
            ]
            if target.co_flags & inspect.CO_VARKEYWORDS:
                params.append(
                    inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD)
                )
            return inspect.Signature(params)
    raise AssertionError(f"{cls_name}.{method} 不在 .pyc 里")


class _FakeAmazingData:
    """记录业务层实际收到的调用，不碰网络。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple, dict]] = []
        self.login_args = None
        # 业务层 `market_data.query_kline` 比的是 `period == Period.<x>.value`。
        # `Period` 是普通 Enum，枚举本身不等于它的 value，所以替身也得给 .value。
        # 常量的位置跟自研包一致：`OpenAmazingData.constant`。
        period = SimpleNamespace(**{
            p: SimpleNamespace(value=f"Period.{p}.value") for p in cli.PERIODS})
        self.constant = SimpleNamespace(Period=period)

    def login(self, **kwargs):
        self.login_args = kwargs

    def _owner(self, cls_name):
        rec = self

        class Owner:
            def __getattr__(self, method):
                def call(*args, **kwargs):
                    rec.calls.append((cls_name, method, args, kwargs))
                    return "RESULT"
                return call

        return Owner()

    def BaseData(self):
        return self._owner("BaseData")

    def InfoData(self):
        return self._owner("InfoData")

    def MarketData(self, calendar):
        self.calls.append(("MarketData", "__init__", (calendar,), {}))
        return self._owner("MarketData")


@pytest.mark.skipif(not BUILD.is_dir(), reason="发行物未解包，解包成 build/ 后再验签名")
# .pyc 是 CPython 3.13 字节码（magic 3571），别的解释器 marshal 不了它，
# 不跳过的话 3.11 上 39 条全报「不在 .pyc 里」，看着像路由错了。
@pytest.mark.skipif(sys.version_info[:2] != (3, 13), reason="厂商 .pyc 是 3.13 字节码")
@pytest.mark.parametrize("category,method,spec", ALL_METHODS, ids=lambda v: getattr(v, "method", v))
def test_分发的调用能绑上业务层真实签名(category, method, spec):
    """路由错了会在这里变成 TypeError，不用等真连上网关才发现。"""
    _, opts = cli.run([category, method] + _required_args(spec))
    fake = _FakeAmazingData()
    cli.dispatch(fake, spec, opts)

    # MarketData 要先取交易日历再造实例，业务调用是最后一次
    business = fake.calls[-1]
    cls_name, called, args, kwargs = business
    assert called == spec.api.split(".")[1], f"{method} 调错了方法 {called}"
    if cls_name == "MarketData":
        assert [c[1] for c in fake.calls] == ["get_calendar", "__init__", called]
    else:
        assert len(fake.calls) == 1, f"{method} 不该发多余的调用"

    sig = _real_signature(cls_name, called)
    sig.bind(object(), *args, **kwargs)      # 绑不上就 TypeError


@pytest.mark.parametrize("category,method,extra,expect", [
    # (额外入参, (args, kwargs) 里该出现什么)
    ("base", "code_list", ["--type", "EXTRA_ETF"], (("EXTRA_ETF",), {})),
    ("base", "code_info", ["--type", "EXTRA_ETF"], (("EXTRA_ETF",), {})),
    ("base", "future_codes", ["--type", "EXTRA_INDEX_A"], (("EXTRA_INDEX_A",), {})),
    ("base", "option_codes", [], (("EXTRA_STOCK_A",), {})),      # 走默认值
    ("base", "bj_code_mapping", [], ((), {})),
    ("base", "stock_basic", [], ((["600519.SH", "000858.SZ"],), {})),
    # 日期一律 int：业务层做数值比较与 reindex，传 str 会炸
    # `'<=' not supported between instances of 'str' and 'int'`
    ("market", "snapshot", [], ((["600519.SH", "000858.SZ"], 20260424, 20260424), {})),
    ("market", "kline", [], ((["600519.SH", "000858.SZ"], 20260424, 20260424,
                              "Period.day.value"), {})),
    ("other", "treasury_yield", [], ((["y1", "y5", "y10"],), {})),
    ("other", "margin_summary", [], ((), {"begin_date": 20260424, "end_date": 20260424})),
    ("finance", "income", [], ((["600519.SH", "000858.SZ"],),
                                {"begin_date": 20260424, "end_date": 20260424})),
    ("index", "constituent", [], ((["600519.SH", "000858.SZ"],), {})),
])
def test_分发传下去的参数值是对的(category, method, extra, expect):
    """绑得上还不够，传错值一样错。"""
    spec = cli.CATEGORIES[category][method]
    _, opts = cli.run([category, method] + _required_args(spec) + extra)
    fake = _FakeAmazingData()
    cli.dispatch(fake, spec, opts)
    args, kwargs = fake.calls[-1][2], fake.calls[-1][3]
    assert (args, kwargs) == expect


def test_calendar_按今天截到当日():
    spec, opts = cli.run(["base", "calendar"])
    fake = _FakeAmazingData()
    cli.dispatch(fake, spec, opts)
    args = fake.calls[-1][2]
    assert args[:2] == ("str", "SH")          # data_type 用 str，market 默认 SH
    assert args[2] == cli.today_str()         # 第三个形参是截至日期


@pytest.mark.skipif(not BUILD.is_dir(), reason="wheel 未解包")
@pytest.mark.parametrize("category,method", [
    ("base", "stock_basic"),
    ("index", "constituent"),
    ("index", "industry_constituent"),
    ("other", "kzz_conv"),
    ("other", "option_basic_info"),
])
def test_没有kwargs的方法不会被塞上begin_end(category, method):
    """get_stock_basic 等没有 **kwargs，塞 begin_date 会 TypeError。"""
    spec = cli.CATEGORIES[category][method]
    assert spec.accepts_dates is False, f"{method} 不该收 --begin/--end"
    _, opts = cli.run([category, method] + _required_args(spec))
    fake = _FakeAmazingData()
    cli.dispatch(fake, spec, opts)
    assert fake.calls[0][3] == {}, f"{method} 不该带日期关键字"


def test_market方法会先造交易日历():
    spec, opts = cli.run(["market", "snapshot", "--codes", "600519.SH",
                          "--begin", "20260424", "--end", "20260424"])
    fake = _FakeAmazingData()
    cli.dispatch(fake, spec, opts)
    assert [c[1] for c in fake.calls] == ["get_calendar", "__init__", "query_snapshot"]
    assert fake.calls[0][0] == "BaseData"     # 交易日历来自 BaseData.get_calendar
    assert fake.calls[0][2] == ("str", "SH", fake.calls[0][2][2])


def test_登录只读环境变量():
    import os
    env = {"TGW_HOST": "h", "TGW_PORT": "8600", "TGW_USER": "u", "TGW_PASSWORD": "p"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        creds = cli.read_credentials()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert creds == {"host": "h", "port": 8600, "username": "u", "password": "p"}


def test_端口不是整数要报错():
    import os
    env = {"TGW_HOST": "h", "TGW_PORT": "not-a-port", "TGW_USER": "u", "TGW_PASSWORD": "p"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with pytest.raises(cli.RuntimeFailure):
            cli.read_credentials()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mapping_rows(text: str, category: str) -> list[str]:
    """取该分类下主对照表的数据行。

    章节里还有「没有 function_id 的方法」这类辅助表，同样会出现方法名，
    所以只认紧跟在 "| method | function_id |" 表头之后、空行之前的那张表。
    """
    section = text.split(f"### {category}")[1].split("\n### ")[0]
    lines = section.splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.startswith("| method | function_id |"))
    rows = []
    for ln in lines[header + 1:]:
        if not ln.startswith("|"):
            break
        rows.append(ln)
    return rows[1:]        # 跳过表头下的分隔行


def test_对照表39行齐全():
    """docs/skill-compat.md 逐行对账，改了一边忘了另一边就红。"""
    text = DOC.read_text(encoding="utf-8")
    total = 0
    for category, method, spec in ALL_METHODS:
        rows = [ln for ln in _mapping_rows(text, category) if ln.startswith(f"| `{method}` |")]
        assert len(rows) == 1, f"{category} {method} 在对照表里应恰好出现 1 行，实际 {len(rows)}"
        cells = [c.strip() for c in rows[0].strip("|").split("|")]

        ids = " / ".join(f"`{i}`" for i in spec.function_ids) if spec.function_ids else "—"
        assert cells[0] == f"`{method}`"
        assert cells[1] == ids, f"{method} function_id 不一致"
        assert spec.call in cells[2], f"{method} 底层调用不一致"
        assert cells[3] == f"`{spec.api}`", f"{method} 业务层入口不一致"
        total += 1
    assert total == 39


def _required_args(spec):
    """按 spec 造一组刚好能过解析的入参。"""
    out = []
    for name in spec.options:
        if name == "codes":
            out += ["--codes", "600519.SH,000858.SZ"]
        elif name == "terms":
            out += ["--terms", "y1,y5,y10"]
        elif name in ("begin", "end", "date"):
            out += [f"--{name}", "20260424"]
    return out


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def test_help_列出6个分类():
    proc = subprocess.run(
        [sys.executable, str(CLI), "--help"], capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    for category in cli.CATEGORY_NAMES:
        assert category in proc.stdout


def test_code_list_解析():
    spec, opts = cli.run(["base", "code_list", "--type", "EXTRA_ETF"])
    assert spec.method == "code_list"
    assert spec.api == "BaseData.get_code_list"
    assert spec.function_ids == ("A010010011",)
    assert opts.type == "EXTRA_ETF"


def test_kline_解析():
    spec, opts = cli.run([
        "market", "kline", "--codes", "600519.SH",
        "--begin", "20260420", "--end", "20260424", "--adjust", "qfq",
    ])
    assert spec.method == "kline"
    assert spec.call == cli.CALL_KLINE
    assert opts.codes == ["600519.SH"]
    assert (opts.begin, opts.end) == (20260420, 20260424)   # int，不是 str
    assert opts.adjust == "qfq"
    assert opts.period == "day"       # 默认值


def test_逗号分隔的codes():
    _, opts = cli.run(["finance", "income", "--codes", "600519.SH, 000858.SZ ,"])
    assert opts.codes == ["600519.SH", "000858.SZ"]


def test_treasury_yield_的terms():
    spec, opts = cli.run(["other", "treasury_yield", "--terms", "y1,y5,y10"])
    assert spec.codes_are_terms is True
    assert opts.terms == ["y1", "y5", "y10"]


def test_margin_summary_的date():
    spec, opts = cli.run(["other", "margin_summary", "--date", "20260424"])
    assert opts.date == 20260424   # int，不是 str
    assert "codes" not in spec.options


def test_没有codes的方法不收codes():
    with pytest.raises(SystemExit) as exc:
        cli.run(["base", "bj_code_mapping", "--codes", "600519.SH"])
    assert exc.value.code == cli.EXIT_USAGE


def test_future_codes_默认ZJ_FUTURE():
    _, opts = cli.run(["base", "future_codes"])
    assert opts.type == "ZJ_FUTURE"


def test_type_默认是EXTRA_STOCK_A():
    _, opts = cli.run(["base", "code_list"])
    assert opts.type == "EXTRA_STOCK_A"


@pytest.mark.parametrize("argv", [
    ["base", "no_such_method"],
    ["no_such_category", "code_list"],
    ["market", "kline", "--codes", "600519.SH", "--begin", "20260420", "--end", "20260424",
     "--period", "min7"],
    ["market", "kline", "--begin", "20260420", "--end", "20260424"],          # 缺 --codes
    ["market", "kline", "--codes", "600519.SH", "--begin", "20260420"],        # 缺 --end
    ["finance", "income"],                                                     # 缺 --codes
    ["base", "code_list", "--type", "BOGUS"],
    ["base", "calendar", "--market", "XN"],
    ["other", "treasury_yield", "--terms", "y7"],
    ["other", "treasury_yield", "--terms", "y1", "--begin", "2026"],           # 日期不是 8 位
    ["base"],
    [],
])
def test_非法参数非零退出(argv):
    assert cli.main(argv) == cli.EXIT_USAGE


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

def _frame(rows, index=None):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(rows)
    if index is not None:
        df.index = pd.Index(index, name="TRADE_DAYS")
    return df


def test_code_list_返回字符串数组():
    spec, _ = cli.run(["base", "code_list"])
    out = cli.normalize(spec, ["600519.SH", "000858.SZ"])
    assert out == ["600519.SH", "000858.SZ"]
    assert json.loads(json.dumps(out, ensure_ascii=False)) == out


def test_普通方法返回code到records():
    spec, _ = cli.run(["finance", "income", "--codes", "600519.SH"])
    result = {"600519.SH": _frame([{"REPORTING_PERIOD": 20251231, "ANN_DATE": "20260328",
                                    "OPERA_REV": 1700.0}])}
    out = cli.normalize(spec, result)
    assert list(out) == ["600519.SH"]
    assert out["600519.SH"][0]["REPORTING_PERIOD"] == 20251231
    assert out["600519.SH"][0]["ANN_DATE"] == "20260328"   # 字符串列原样透传
    assert out["600519.SH"][0]["OPERA_REV"] == 1700.0


def test_裸DataFrame按自然键分组():
    """裸 DataFrame 按 record 里的自然键分组，不再按方法名包一层。

    16 个方法的分组列与形态在 tests/test_output_shapes.py 里整体对账。
    """
    spec, _ = cli.run(["index", "industry_info"])
    out = cli.normalize(spec, _frame([{"INDEX_CODE": "801010"}]))
    assert out == {"801010": [{"INDEX_CODE": "801010"}]}


def test_snapshot_按date摊平成code到records():
    spec, _ = cli.run(["market", "snapshot", "--codes", "600519.SH",
                       "--begin", "20260424", "--end", "20260424"])
    result = {20260424: {"600519.SH": _frame([{"last_price": 1700.0}])}}
    out = cli.normalize(spec, result)
    assert out == {"600519.SH": [{"last_price": 1700.0}]}


def test_索引里的日期不会丢():
    spec, _ = cli.run(["market", "snapshot", "--codes", "600519.SH",
                       "--begin", "20260424", "--end", "20260424"])
    result = {20260424: {"600519.SH": _frame([{"last_price": 1.0}], index=[20260424])}}
    out = cli.normalize(spec, result)
    assert out["600519.SH"][0]["TRADE_DAYS"] == 20260424


def test_非有限浮点转null():
    spec, _ = cli.run(["finance", "income", "--codes", "600519.SH"])
    out = cli.normalize(spec, {"600519.SH": _frame([{"x": float("nan")}])})
    assert out["600519.SH"][0]["x"] is None


def test_nan_infinity_null():
    assert cli.to_jsonable(float("nan")) is None
    assert cli.to_jsonable(float("inf")) is None


def test_时间戳转ISO():
    import datetime as dt
    assert cli.to_jsonable(dt.date(2026, 4, 24)) == "2026-04-24"
    assert cli.to_jsonable(dt.datetime(2026, 4, 24, 15, 0)).startswith("2026-04-24T15:00")


def test_numpy标量能序列化():
    np = pytest.importorskip("numpy")
    assert cli.to_jsonable(np.int64(7)) == 7
    assert cli.to_jsonable(np.float64(1.5)) == 1.5


# --------------------------------------------------------------------------
# 退出码与 stdout 纯净性
# --------------------------------------------------------------------------

def _run(args, env_extra=None):
    import os
    env = dict(os.environ)
    env.pop("TGW_HOST", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True,
        env=env, check=False,
    )


def test_缺凭据是运行期失败不是参数失败():
    # 业务层是自研包（src/OpenAmazingData/），不依赖任何厂商发行物，
    # 任何一份 clone 都跑得到「读凭据」这一步。
    proc = _run(["base", "code_list", "--type", "EXTRA_ETF"])
    assert proc.returncode == cli.EXIT_RUNTIME
    assert proc.stdout == ""
    assert "TGW_HOST" in proc.stderr


def test_装不上替身是环境缺件且stdout干净():
    proc = _run(["base", "code_list", "--type", "EXTRA_ETF"], {
        "TGW_HOST": "127.0.0.1", "TGW_PORT": "8600",
        "TGW_USER": "u", "TGW_PASSWORD": "p",
    })
    assert proc.returncode in (cli.EXIT_ENV, cli.EXIT_RUNTIME)
    assert proc.stdout == "", "任何失败都不许往 stdout 写东西"
    assert proc.stderr.strip()


def test_非法参数时stdout干净():
    proc = _run(["base", "no_such_method"])
    assert proc.returncode == cli.EXIT_USAGE
    assert proc.stdout == ""
    assert proc.stderr.strip()


# -- 代码后缀校验 --------------------------------------------------------
# 集成方实测：`market snapshot --codes 510050`（不带后缀）在业务层的
# `get_tgw_type_code` 里炸 `ValueError: not enough values to unpack`，
# exit 3，报错出现在几十行之外，看着像协议问题。入口就该拦住。

def test_裸代码在入口被拦下并说明原因():
    from tgwpy.cli import CliError, _check_codes

    with pytest.raises(CliError) as exc:
        _check_codes(["510050"])
    msg = str(exc.value)
    assert "510050" in msg
    assert "后缀" in msg
    assert "600519.SH" in msg          # 要给出正确写法


def test_带后缀的代码全部放行():
    from tgwpy.cli import _check_codes

    for code in ("600519.SH", "159915.SZ", "430047.BJ", "822999.BJ"):
        _check_codes([code])           # 不抛就对了


def test_混合列表只点名没后缀的那些():
    from tgwpy.cli import CliError, _check_codes

    with pytest.raises(CliError) as exc:
        _check_codes(["600519.SH", "510050", "159915"])
    msg = str(exc.value)
    # 两行：点名行里只有没后缀的；示例行里都是合法写法
    named = msg.splitlines()[0]
    assert "510050" in named and "159915" in named
    assert "600519.SH" not in named, "合法的那个不该被点名"


def test_空代码表不拦():
    from tgwpy.cli import _check_codes

    _check_codes(None)
    _check_codes([])


# U5：后缀表按方法的代码域分。下游用 `.SI` 查行业成分 / 权重，期货是
# `IF2512.CFE`（CLI 自己的 MARKETS 就写的 CFE）。

@pytest.mark.parametrize("argv", [
    ["index", "industry_constituent", "--codes", "801120.SI"],
    ["index", "industry_weight", "--codes", "801120.SI"],
    ["index", "constituent", "--codes", "000300.SH"],
    ["market", "kline", "--codes", "IF2512.CFE", "--begin", "20251201", "--end", "20251205"],
    ["market", "kline", "--codes", "510300.SH", "--begin", "20251201", "--end", "20251205"],
    ["other", "fund_share", "--codes", "510300.SH"],
])
def test_U5_本方法认得的合法代码放行(argv):
    spec, opts = cli.run(argv)
    assert opts.codes


@pytest.mark.parametrize("argv", [
    ["other", "fund_share", "--codes", "510300"],
    ["index", "industry_constituent", "--codes", "801120"],
    # 行情一路不认 .SI：业务层 split_code 会抛「未知类型--SI」
    ["market", "kline", "--codes", "801120.SI", "--begin", "20251201", "--end", "20251205"],
    # 业务层不认 CFFEX，期货要写 CFE
    ["market", "kline", "--codes", "IF2512.CFFEX", "--begin", "20251201", "--end", "20251205"],
])
def test_U5_裸码与本方法不认的后缀在解析阶段拒绝(argv):
    with pytest.raises(cli.CliError):
        cli.run(argv)


def test_U5_行情后缀表与业务层一致():
    from OpenAmazingData._codes import MARKET_BY_SUFFIX

    assert cli.MARKET_SUFFIXES == frozenset(MARKET_BY_SUFFIX)


def test_C01_参数错误不登录(monkeypatch):
    """代码与区间错误在登录前就以退出码 2 返回，不占会话锁。"""
    called = []
    monkeypatch.setattr(cli, "load_amazingdata", lambda: called.append(1))
    assert cli.main(["other", "fund_share", "--codes", "510300"]) == cli.EXIT_USAGE
    assert cli.main(["market", "kline", "--codes", "600519.SH",
                     "--begin", "20251205", "--end", "20251201"]) == cli.EXIT_USAGE
    assert called == []


# -- 复权缺因子要补下载，不能静默少给 ------------------------------

def test_缺因子时先尝试补下载再判定():
    """`get_backward_factor` 是整表快照语义：文件在但缺某只时它只
    `print(... not in local backward_factor)` 然后把表原样返回。
    不补下载的话，分批同步里每一批都只有缓存命中的那几只拿得到
    复权价，其余 exit 0、错误表为空地消失。"""
    import inspect

    src = inspect.getsource(cli._factors_for)
    assert "_fetch_missing_factors" in src, "缺因子时必须先试补下载"
    # 补不回来时要有可数的汇总，而不是散落的单行警告
    assert "复权未完成" in inspect.getsource(cli.apply_adjust)


def test_补下载交给业务层自己定数据根():
    """补下载不再由 CLI 拼 `local_path`。

    自研包 `get_backward_factor(code_list, is_local=False)` 就是补下载那一路，
    数据根用它自己的缺省（`TGW_LOCAL_DATA_ROOT`，没设 `~/.amazingdata_local/`）。
    CLI 传一个猜出来的路径，写盘就会落到另一个数据根去。
    """
    import inspect

    src = inspect.getsource(cli._fetch_missing_factors)
    assert "is_local=False" in src
    assert "local_path=" not in src, "数据根由业务层自己定，CLI 不该再拼"
    assert not hasattr(cli, "_local_path_of"), "读签名默认值那套随厂商补丁一起没了"


# -- 日历取不全的归类 ------------------------------------------------

def test_交易日历取不全归到运行期失败(monkeypatch, capsys):
    """日历截断不是网络故障，也不是参数错。

    退出码与其它运行期失败一致（3），但文案要说清是哪一种——按「网络不通」
    去查会白查半天，入口在 `TGW_CALENDAR_FIRST_DAY`。
    """
    from OpenAmazingData.base import IncompleteCalendar

    class _NoCalendar:
        def get_calendar(self, *a, **k):
            raise IncompleteCalendar("交易日历取不全：还差 3 个窗口")

    fake = SimpleNamespace(BaseData=lambda: _NoCalendar())
    monkeypatch.setattr(cli, "load_amazingdata", lambda: fake)

    assert cli.main(["market", "snapshot", "--codes", "600519.SH",
                     "--begin", "20260424", "--end", "20260424"]) == cli.EXIT_RUNTIME
    captured = capsys.readouterr()
    assert captured.out == "", "失败时 stdout 仍然只能空"
    assert "交易日历取不全" in captured.err
    assert "网络" not in captured.err, "别把它说成网络故障"


# -- CA 证书：仓内随仓发布，TGW_CA_CERT 覆盖 ----------------------

def test_默认用仓内那张证书(monkeypatch):
    from tgwpy import client

    monkeypatch.delenv("TGW_CA_CERT", raising=False)
    got = client.resolve_ca_cert()
    assert got == client.BUNDLED_CA_CERT
    assert got.is_file(), "证书随仓发布，公开仓不该要求先解 wheel"
    assert got.parent.name == "cert"


def test_仓内证书能被ssl加载():
    """不能验链的证书等于没有。`load_verify_locations` 抛错就是文件坏了。"""
    import ssl

    from tgwpy import client

    ssl.create_default_context().load_verify_locations(
        cafile=str(client.BUNDLED_CA_CERT))


def test_TGW_CA_CERT_覆盖缺省(tmp_path, monkeypatch):
    from tgwpy import client

    mine = tmp_path / "my-ca.crt"
    mine.write_text(client.BUNDLED_CA_CERT.read_text(encoding="ascii"),
                    encoding="ascii")
    monkeypatch.setenv("TGW_CA_CERT", str(mine))
    assert client.resolve_ca_cert() == mine


def test_TGW_CA_CERT_接受波浪号路径(tmp_path, monkeypatch):
    from tgwpy import client

    (tmp_path / "ca.crt").write_text("x", encoding="ascii")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TGW_CA_CERT", "~/ca.crt")
    assert client.resolve_ca_cert() == tmp_path / "ca.crt"


def test_证书不在时报清楚是哪个路径怎么设(tmp_path, monkeypatch):
    from tgwpy import client

    missing = tmp_path / "nope" / "ca.crt"
    monkeypatch.setenv("TGW_CA_CERT", str(missing))
    with pytest.raises(FileNotFoundError) as exc:
        client.resolve_ca_cert()
    msg = str(exc.value)
    assert str(missing) in msg, "报错要带路径"
    assert "TGW_CA_CERT" in msg, "报错要带怎么设"


def test_client与probe共用同一个解析函数():
    from tgwpy import client, probe

    assert probe.resolve_ca_cert is client.resolve_ca_cert


def test_证书缺件报成退出码4():
    """证书不存在是本地缺件（4），不是登录失败（3）。

    它会被传输层的 `except Exception` 吞成一句「连不上网关」，所以登录前
    先自己问一次。"""
    proc = _run(["base", "code_list", "--type", "EXTRA_ETF"], {
        "TGW_HOST": "127.0.0.1", "TGW_PORT": "8600",
        "TGW_USER": "u", "TGW_PASSWORD": "p",
        "TGW_CA_CERT": "/tmp/oad-f6-no-such-ca.crt",
    })
    assert proc.returncode == cli.EXIT_ENV
    assert proc.stdout == ""
    assert "TGW_CA_CERT" in proc.stderr


def test_超时文案不再指向注入传输层():
    """服务端不给数据 ≠ 没注入传输层。集成方在这上面绕了很久。"""
    from tgwpy.compat import tgw_module

    assert "set_transport" in tgw_module.NOT_LOGGED_IN
    assert "set_transport" not in tgw_module.NO_RESPONSE
    assert "官方客户端" in tgw_module.NO_RESPONSE
