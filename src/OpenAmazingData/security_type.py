"""证券类型判定：代码算不算某一类证券。

两张表抄自厂商 `config/security_type_config.py`，由 .pyc 的常量表逐字抽出。`base_type` 是单个市场的代码正则，`extra_type`
是一组 `base_type` 的名字。

判定用 `re.match`：从头匹配、**不锚尾**（厂商 `utils/security_type.py`）。
名字两张表里都没有就返回 False，不抛错。
"""

from __future__ import annotations

import re

#: 单市场的代码正则。
BASE_TYPE: dict[str, tuple[str, ...]] = {
    "MARKET_SH": (
        r"\d{6}.SH",
    ),
    "MARKET_SZ": (
        r"\d{6}.SZ",
    ),
    "ZJ_FUTURE": (
        r"[a-zA-Z0-9]{6}.CFE", r"[a-zA-Z0-9]{5}.CFE", r"[a-zA-Z0-9]{4}.CFE",
        r"[a-zA-Z0-9]{3}.CFE",
    ),
    "SQ_FUTURE": (
        r"[a-zA-Z0-9]{6}.SHF", r"[a-zA-Z0-9]{5}.SHF", r"[a-zA-Z0-9]{4}.SHF",
        r"[a-zA-Z0-9]{3}.SHF",
    ),
    "DS_FUTURE": (
        r"[a-zA-Z0-9]{6}.DCE", r"[a-zA-Z0-9]{5}.DCE", r"[a-zA-Z0-9]{4}.DCE",
        r"[a-zA-Z0-9]{3}.DCE",
    ),
    "ZS_FUTURE": (
        r"[a-zA-Z0-9]{6}.CZC", r"[a-zA-Z0-9]{5}.CZC", r"[a-zA-Z0-9]{4}.CZC",
        r"[a-zA-Z0-9]{3}.CZC",
    ),
    "SN_FUTURE": (
        r"[a-zA-Z0-9]{6}.INE", r"[a-zA-Z0-9]{5}.INE", r"[a-zA-Z0-9]{4}.INE",
        r"[a-zA-Z0-9]{3}.INE",
    ),
    "SH_OPTION": (
        r"\d{8}.SH",
    ),
    "SZ_OPTION": (
        r"\d{8}.SZ",
    ),
    "ZJ_OPTION": (
        r"[A-Za-z]{1,3}\d{4}-[CP]-\d+.CFE",
    ),
    "SQ_OPTION": (
        r"[A-Za-z]{1,3}\d{4}[CP]\d+.SHF",
    ),
    "DS_OPTION": (
        r"[A-Za-z]{1,3}\d{4}_[CP]_\d+.DCE",
    ),
    "ZS_OPTION": (
        r"[A-Za-z]{1,3}\d{4}[CP]\d+.CZC",
    ),
    "SN_OPTION": (
        r"[A-Za-z]{1,3}\d{4}[CP]\d+.INE",
    ),
    "SZ_HKT": (
        r"0\d{4}.SZ",
    ),
    "SH_HKT": (
        r"0\d{4}.SH",
    ),
    "SH_KZZ": (
        r"110\d{3}.SH", r"111\d{3}.SH", r"113\d{3}.SH", r"118\d{3}.SH",
    ),
    "SH_INDEX": (
        r"000\d{3}.SH",
    ),
    "SH_A": (
        r"60\d{4}.SH", r"688\d{3}.SH", r"689\d{3}.SH",
    ),
    "SH_B": (
        r"90\d{4}.SH",
    ),
    "SH_ETF": (
        r"510\d{3}.SH", r"511\d{3}.SH", r"512\d{3}.SH", r"513\d{3}.SH", r"515\d{3}.SH",
        r"516\d{3}.SH", r"517\d{3}.SH", r"518\d{3}.SH", r"520\d{3}.SH", r"526\d{3}.SH",
        r"530\d{3}.SH", r"551\d{3}.SH", r"560\d{3}.SH", r"561\d{3}.SH", r"562\d{3}.SH",
        r"563\d{3}.SH", r"588\d{3}.SH", r"589\d{3}.SH",
    ),
    "SH_GLRA": (
        r"204\d{3}.SH",
    ),
    "SH_STAR_BOARD": (
        r"68\d{4}.SH",
    ),
    "SZ_KZZ": (
        r"123\d{3}.SZ", r"127\d{3}.SZ", r"128\d{3}.SZ",
    ),
    "SZ_INDEX": (
        r"39\d{4}.SZ", r"47\d{4}.SZ", r"48\d{4}.SZ", r"98\d{4}.SZ", r"97\d{4}.SZ",
    ),
    "SZ_A": (
        r"00\d{4}.SZ", r"30\d{4}.SZ",
    ),
    "SZ_B": (
        r"20\d{4}.SZ",
    ),
    "SZ_GEM_BORAD": (
        r"30\d{4}.SZ",
    ),
    "SZ_ETF": (
        r"159\d{3}.SZ", r"158\d{3}.SZ",
    ),
    "SZ_GLRA": (
        r"1318\d{2}.SZ",
    ),
    "BJ_INDEX": (
        r"899\d{3}.BJ",
    ),
    "BJ_A": (
        r"430\d{3}.BJ", r"83\d{4}.BJ", r"870\d{3}.BJ", r"871\d{3}.BJ", r"872\d{3}.BJ",
        r"873\d{3}.BJ", r"920\d{3}.BJ",
    ),
    "BJ_A_NEW": (
        r"920\d{3}.BJ", r"899\d{3}.BJ",
    ),
}

#: 跨市场的组合，值是 `BASE_TYPE` 的键。
EXTRA_TYPE: dict[str, tuple[str, ...]] = {
    "EXTRA_STOCK_A": (
        "SH_A", "SZ_A", "BJ_A",
    ),
    "EXTRA_INDEX_A": (
        "SH_INDEX", "SZ_INDEX", "BJ_INDEX",
    ),
    "EXTRA_STOCK_A_SH_SZ": (
        "SH_A", "SZ_A",
    ),
    "EXTRA_IDNEX_A_SH_SZ": (
        "SH_INDEX", "SZ_INDEX",
    ),
    "EXTRA_INDEX_A_SH_SZ": (
        "SH_INDEX", "SZ_INDEX",
    ),
    "EXTRA_ETF": (
        "SH_ETF", "SZ_ETF",
    ),
    "EXTRA_GLRA": (
        "SH_GLRA", "SZ_GLRA",
    ),
    "EXTRA_KZZ": (
        "SH_KZZ", "SZ_KZZ",
    ),
    "EXTRA_HKT": (
        "SH_HKT", "SZ_HKT",
    ),
    "EXTRA_FUTURE": (
        "ZJ_FUTURE", "SQ_FUTURE", "DS_FUTURE", "ZS_FUTURE", "SN_FUTURE",
    ),
    "EXTRA_ETF_OP": (
        "SH_OPTION", "SZ_OPTION",
    ),
    "EXTRA_CF_OP": (
        "SQ_OPTION", "DS_OPTION", "ZS_OPTION", "SN_OPTION",
    ),
    "EXTRA_INDEX_OP": (
        "ZJ_OPTION", "SH_OPTION", "SZ_OPTION",
    ),
    "EXTRA_OP": (
        "SQ_OPTION", "DS_OPTION", "ZS_OPTION", "SN_OPTION", "ZJ_OPTION", "SH_OPTION",
        "SZ_OPTION",
    ),
}


def is_security_type(code: str, security_type: str) -> bool:
    """`code` 命中 `security_type` 的任一正则返回 True。

    展开顺序照厂商：先查 `base_type`，查不到再查 `extra_type` 并把它列的
    基础类型摊平；两处都查不到返回 False。
    """
    patterns = BASE_TYPE.get(security_type)
    if patterns is None:
        patterns = tuple(regex
                         for name in EXTRA_TYPE.get(security_type, ())
                         for regex in BASE_TYPE[name])
    return any(re.match(regex, code) for regex in patterns)
