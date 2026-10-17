"""CLI 层：`<category> <method> [options]` 的参数解析、分发、JSON 序列化。

对外接口是 6 大类 39 个方法，契约见 `CATEGORIES`（本文件底部的事实源）。
输出格式：

    stdout  只有 JSON：code_list 是字符串数组，其余是 {code: [records...]}
    stderr  全部错误信息与告警，绝不污染 stdout

口径**按 SKILL.md，不按参照实现**。参照对 16 个没有代码维度的方法吐裸数组
`[records...]`，我们照样分组成 `{code: [records...]}`——record 里本来带着
代码，摆在键上比让调用方再扫一遍划算。唯一的形状差异是 `snapshot`，见

本文件只干三件事：解析参数、分发到自研 SDK（`src/OpenAmazingData/`）、
把业务层的 DataFrame 序列化成 JSON。协议编解码在 tgwpy.compat / tgwpy.frames 里。

CATEGORIES 是 39 个方法的唯一事实源：argparse 的子命令、必填校验、
分发目标全部从它生成。

**不再依赖厂商发行物**：业务层是自研包，登录用的 CA 证书随仓发布
（`src/tgwpy/cert/dgw-ca.crt`）。公开仓 clone 下来就能跑，不用先解 wheel。

凭据只从环境变量读，不落盘、不进 git：
    TGW_HOST / TGW_PORT / TGW_USER / TGW_PASSWORD
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

# tgwpy 自身的导入一律写在函数里：本文件可以被 `python src/tgwpy/cli.py`
# 直接跑，那时 sys.path[0] 是 src/tgwpy，顶层 import tgwpy 拿不到。

# 以 `python src/tgwpy/cli.py` 方式直接跑时，sys.path[0] 是 src/tgwpy，
# 补上 src 才能 `import tgwpy.compat` 与 `import OpenAmazingData`。
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

EXIT_OK = 0
EXIT_USAGE = 2      # 参数/用法错误
EXIT_RUNTIME = 3    # 凭据、登录、网络、协议失败
EXIT_ENV = 4        # 本地运行环境缺件
EXIT_SERVER = 5     # 网关主动不可用（连续失败被闸门拦下），值得过会儿重跑

# SKILL.md 的合法取值
SECURITY_TYPES = (
    "EXTRA_STOCK_A", "SH_A", "SZ_A", "BJ_A", "EXTRA_STOCK_A_SH_SZ",
    "EXTRA_INDEX_A", "EXTRA_ETF", "EXTRA_KZZ", "EXTRA_HKT", "EXTRA_GLRA",
    "EXTRA_ETF_OP", "SH_OPTION", "SZ_OPTION", "ZJ_FUTURE",
)
MARKETS = ("SH", "SZ", "BJ", "CFE")
PERIODS = (
    "min1", "min3", "min5", "min10", "min15", "min30", "min60", "min120",
    "day", "week", "month", "season", "year",
)
ADJUST_MODES = ("none", "qfq", "hfq")
TREASURY_TERMS = ("m3", "y1", "y5", "y10", "y30")

# 底层 tgw 调用。取值对应 AmazingData 业务层字节码里实际调用的
# tgw.QueryThirdInfo / tgw.QueryKline / tgw.QuerySnapshot /
# tgw.QuerySecuritiesInfo / tgw.QueryETFInfo
# （build/AmazingData-1.1.15-cp313-none-any 下各 .pyc 的 LOAD_ATTR 实测）。
CALL_THIRD_INFO = "QueryThirdInfo"
CALL_KLINE = "QueryKline"
CALL_SNAPSHOT = "QuerySnapshot"
CALL_SECURITIES_INFO = "QuerySecuritiesInfo"
CALL_ETF_INFO = "QueryETFInfo"


class CliError(Exception):
    """参数/用法错误，退出码 2。"""


class RuntimeFailure(Exception):
    """凭据、登录、网络或协议层失败，退出码 3。"""


class SetupError(Exception):
    """本地运行环境缺件，退出码 4。"""


#: 本进程持有的单会话锁。模块级是因为锁必须活到 `release_session()`——
#: 挂在 `load_amazingdata` 的局部变量上会被 GC 掉，之后谁也放不掉它。
_SESSION_LOCK: Any = None


@dataclass(frozen=True)
class MethodSpec:
    """一个 CLI 方法的完整契约。

    api 写成 "InfoData.get_income" 这种带类名的形式，分发时按点号拆开。
    function_ids 来自抓包实证，契约变了这里先变。
    """

    method: str
    api: str
    call: str
    function_ids: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    # 业务层 get_* 是否吃 begin_date/end_date 关键字参数。字节码实测：
    # get_stock_basic / get_bj_code_mapping / get_industry_base_info /
    # get_index_constituent / get_kzz_* / get_option_* 都没有 **kwargs。
    accepts_dates: bool = False
    # --terms 走的是 code_list 位（国债收益率的 indi_type）
    codes_are_terms: bool = False
    # 返回值是「没有代码维度」的裸 DataFrame 时，按 record 里的哪一列分组成
    # SKILL.md 约定的 {code: [records...]}。逐方法显式配，不靠猜——
    # 每列的依据见参照实现的输出字段清单。
    group_column: str = ""
    type_default: str = "EXTRA_STOCK_A"
    note: str = ""


def _spec(method: str, api: str, call: str, **kw: Any) -> MethodSpec:
    return MethodSpec(method=method, api=api, call=call, **kw)


_DATES = ("begin", "end")
_CODES_DATES = ("codes", "begin", "end")

# 16 个方法返回的是「没有代码维度」的裸 DataFrame，参照实现原样吐成裸数组
# `[records...]`。我们按 SKILL.md 分组成 `{code: [records...]}`——record 里本来
# 就带着代码，摆在键上比让调用方再扫一遍划算。分组列逐方法写在各自的
# `group_column` 上，依据是参照实现实测的
# 字段清单：13 个带 `MARKET_CODE`，另 3 个的自然键分别是 `NEW_CODE`
# （`bj_code_mapping`）、`INDEX_CODE`（`industry_info`）、`EXCHANGE`
# （`margin_summary` 是全市场汇总，按交易所分比按代码分合理）。

# 6 大类 39 个方法。顺序即 SKILL.md 的表格顺序。
CATEGORIES: dict[str, dict[str, MethodSpec]] = {
    "base": {
        "code_list": _spec(
            "code_list", "BaseData.get_code_list", CALL_THIRD_INFO,
            function_ids=("A010010011",), options=("type",),
            note="先用 QueryThirdInfo 拉 A010010011 的表头，再用 QuerySecuritiesInfo 补英文名",
        ),
        "code_info": _spec(
            "code_info", "BaseData.get_code_info", CALL_SECURITIES_INFO,
            options=("type",),
        ),
        "calendar": _spec(
            "calendar", "BaseData.get_calendar", CALL_THIRD_INFO,
            function_ids=("A010061003",), options=("market",),
        ),
        "stock_basic": _spec(
            "stock_basic", "InfoData.get_stock_basic", CALL_THIRD_INFO,
            function_ids=("A010010001",), options=("codes",), required=("codes",),
            group_column="MARKET_CODE",
        ),
        "future_codes": _spec(
            "future_codes", "BaseData.get_future_code_list", CALL_SECURITIES_INFO,
            options=("type",), type_default="ZJ_FUTURE",
            note="期货代码表，用 A 股类型查没有意义，默认 ZJ_FUTURE",
        ),
        "option_codes": _spec(
            "option_codes", "BaseData.get_option_code_list", CALL_SECURITIES_INFO,
            options=("type",),
        ),
        "history_status": _spec(
            "history_status", "InfoData.get_history_stock_status", CALL_THIRD_INFO,
            function_ids=("A010060002", "A010060005"),
            options=_CODES_DATES, required=("codes",), accepts_dates=True,
        ),
        "bj_code_mapping": _spec(
            "bj_code_mapping", "InfoData.get_bj_code_mapping", CALL_THIRD_INFO,
            function_ids=("A010010010",), group_column="NEW_CODE",
        ),
    },
    "market": {
        "kline": _spec(
            "kline", "MarketData.query_kline", CALL_KLINE,
            options=(*_CODES_DATES, "period", "adjust", "allow_partial"),
            required=("codes", "begin", "end"),
            note="无 function_id，走 tgw.QueryKline；--adjust 由本层套复权因子",
        ),
        "snapshot": _spec(
            "snapshot", "MarketData.query_snapshot", CALL_SNAPSHOT,
            options=_CODES_DATES, required=("codes", "begin", "end"),
            note="无 function_id，走 tgw.QuerySnapshot",
        ),
    },
    "finance": {
        "balance_sheet": _spec(
            "balance_sheet", "InfoData.get_balance_sheet", CALL_THIRD_INFO,
            function_ids=("A010050001",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "income": _spec(
            "income", "InfoData.get_income", CALL_THIRD_INFO,
            function_ids=("A010050002",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "cash_flow": _spec(
            "cash_flow", "InfoData.get_cash_flow", CALL_THIRD_INFO,
            function_ids=("A010050003",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "profit_express": _spec(
            "profit_express", "InfoData.get_profit_express", CALL_THIRD_INFO,
            function_ids=("A010050007",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "profit_notice": _spec(
            "profit_notice", "InfoData.get_profit_notice", CALL_THIRD_INFO,
            function_ids=("A010050005",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
    },
    "shareholder": {
        "top10": _spec(
            "top10", "InfoData.get_share_holder", CALL_THIRD_INFO,
            function_ids=("A010040001",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "holder_num": _spec(
            "holder_num", "InfoData.get_holder_num", CALL_THIRD_INFO,
            function_ids=("A010040005",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "equity_structure": _spec(
            "equity_structure", "InfoData.get_equity_structure", CALL_THIRD_INFO,
            function_ids=("A010010004",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "pledge_freeze": _spec(
            "pledge_freeze", "InfoData.get_equity_pledge_freeze", CALL_THIRD_INFO,
            function_ids=("A010040004",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "restricted": _spec(
            "restricted", "InfoData.get_equity_restricted", CALL_THIRD_INFO,
            function_ids=("A010040003",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "dividend": _spec(
            "dividend", "InfoData.get_dividend", CALL_THIRD_INFO,
            function_ids=("A010030001",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "right_issue": _spec(
            "right_issue", "InfoData.get_right_issue", CALL_THIRD_INFO,
            function_ids=("A010030002",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
    },
    "index": {
        "constituent": _spec(
            "constituent", "InfoData.get_index_constituent", CALL_THIRD_INFO,
            function_ids=("A010200002",), options=("codes",), required=("codes",),
        ),
        "weight": _spec(
            "weight", "InfoData.get_index_weight", CALL_THIRD_INFO,
            function_ids=("A010200005",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "industry_info": _spec(
            "industry_info", "InfoData.get_industry_base_info", CALL_THIRD_INFO,
            function_ids=("A010200006",), group_column="INDEX_CODE",
        ),
        "industry_constituent": _spec(
            "industry_constituent", "InfoData.get_industry_constituent", CALL_THIRD_INFO,
            function_ids=("A010200003",), options=("codes",), required=("codes",),
        ),
        "industry_weight": _spec(
            "industry_weight", "InfoData.get_industry_weight", CALL_THIRD_INFO,
            function_ids=("A010200008",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "industry_daily": _spec(
            "industry_daily", "InfoData.get_industry_daily", CALL_THIRD_INFO,
            function_ids=("A010200007",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
    },
    "other": {
        "margin_summary": _spec(
            "margin_summary", "InfoData.get_margin_summary", CALL_THIRD_INFO,
            function_ids=("A010080002",), options=("date",), required=("date",),
            accepts_dates=True, group_column="EXCHANGE",
            note="全市场汇总，没有 --codes；--date 同时作为 begin_date/end_date",
        ),
        "margin_detail": _spec(
            "margin_detail", "InfoData.get_margin_detail", CALL_THIRD_INFO,
            function_ids=("A010080001",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "long_hu_bang": _spec(
            "long_hu_bang", "InfoData.get_long_hu_bang", CALL_THIRD_INFO,
            function_ids=("A010070002",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "block_trading": _spec(
            "block_trading", "InfoData.get_block_trading", CALL_THIRD_INFO,
            function_ids=("A010070001",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True, group_column="MARKET_CODE",
        ),
        "etf_pcf": _spec(
            "etf_pcf", "BaseData.get_etf_pcf", CALL_ETF_INFO,
            options=("codes",), required=("codes",),
            note="无 function_id，走 tgw.QueryETFInfo",
        ),
        "fund_share": _spec(
            "fund_share", "InfoData.get_fund_share", CALL_THIRD_INFO,
            function_ids=("A010090002",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "fund_iopv": _spec(
            "fund_iopv", "InfoData.get_fund_iopv", CALL_THIRD_INFO,
            function_ids=("A010090003",), options=_CODES_DATES,
            required=("codes",), accepts_dates=True,
        ),
        "treasury_yield": _spec(
            "treasury_yield", "InfoData.get_treasury_yield", CALL_THIRD_INFO,
            function_ids=("A010050009",), options=("terms", *_DATES),
            required=("terms",), accepts_dates=True, codes_are_terms=True,
            note="--terms 进 code_list 位，对应契约里的 indi_type 参数",
        ),
        "option_basic_info": _spec(
            "option_basic_info", "InfoData.get_option_basic_info", CALL_THIRD_INFO,
            function_ids=("A010500001",), options=("codes",), required=("codes",),
            group_column="MARKET_CODE",
        ),
        "kzz_issuance": _spec(
            "kzz_issuance", "InfoData.get_kzz_issuance", CALL_THIRD_INFO,
            function_ids=("A010400008",), options=("codes",), required=("codes",),
            group_column="MARKET_CODE",
        ),
        "kzz_conv": _spec(
            "kzz_conv", "InfoData.get_kzz_conv", CALL_THIRD_INFO,
            function_ids=("A010400003",), options=("codes",), required=("codes",),
            group_column="MARKET_CODE",
        ),
    },
}

# 6 个分类
CATEGORY_NAMES = ("base", "market", "finance", "shareholder", "index", "other")


# --------------------------------------------------------------------------
# 参数类型
# --------------------------------------------------------------------------

def parse_date(text: str) -> int:
    """8 位整数日期，如 20260420，**返回 int**。

    业务层拿它做数值比较（`date_list_split_len` 切分、`reindex` 对齐），
    传 str 会炸 `TypeError: '<=' not supported between instances of
    'str' and 'int'`。服务端契约里的 `start_date` / `end_date` 也是整数字面量。
    """
    raw = text.strip()
    if len(raw) != 8 or not raw.isdigit():
        raise argparse.ArgumentTypeError(
            f"日期必须是 8 位整数（yyyymmdd），收到 {text!r}"
        )
    return int(raw)


def parse_codes(text: str) -> list[str]:
    """逗号分隔的代码表，如 600519.SH,000858.SZ。"""
    codes = [c.strip() for c in text.split(",")]
    codes = [c for c in codes if c]
    if not codes:
        raise argparse.ArgumentTypeError("--codes 不能为空")
    return codes


def parse_terms(text: str) -> list[str]:
    """国债收益率期限，逗号分隔。"""
    terms = [t.strip() for t in text.split(",")]
    terms = [t for t in terms if t]
    if not terms:
        raise argparse.ArgumentTypeError("--terms 不能为空")
    bad = [t for t in terms if t not in TREASURY_TERMS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"不支持的期限 {bad}，可选 {'/'.join(TREASURY_TERMS)}"
        )
    return terms


def today_str() -> str:
    return date.today().strftime("%Y%m%d")


# --------------------------------------------------------------------------
# argparse：子命令全部由 CATEGORIES 生成
# --------------------------------------------------------------------------

def _add_option(parser: argparse.ArgumentParser, spec: MethodSpec, name: str) -> None:
    required = name in spec.required
    if name == "codes":
        parser.add_argument(
            "--codes", required=required, type=parse_codes, metavar="C1,C2",
            help="逗号分隔的代码表，如 600519.SH,000858.SZ",
        )
    elif name == "terms":
        parser.add_argument(
            "--terms", required=required, type=parse_terms, metavar="y1,y5",
            help="国债收益率期限，逗号分隔，可选 " + "/".join(TREASURY_TERMS),
        )
    elif name in _DATES:
        parser.add_argument(
            f"--{name}", required=required, type=parse_date, metavar="YYYYMMDD",
            help="8 位整数日期，如 20260420",
        )
    elif name == "period":
        parser.add_argument(
            "--period", choices=PERIODS, default="day", metavar="PERIOD",
            help="K 线周期，默认 day，可选 " + "/".join(PERIODS),
        )
    elif name == "adjust":
        parser.add_argument(
            "--adjust", choices=ADJUST_MODES, default="none", metavar="MODE",
            help="复权方式，默认 none，可选 none/qfq(前复权)/hfq(后复权)",
        )
    elif name == "allow_partial":
        parser.add_argument(
            "--allow-partial", action="store_true",
            help="复权有缺口时交出能复权的部分（剔除缺因子的行与代码，stderr 汇总）；"
                 "缺省整次失败",
        )
    elif name == "type":
        parser.add_argument(
            "--type", choices=SECURITY_TYPES, default=spec.type_default,
            metavar="SECURITY_TYPE",
            help=f"security_type，默认 {spec.type_default}",
        )
    elif name == "market":
        parser.add_argument(
            "--market", choices=MARKETS, default="SH", metavar="MARKET",
            help="市场，默认 SH，可选 " + "/".join(MARKETS),
        )
    elif name == "date":
        parser.add_argument(
            "--date", required=required, type=parse_date, metavar="YYYYMMDD",
            help="8 位整数日期，如 20260424",
        )
    else:  # pragma: no cover - CATEGORIES 写错时立刻暴露
        raise AssertionError(f"未知的 option {name!r}（方法 {spec.method}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="AmazingData 查询 CLI（arm macOS 原生实现）",
        epilog="凭据从环境变量读：TGW_HOST TGW_PORT TGW_USER TGW_PASSWORD"
               "（CA 证书默认用仓内 src/tgwpy/cert/dgw-ca.crt，TGW_CA_CERT 可覆盖）",
    )
    cats = parser.add_subparsers(dest="category", metavar="<category>")
    for category in CATEGORY_NAMES:
        methods = CATEGORIES[category]
        cat_p = cats.add_parser(category, help=f"{category} 类方法")
        sub = cat_p.add_subparsers(dest="method", metavar="<method>")
        for method, spec in methods.items():
            desc = f"{spec.api}（{spec.call}）"
            if spec.function_ids:
                desc += " " + "/".join(spec.function_ids)
            m_p = sub.add_parser(method, help=desc, description=desc)
            for name in spec.options:
                _add_option(m_p, spec, name)
            m_p.add_argument(
                "--use-cache", action="store_true",
                help="沿用业务层的本地缓存（厂商语义：有缓存就不发请求）。"
                     "缺省每次向服务端现取",
            )
    return parser


# --------------------------------------------------------------------------
# 运行环境
# --------------------------------------------------------------------------

def read_credentials() -> dict[str, Any]:
    """只从环境变量读，缺一个都不猜。"""
    names = ("TGW_HOST", "TGW_PORT", "TGW_USER", "TGW_PASSWORD")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeFailure(
            "缺少环境变量 " + " ".join(missing) + "（凭据只从环境变量读，不落盘）"
        )
    try:
        port = int(os.environ["TGW_PORT"])
    except ValueError as exc:
        raise RuntimeFailure(f"TGW_PORT 必须是整数，收到 {os.environ['TGW_PORT']!r}") from exc
    return {
        "username": os.environ["TGW_USER"],
        "password": os.environ["TGW_PASSWORD"],
        "host": os.environ["TGW_HOST"],
        "port": port,
    }


def _is_incomplete_calendar(exc: BaseException) -> bool:
    """是不是自研包的日历异常（`OpenAmazingData.base.IncompleteCalendar`）。

    延迟导入：包没装上时这里抛的异常会盖掉真正的错误，而它只在成功
    登录之后的调用链上才可能出现，那时包必然已经在 `sys.modules` 里。
    """
    try:
        from OpenAmazingData.base import IncompleteCalendar
    except Exception:  # noqa: BLE001 - 包没装上：不是日历异常
        return False
    return isinstance(exc, IncompleteCalendar)


def load_amazingdata() -> Any:
    """装传输层，再 import 自研包，最后登录。

    业务层是 `src/OpenAmazingData/`（自研，与厂商 1.1.15 的暴露面一致）。
    它顶层就 import `tgwpy.compat`，传输层必须在它之前就位。

    **传输层必须一起装。** 只 `install()` 的话 `_SESSION.transport is None`，
    compat 的 `Login()` 直接返回 False，表现为业务层那句 `login fail` + exit 3，
    看起来像凭据问题——其实同一份凭据走 `src/tgwpy/client.py logon` 是成功的。
    `install_transport()` 内部会 `install_compat()` + `set_transport()`。

    **单会话锁在装传输层之前拿。** 同一账号第二条连接登录就是 -98，
    而 `Close()` 是「关连接」不是「退会话」，所以本进程结束时锁会正常释放，
    远端的会话还得靠它自己超时。

    顺序：读凭据 → 查 CA 证书 → 拿锁 → 装传输层 → import → 登录。
    凭据在证书之前，是为了让「没配环境变量」报成退出码 3 而不是 4——
    缺前者是配置问题，缺后者是本地缺件，两者该分开。
    """
    try:
        from tgwpy.wire import install_transport
    except ImportError as exc:
        raise SetupError(f"找不到 tgwpy.wire：{exc}") from exc

    creds = read_credentials()

    # 证书找不到的话，报错会一路掉进 `_ensure_ready` 的 `except Exception`
    # 变成一句「连不上网关」。在登录前先问一次，退出码 4，路径写在消息里。
    try:
        from tgwpy.client import resolve_ca_cert

        resolve_ca_cert()
    except FileNotFoundError as exc:
        raise SetupError(str(exc)) from exc

    # 拿不到锁就别往下走——登录一定失败，报出来还容易被当成凭据问题。
    from tgwpy.guard import SessionBusy, SessionLock

    global _SESSION_LOCK
    _SESSION_LOCK = SessionLock(creds["username"], creds["host"])
    try:
        _SESSION_LOCK.require(timeout=_env_float("TGW_LOCK_WAIT", 0.0))
    except SessionBusy as exc:
        _SESSION_LOCK = None
        raise RuntimeFailure(str(exc)) from exc

    try:
        install_transport(
            host=creds["host"], port=creds["port"],
            user=creds["username"], password=creds["password"],
        )
    except Exception as exc:
        release_session()
        raise SetupError(
            f"装入传输层失败：{type(exc).__name__}: {exc}"
        ) from exc

    try:
        import OpenAmazingData as ad
    except Exception as exc:
        release_session()
        raise SetupError(
            f"import OpenAmazingData 失败：{type(exc).__name__}: {exc}"
        ) from exc

    # 登录失败时是 print('login fail') + exit(0)（与厂商 tgw_login 同一套语义）。
    # 两样都得拦：报错信息不能进 stdout，退出码 0 也不能冒充登录成功。
    # 所以把它的 stdout 整个转到 stderr，再自己判成败。
    noise = io.StringIO()
    try:
        with contextlib.redirect_stdout(noise):
            ad.login(
                username=creds["username"], password=creds["password"],
                host=creds["host"], port=creds["port"],
            )
    except SystemExit as exc:
        detail = noise.getvalue().strip() or "无输出"
        release_session()
        raise RuntimeFailure(
            f"登录 {creds['host']}:{creds['port']} 失败"
            f"（业务层 exit {exc.code}）：{detail}"
        ) from exc
    except Exception as exc:
        release_session()
        raise RuntimeFailure(
            f"登录 {creds['host']}:{creds['port']} 失败：{type(exc).__name__}: {exc}"
        ) from exc
    if noise.getvalue().strip():
        print(noise.getvalue().strip(), file=sys.stderr)
    return ad


def release_session() -> None:
    """放掉单会话锁并停掉后台事件循环。

    进程退出前调一次，异常路径上也得调到——`flock` 靠进程退出兜底是
    最后一道保险，不该是常规路径。
    """
    global _SESSION_LOCK
    lock, _SESSION_LOCK = _SESSION_LOCK, None
    if lock is not None:
        lock.release()
    try:
        from tgwpy.compat import tgw_module as tm

        transport = tm._SESSION.transport
    except Exception:  # noqa: BLE001 - 没装上传输层时没什么可关的
        transport = None
    if transport is not None:
        try:
            transport.shutdown()
        except Exception:  # noqa: BLE001 - 收尾不该盖掉真正的错误
            pass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _market_data(ad: Any, end: Any = None) -> Any:
    """MarketData 要一份交易日历，来源就是 BaseData.get_calendar。

    日历只要到本次查询的 `--end`（不晚于今天）：`MarketData` 只用它挑区间内的
    交易日。只查历史区间时就不必去核实最新日历，磁盘缓存覆盖得到就零请求（U3）。
    """
    today = today_str()
    until = min(str(end), today) if end else today
    calendar = ad.BaseData().get_calendar("str", "SH", until)
    return ad.MarketData(calendar)


def _resolve(ad: Any, spec: MethodSpec, opts: argparse.Namespace | None = None) -> Any:
    """把 "InfoData.get_income" 变成绑好的方法。"""
    cls_name, method = spec.api.split(".")
    owner = ad
    if cls_name == "MarketData":
        owner = _market_data(ad, getattr(opts, "end", None))
    else:
        owner = getattr(ad, cls_name)()
    return getattr(owner, method)


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------

def _date_kwargs(spec: MethodSpec, opts: argparse.Namespace) -> dict[str, str]:
    """把 --date/--begin/--end 翻成业务层的 begin_date/end_date。

    没有 **kwargs 的 get_* 收到这两个关键字会 TypeError，所以先问过 spec。
    """
    if not spec.accepts_dates:
        return {}
    if getattr(opts, "date", None):
        # margin_summary 用 --date 开一个单日闭区间
        return {"begin_date": opts.date, "end_date": opts.date}
    kwargs = {}
    if getattr(opts, "begin", None):
        kwargs["begin_date"] = opts.begin
    if getattr(opts, "end", None):
        kwargs["end_date"] = opts.end
    return kwargs


# 收 --type 的四个方法，签名都是 (security_type)
_TYPE_ARG_METHODS = frozenset({
    "get_code_list", "get_code_info", "get_future_code_list", "get_option_code_list",
})

#: 行情 / 代码表 / ETF 申赎这一路的后缀：业务层 `_codes.split_code` 只认这八个
#: （厂商 `convert.py::get_tgw_type_code` 同表），别的后缀进去是
#: `Exception('未知类型--XX')`。`tests/test_cli.py` 盯着它和 SDK 的表一致。
MARKET_SUFFIXES = frozenset({"SH", "SZ", "BJ", "SHF", "CFE", "DCE", "CZC", "INE"})

#: 资讯类（`QueryThirdInfo`）的代码原样进 `market_code` / `index_code`，不过
#: `split_code`。多出来的两个：`SI` 是申万行业指数（`801030.SI` 行业成分 706 条，
#: 2026-10-11 网关调研 R2 探针实测），`HK` 照旧放行。
INFO_SUFFIXES = MARKET_SUFFIXES | {"HK", "SI"}

#: 代码本体只要求字母数字：期货是 `IF2512.CFE`，期权是 8 位数字，行业指数是
#: 6 位数字。本体对不对交给服务端（错了回空），入口只拦「没后缀」和「后缀
#: 不在这一路认的表里」——前者会在几十行外炸，后者会在业务层里炸。
_SUFFIXED = re.compile(r"^[A-Za-z0-9]+\.([A-Z]+)$")


def _code_suffixes(spec: MethodSpec | None) -> frozenset[str]:
    if spec is not None and spec.call in (CALL_KLINE, CALL_SNAPSHOT, CALL_ETF_INFO):
        return MARKET_SUFFIXES
    return INFO_SUFFIXES


def _check_codes(codes: Sequence[str] | None, spec: MethodSpec | None = None) -> None:
    """代码必须带这个方法认得的交易所后缀，缺了在入口就说清楚。

    业务层的 `get_tgw_type_code(code)` 直接 `code.split(".")` 拆成
    `(market_type, security_code)`，裸码进去就是
    `ValueError: not enough values to unpack (expected 2, got 1)`——
    报错出现在几十行之外，看着像协议问题。

    **不在这里猜交易所。** 猜错比报错更糟：拿 510050 当 SZ 查，出来的
    是另一只票的数据，而且不报错。宁可让人自己写对后缀。

    后缀表按方法的代码域分（`_code_suffixes`）：行情一路不认 `.SI`，资讯一路认。
    曾经全部方法共用一张「5–6 位数字 + SH…CFFEX」的表，误拒了 `801120.SI`
    和 `IF2512.CFE`，却放行了业务层并不认的 `CFFEX`。
    """
    if not codes:
        return
    allowed = _code_suffixes(spec)
    bad = []
    for c in codes:
        m = _SUFFIXED.match(str(c).strip())
        if not m or m.group(1) not in allowed:
            bad.append(str(c))
    if bad:
        sample = ", ".join(bad[:5]) + (" …" if len(bad) > 5 else "")
        raise CliError(
            f"这些代码没带交易所后缀，或后缀不在本方法认的范围里：{sample}\n"
            f"  形如 600519.SH / 159915.SZ / 430047.BJ；本方法认 "
            f"{'/'.join(sorted(allowed))}。\n"
            f"  这里不替你猜——猜错会查到另一只票，而且不报错。"
        )


def _check_range(opts: argparse.Namespace) -> None:
    """`--begin` 晚于 `--end` 在入口拒绝。

    放进业务层的后果不一：权重表曾经永久挂住（V11），别的方法回空、exit 0。
    """
    begin, end = getattr(opts, "begin", None), getattr(opts, "end", None)
    if begin is not None and end is not None and int(begin) > int(end):
        raise CliError(f"--begin {begin} 晚于 --end {end}")


#: 成分类方法走 `Members` 引擎：`is_local=True` 只读本地，缺的代码直接跳过、
#: **不回落到下载**（厂商行为，`info.py` `Members.query`）。
_MEMBERS_METHODS = frozenset({"get_index_constituent", "get_industry_constituent"})


@contextlib.contextmanager
def _call_root(use_cache: bool):
    """这一次调用的数据根。

    **缺省给一个空的临时目录，用完即删**（U1）。业务层是厂商语义：
    `is_local=True` 时有缓存就直接返回、一个请求都不发，成分类没缓存就返回空。
    CLI 交给下游的必须是服务端当前的数据，所以每次调用都从空目录起步、
    `is_local=False`，业务层只能现取。目录按**调用**给而不是按进程给：批处理
    一个进程跑几十批，共用一个目录的话复权因子整表会越攒越大
    （`download_backward_factor` 无条件把本地已有的代码并进请求）。

    `--use-cache` 时返回 None：沿用业务层自己的数据根和缓存语义。

    交易日历的磁盘缓存不在这里：它是本仓自己加的，跟着 `TGW_LOCAL_DATA_ROOT`
    走（`OpenAmazingData.base._calendar_path`），按需增量刷新，不受影响。
    """
    if use_cache:
        yield None
        return
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="tgwpy-fresh-")
    try:
        yield root + os.sep
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _cache_kwargs(fn: Any, root: str | None) -> dict[str, Any]:
    """`root` 不为 None 时，给接受这两个参数的方法补上 `local_path` 与 `is_local=False`。

    按签名判断而不是按方法名列清单：67 个查询方法里有的没有 `is_local`
    （公告、K 线），列清单迟早漂。
    """
    if root is None:
        return {}
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    kwargs: dict[str, Any] = {}
    if "local_path" in params:
        kwargs["local_path"] = root
    if "is_local" in params:
        kwargs["is_local"] = False
    return kwargs


def dispatch(ad: Any, spec: MethodSpec, opts: argparse.Namespace) -> Any:
    with _call_root(bool(getattr(opts, "use_cache", False))) as root:
        return _dispatch(ad, spec, opts, root)


def _dispatch(ad: Any, spec: MethodSpec, opts: argparse.Namespace,
              root: str | None) -> Any:
    fn = _resolve(ad, spec, opts)
    method = spec.api.split(".")[1]
    extra = _cache_kwargs(fn, root)

    if spec.codes_are_terms:
        # 国债收益率：--terms 进 code_list 位，对应契约里的 indi_type
        return fn(opts.terms, **extra)

    if method in _TYPE_ARG_METHODS:
        return fn(opts.type, **extra)

    if method == "get_calendar":
        return fn("str", opts.market, today_str())

    if "codes" not in spec.options:
        return fn(**_date_kwargs(spec, opts), **extra)

    if method == "query_kline":
        # 业务层比较的是 `period == Period.<x>.value`。`Period` 是普通 Enum，
        # `Period.day == Period.day.value` 实测为 False（值是 10100，
        # 枚举本身不等于它的 value）——直接传枚举 13 个分支全不命中，
        # date_list_split_len 留 None，`range(..., None)` 抛 TypeError。
        period = getattr(ad.constant.Period, opts.period).value
        result = fn(opts.codes, opts.begin, opts.end, period)
        if opts.adjust == "none":
            return result
        return apply_adjust(ad, result, opts.adjust,
                            allow_partial=bool(getattr(opts, "allow_partial", False)),
                            root=root)

    if method == "query_snapshot":
        return fn(opts.codes, opts.begin, opts.end)

    result = fn(opts.codes, **_date_kwargs(spec, opts), **extra)
    if root is None and method in _MEMBERS_METHODS and isinstance(result, dict):
        # `--use-cache` 下成分类没缓存的代码被厂商语义跳过；缺的那几只现取，
        # 不让「新环境」看起来像「没有成分股」。
        missing = [c for c in opts.codes if c not in result]
        if missing:
            result = {**result, **fn(missing, is_local=False)}
    return result


#: 复权因子只走 backward_factor 这一张表。ReqGetExFactor 回的是 5 列 CSV，
#: 其中 `cum_factor` 是阶梯累积值，业务层落进 HDF5 的也是它；前复权就是
#: 同一张表除以末值，不需要再取一张 adj_factor 表（那张表落什么未验证）。
#: 证据：抓包原文 + 与参照实现逐点对账。
_FACTOR_GETTER = "get_backward_factor"

#: 四个价格列都按因子缩放，`volume` / `amount` 不动。照参照 cli.py:127。
_PRICE_COLUMNS = ("open", "high", "low", "close")

#: K 线的时间列。参照输出的字段是 `index` `code` `kline_time` `open` `high`
#: `low` `close` `volume` `amount`，**全小写**（reference-output-shapes.md 2.1），
#: 是 39 个方法里唯一小写的一套。
_KLINE_TIME = "kline_time"


def _factor_columns_of(table: Any) -> dict[str, Any] | None:
    """因子表 → `{代码: Series}`。形状不认识返回 None。

    业务层 `get_backward_factor` 返回的是 **DataFrame**（每列一只股票的
    阶梯因子，索引=交易日），不是 `{code: Series}` 字典。之前只认字典，
    于是复权被静默跳过，只打一行警告——`--adjust qfq` 看着成功，
    实际交出去的是没复权的价。两种形状都收。
    """
    import pandas as pd

    if isinstance(table, pd.DataFrame):
        return {str(c): table[c] for c in table.columns}
    if isinstance(table, dict):
        return {str(k): v for k, v in table.items()}
    return None


def _root_kwargs(root: str | None) -> dict[str, Any]:
    """`_call_root` 给的临时数据根；`--use-cache` 时为空，用业务层自己的缺省。"""
    return {"local_path": root} if root is not None else {}


def _fetch_missing_factors(base: Any, codes: Sequence[str], root: str | None = None) -> Any:
    """补下载复权因子，返回下载后的整表；补不回来返回 None。

    **为什么必须补。** `get_backward_factor` 是**整表快照**语义：文件不存在
    才下载，文件在但缺某只时只 `print("... not in local backward_factor")`
    然后把表原样返回。不补的后果：分批同步每批代码不同，因子文件一旦建起来
    就再也不更新，之后**每一批都只有缓存命中的那几只**有复权价，其余静默
    返回空——exit 0、错误表为空，只有 stderr 一行警告。工厂库里 qfq 行数看着
    在涨，其实只涨缓存那部分。

    `is_local=False` 就是补下载那一路：按 `code_list` 重下一轮，本地已有的代码
    一并带上（厂商 `download_backward_factor` 无条件读本地），整表写回
    `basedata/backward_factor/`。数据根只有两种来源：`_call_root` 给的本次调用
    的空目录，或者（`--use-cache`）业务层自己的缺省——本层不另猜路径，猜出来的
    路径会让补下载写到另一个数据根去。
    """
    try:
        return getattr(base, _FACTOR_GETTER)(list(codes), is_local=False,
                                             **_root_kwargs(root))
    except Exception as exc:  # noqa: BLE001
        print(
            f"警告：补下载复权因子失败（{type(exc).__name__}: {exc}）",
            file=sys.stderr,
        )
        return None


class AdjustIncomplete(RuntimeFailure):
    """要求的复权有一行做不出来。退出码 3，stdout 不出任何东西。

    U2（下游集成方反馈）：缺因子的行曾原样保留未复权价、整只缺的静默剔除，
    退出码都是 0。调用方要么拿到完整的复权结果，要么明确失败；要部分结果得
    显式 `--allow-partial`。
    """


def _last_day(series: Any) -> Any:
    import pandas as pd

    valid = series.dropna()
    return pd.to_datetime(valid.index).normalize().max() if not valid.empty else None


def _factors_for(base: Any, codes: Sequence[str], adjust: str,
                 until: dict[str, Any] | None = None,
                 root: str | None = None) -> dict[str, Any]:
    """要复权的这几只，各自的因子列 `{代码: Series}`。

    两种情况会用 `is_local=False` 现取一次（`_fetch_missing_factors`）：

    1. 本地整表缺代码：末尾那次 `factor[code_list]` 抛 `KeyError`（厂商语义：
       只打印一句 `not in local backward_factor`）；
    2. 因子表的最后一天早于这只代码 K 线的最后一天（`until`）：
       `backward_factor.h5` 一旦存在就只读不刷新，缓存建好之后的新交易日
       匹配不到因子（U1）。CLI 默认用隔离的空数据根，这条只在 `--use-cache`
       时才会触发。

    现取之后仍缺的，交给 `apply_adjust` 按 U2 的规则处理。
    """
    downloaded = False
    try:
        table = getattr(base, _FACTOR_GETTER)(list(codes), **_root_kwargs(root))
    except KeyError:
        downloaded = True
        table = _fetch_missing_factors(base, codes, root)
    except Exception as exc:
        if _is_incomplete_calendar(exc):
            raise         # 日历取不全有自己的说法，交给 main 归类
        raise RuntimeFailure(
            f"取复权因子失败（{_FACTOR_GETTER}，adjust={adjust}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    factors = _factor_columns_of(table)
    if factors is None:
        print(f"警告：复权因子形状不认识（{type(table).__name__}）", file=sys.stderr)
        return {}

    if not downloaded and until:
        import pandas as pd

        def _behind(code: str) -> bool:
            series, need = factors.get(code), until.get(code)
            if not isinstance(series, pd.Series) or need is None:
                return False
            last = _last_day(series)
            return last is None or last < need

        stale = [c for c in codes if _behind(c)]
        if stale:
            print(
                f"复权因子表的末日早于 K 线末日（{len(stale)} 只，如 {stale[0]}），"
                "现取一次",
                file=sys.stderr,
            )
            fresh = _factor_columns_of(_fetch_missing_factors(base, codes, root))
            if fresh is not None:
                factors = fresh
            downloaded = True

    missing = [c for c in codes if c not in factors]
    if missing:
        print(
            f"复权因子表缺 {len(missing)} 只（{', '.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}）"
            + ("，已现取" if downloaded else ""),
            file=sys.stderr,
        )
    return factors


def _sample(items: Sequence[str], n: int = 10) -> str:
    return ", ".join(items[:n]) + (" …" if len(items) > n else "")


def apply_adjust(ad: Any, kline_map: Any, adjust: str, allow_partial: bool = False,
                 root: str | None = None) -> Any:
    """按复权因子缩放 K 线的四个价格列。

    公式照参照实现：

        matched = factor.reindex(kline_time)      # 因子是宽表 {code: Series} 的一列
        qfq     matched / factor.iloc[-1]         # 前复权除末值归一
        hfq     matched                           # 后复权直接用

    **复权价和未复权价绝不混在一张表里**（U2）。有任何一行做不出来：

    * 缺省：抛 `AdjustIncomplete`（退出码 3），stderr 列出代码与缺因子的日期；
    * `allow_partial=True`：缺因子的行剔除，整只做不出来的代码剔除，
      stderr 给一条可数汇总，退出码 0。

    因子只向**真有 K 线的那几只**要；`kline_map` 里不是 DataFrame 的值
    （业务层的「没数据」是 None）原样放行，那不是复权失败。
    """
    import pandas as pd

    base = ad.BaseData()
    frames = {c: f for c, f in (kline_map or {}).items()
              if isinstance(f, pd.DataFrame)}
    until = {}
    for code, frame in frames.items():
        if _KLINE_TIME in frame.columns and not frame.empty:
            until[code] = pd.to_datetime(frame[_KLINE_TIME]).dt.normalize().max()
    factors = _factors_for(base, list(frames), adjust, until, root) if frames else {}

    out: dict[str, Any] = {}
    problems: list[str] = []
    dropped: list[str] = []
    trimmed = 0
    for code, frame in (kline_map or {}).items():
        if not isinstance(frame, pd.DataFrame):
            out[code] = frame
            continue
        adjusted, problem, missing_days = _adjust_frame(code, frame, factors, adjust)
        if problem:
            problems.append(f"{code}：{problem}")
        if adjusted is None:
            dropped.append(code)
            continue
        if missing_days:
            trimmed += len(missing_days)
        out[code] = adjusted

    if problems and not allow_partial:
        raise AdjustIncomplete(
            f"复权未完成（adjust={adjust}），{len(problems)}/{len(frames)} 只有问题，"
            "整次不交结果；要部分结果加 --allow-partial：\n  "
            + "\n  ".join(problems[:20])
            + (f"\n  … 另 {len(problems) - 20} 只" if len(problems) > 20 else "")
        )
    if problems:
        print(
            f"复权未完成（--allow-partial）：剔除 {len(dropped)}/{len(frames)} 只"
            f"{'（' + _sample(dropped) + '）' if dropped else ''}，"
            f"另剔除 {trimmed} 根缺因子的 K 线；不含未复权价",
            file=sys.stderr,
        )
        for line in problems[:20]:
            print("  " + line, file=sys.stderr)
    return out


def _adjust_frame(
    code: str, frame: Any, factors: Any, adjust: str
) -> tuple[Any | None, str, list[str]]:
    """复权单只股票的 K 线。

    返回 `(结果, 问题, 缺因子的日期)`：
    * 全部做出来：`(frame, "", [])`；
    * 部分行缺因子：`(只含做出来那些行的 frame, 问题, 日期)`——要不要接受由
      `apply_adjust` 按 `allow_partial` 决定；
    * 整只做不出来：`(None, 问题, [])`。

    因子表是宽表 `{code: Series}`（索引 = 交易日，列名 = 带后缀的代码），
    实测 HDF5 里就是单列的阶梯函数，见 reference-output-shapes.md 1.4。
    """
    import pandas as pd

    if frame.empty:
        return frame, "", []
    factor = factors.get(code) if isinstance(factors, dict) else None
    if not isinstance(factor, pd.Series) or factor.dropna().empty:
        return None, "没有复权因子", []
    if factor.index.has_duplicates:
        return None, "复权因子表有重复交易日", []
    if _KLINE_TIME not in frame.columns:
        return None, f"缺 {_KLINE_TIME} 列（实际 {list(frame.columns)}）", []
    prices = [c for c in _PRICE_COLUMNS if c in frame.columns]
    if not prices:
        return None, f"缺全部价格列 {list(_PRICE_COLUMNS)}", []

    # 两边都归到零点再对齐：K 线的时间列可能是 datetime、date 或字符串，
    # 因子表索引恒为 datetime64[ns]。按值对齐，不碰 frame 自己的索引形态。
    factor = factor.copy()
    factor.index = pd.to_datetime(factor.index).normalize()
    days = pd.to_datetime(frame[_KLINE_TIME]).dt.normalize()
    matched = factor.reindex(days)

    hit = matched.notna().to_numpy()
    missing_days = sorted({d.strftime("%Y%m%d") for d in days[~hit]})
    if not hit.any():
        return None, f"{len(frame)} 根 K 线都匹配不上复权因子（{_sample(missing_days, 5)}）", []

    anchor = factor.dropna().iloc[-1]
    multiplier = matched if adjust == "hfq" else matched / anchor
    out = frame.copy()
    # multiplier 的索引是 K 线时间，frame 的是行号，两者对不上。借 to_numpy
    # 换成按行对齐，免得 pandas 按索引对齐出一个全 NaN 的「复权价」。
    scale = pd.Series(multiplier.to_numpy(), index=out.index)
    for col in prices:
        out[col] = (out[col] * scale).round(6)
    if hit.all():
        return out, "", []
    problem = (f"{int((~hit).sum())}/{len(hit)} 根 K 线缺复权因子"
               f"（{_sample(missing_days, 5)}）")
    return out[hit], problem, missing_days


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

def to_jsonable(value: Any) -> Any:
    """业务层的 DataFrame / numpy 标量 / Timestamp 统一转成 json 能吃的结构。"""
    import pandas as pd

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, pd.DataFrame):
        return json.loads(value.to_json(orient="records", date_format="iso"))
    if isinstance(value, pd.Series):
        return to_jsonable(value.tolist())
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    # numpy 标量不是 python int/float 的子类，取 .item() 再走一遍
    if hasattr(value, "item") and getattr(value, "shape", None) == ():
        return to_jsonable(value.item())
    return str(value)


def _frame_records(frame: Any) -> Any:
    """DataFrame -> records 列表；索引非默认时先落成列，别把日期弄丢。"""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        return to_jsonable(frame)
    if not isinstance(frame.index, pd.RangeIndex):
        frame = frame.reset_index()
    return to_jsonable(frame)


def normalize(spec: MethodSpec, result: Any) -> Any:
    """抹平业务层各方法不同的返回形状，凑成 SKILL.md 约定的两种格式。"""
    import pandas as pd

    if spec.method == "code_list":
        return [str(c) for c in (result or [])]

    if spec.method == "snapshot":
        # query_snapshot 直接返回 {date: {code: DataFrame}}，没有错误表。
        # 字节码实测 market_data.pyc 的 query_snapshot：result 由
        # `BUILD_MAP 0`（offset 844）建出来，末尾只有一条
        # `self.lock.release(); return result`（RETURN_VALUE @ 1946），
        # 全函数零个 BUILD_TUPLE。曾经这里拆过一个 2-tuple，没有任何证据
        # 支撑，删掉。
        return _flatten_by_code(result)

    if isinstance(result, dict):
        return {str(k): _frame_records(v) for k, v in result.items()}
    if isinstance(result, pd.DataFrame):
        return _group_by_column(spec, result)
    return to_jsonable(result)


def _group_by_column(spec: MethodSpec, frame: Any) -> dict[str, Any]:
    """裸 DataFrame -> `{code: [records...]}`，按 spec.group_column 那列分组。

    键取列值原样，`MARKET_CODE` 是 `600519.SH` 就写 `600519.SH`，不裁后缀。
    依据：参照实现在本来就返回 `{code: [...]}` 的 16 个方法里，键也是带后缀的
    （`600519.SH` / `000300.SH` / `801030.SI` / `510050.SH`，见
    参照实现的输出实测），裁了反倒跟它们对不上；要补后缀
    就得先猜交易所，那是拿猜出来的值充数。

    分组列缺失或为空的 record 归进 `spec.method` 桶，不悄悄丢掉——这说明
    自然键选错了或网关改了字段，告警走 stderr，stdout 仍然只有 JSON。
    """
    records = _frame_records(frame)
    if not spec.group_column:
        return {spec.method: records}

    column = spec.group_column
    blank = sum(1 for r in records if not r.get(column))
    if blank:
        print(
            f"警告：{spec.method} 有 {blank}/{len(records)} 条记录缺 {column}，"
            f"归入 {spec.method!r} 桶",
            file=sys.stderr,
        )
    grouped: dict[str, list[Any]] = {}
    for record in records:
        key = str(record[column]) if record.get(column) else spec.method
        grouped.setdefault(key, []).append(record)
    return grouped


def _flatten_by_code(by_date: Any) -> dict[str, Any]:
    """{date: {code: df}} -> {code: [records...]}，日期落在 record 里。"""
    merged: dict[str, list[Any]] = {}
    for _, code_map in (by_date or {}).items():
        for code, frame in (code_map or {}).items():
            merged.setdefault(str(code), []).extend(_frame_records(frame))
    return merged


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def run(argv: Sequence[str]) -> tuple[MethodSpec, argparse.Namespace]:
    """只解析，不碰网络。测试用它把「参数是否解析正确」和「网络是否通」拆开。"""
    parser = build_parser()
    args = parser.parse_args(list(argv))
    if not args.category:
        raise CliError("缺少 <category>，可选 " + "/".join(CATEGORY_NAMES))
    if not args.method:
        raise CliError(
            f"{args.category} 缺少 <method>，可选 "
            + "/".join(CATEGORIES[args.category])
        )
    spec = CATEGORIES[args.category][args.method]
    # 能在本地判定的错误都在这里拦：登录要占单会话锁、消耗一次会话，
    # 参数错不该付这个代价。
    if "codes" in spec.options:
        _check_codes(getattr(args, "codes", None), spec)
    _check_range(args)
    return spec, args


def main(argv: Sequence[str] | None = None) -> int:
    from tgwpy.guard import ServerUnavailable

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec, opts = run(argv)
    except SystemExit as exc:          # argparse 自己的 --help / 用法错误
        return int(exc.code or EXIT_OK)
    except CliError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        ad = load_amazingdata()
        # 业务层在下载/落盘时会 print 进度（"本地无 X" / "X 下载完成"）。
        # stdout 只能有 JSON，这些全赶到 stderr 去——`redirect_stdout` 得
        # 覆盖 dispatch 整段，光包 login 不够。
        noise = io.StringIO()
        with contextlib.redirect_stdout(noise):
            result = dispatch(ad, spec, opts)
        if noise.getvalue().strip():
            print(noise.getvalue().rstrip(), file=sys.stderr)
    except CliError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except SetupError as exc:
        print(f"环境缺件：{exc}", file=sys.stderr)
        return EXIT_ENV
    except RuntimeFailure as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except ServerUnavailable as exc:
        # 网关主动不可用了。退出码单列一个，批处理的调度方能据此
        # 「过两小时再跑」而不是当成任务本身失败。
        print(f"网关不可用，已停手：{exc}", file=sys.stderr)
        return EXIT_SERVER
    except Exception as exc:           # 网络/协议层兜底，明确区别于参数错误
        if _is_incomplete_calendar(exc):
            # 退出码与其它运行期失败一致（3），但文案要说清是哪一种：
            # 日历截断去查网络是白查，入口在 `TGW_CALENDAR_FIRST_DAY`。
            print(f"运行失败：交易日历取不全：{exc}", file=sys.stderr)
            return EXIT_RUNTIME
        print(
            f"运行失败（网络/协议层，与参数无关）：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME
    finally:
        release_session()

    try:
        payload = normalize(spec, result)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:           # 序列化失败同样不该污染 stdout
        print(f"序列化失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
