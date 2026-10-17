"""代码后缀与市场码互转。行情、代码表、复权因子三处共用。

后缀表取自厂商 `utils/convert.py::get_tgw_type_code`，只有八项；不认的后缀
抛 `Exception('未知类型--<后缀>')`，与厂商同一句话。
"""

from __future__ import annotations

from tgwpy.compat import tgw_module as tgw

MARKET_BY_SUFFIX = {
    "BJ": tgw.MarketType.kNEEQ,
    "SH": tgw.MarketType.kSSE,
    "SZ": tgw.MarketType.kSZSE,
    "SHF": tgw.MarketType.kSHFE,
    "CFE": tgw.MarketType.kCFFEX,
    "DCE": tgw.MarketType.kDCE,
    "CZC": tgw.MarketType.kCZCE,
    "INE": tgw.MarketType.kINE,
}
#: 反向表，回包按市场码拼回带后缀的代码（`convert.py::get_code`）。
SUFFIX_BY_MARKET = {market: suffix for suffix, market in MARKET_BY_SUFFIX.items()}


def split_code(code: str) -> tuple[int, str]:
    """`600519.SH` → (101, `600519`)。"""
    bare, _, suffix = code.partition(".")
    market = MARKET_BY_SUFFIX.get(suffix.upper())
    if market is None:
        raise Exception(f"未知类型--{suffix.upper()}")
    return market, bare


def full_code(market: int, security_code: str) -> str:
    return f"{security_code}.{SUFFIX_BY_MARKET[market]}"
