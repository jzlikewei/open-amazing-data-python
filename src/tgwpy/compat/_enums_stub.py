"""tgw 枚举的取值来源。

真正的枚举数值在原生库里，由 `tgwpy/enums.py`（挖掘 `libtgw.so` 的 agent 维护）
提供。本模块只做一件事：优先转发到它，缺失时退回占位定义。

**占位值能不能用？** 能，但只在本层内部用。AmazingData 业务层拿枚举做的事
只有三种（`utils/convert.py::get_market`、`utils/constant.py::Period`、
`on_data.py::process_data`，均为字节码核实）：`==` 比较、`in (kSuccess,
kDataEmpty)` 判定、以及当 dict key。全部只要求「同一枚举内取值互不相同」。
把数值写上网关的是 `tgwpy/frames/`，那一层直接用 `tgwpy.enums`，不经过本文件。

所以占位值不会污染协议，也不会让业务层算错。唯一的硬约束是
`ErrorCode.kSuccess == 0`——这个已由 `tgw/interface.py` 的文档串
「返回值: 错误码，0表示无错误」核实，其余取值标 `unverified`。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "ApiMode",
    "ColocatChannelMode",
    "ColocationDataPermission",
    "ErrorCode",
    "EventCode",
    "EventLevel",
    "HistoryTaskStatus",
    "InternetDataPermission",
    "LogLevel",
    "MDDatatype",
    "MarketType",
    "OrderBookType",
    "SubscribeDataType",
    "SubscribeDerivedDataType",
    "SubscribeOrderBookDataType",
    "SubscribeType",
    "VarietyCategory",
    "MINED_VALUES_VERIFIED",
    "VALUES_SOURCE",
]

#: 完整的枚举名清单。转发到 `tgwpy.enums` 前逐个核对，缺一个就整体退回占位，
#: 避免出现「一半真值一半假值」的混合体。
_ENUM_NAMES = frozenset(__all__) - {"MINED_VALUES_VERIFIED", "VALUES_SOURCE"}

#: 取值是否来自原生库挖掘结果。False 表示当前是占位值。
MINED_VALUES_VERIFIED = False
#: 取值来源说明，排查协议问题时第一眼要看的东西。
VALUES_SOURCE = "compat/_enums_stub.py 的占位顺序值（tgwpy.enums 尚未落地）"

# 成员名逐个抄自 SWIG 生成的 linux_py313_x64_package/tgw.py 的类体，
# 与原生库一致；数值全部是占位顺序值，标 unverified。
_MARKET = {
    "kNone": None, "kNEEQ": 1, "kSHFE": 2, "kCFFEX": 3, "kDCE": 4,
    "kCZCE": 5, "kINE": 6, "kSSE": 7, "kSZSE": 8, "kHKEx": 9,
    "kBK": 10, "kMax": 11,
}
_ERROR = {
    "kFailure": 1, "kUnInited": 2, "kNullSpi": 3, "kParamIllegal": 4,
    "kNetError": 5, "kPermissionError": 6, "kLogonFailed": 7,
    "kAllocateMemoryFailed": 8, "kChannelError": 9, "kOverLoad": 10,
    "kLogoned": 11, "kHqsError": 12, "kNonQueryTimePeriod": 13,
    "kDbAndCodeTableNoCode": 14, "kIllegalMode": 15, "kThreadBusy": 16,
    "kParseDataError": 17, "kTimeout": 18, "kFlowOverLimit": 19,
    "kCodeTableCacheNotAvailable": 20, "kOverMaxSubLimit": 21,
    "kLostConnection": 22, "kOverMaxQueryLimit": 23, "kFunctionIdNull": 24,
    "kDataEmpty": 25, "kUserNotExist": 26, "kVerifyFailure": 27,
    "kApiInterfaceUsing": 28, "kTaskIdRepeat": 29, "kDqsError": 30,
    "kSuccess": 0,
}
_MDDATATYPE = {
    "k1KLine": 1, "k3KLine": 2, "k5KLine": 3, "k10KLine": 4, "k15KLine": 5,
    "k30KLine": 6, "k60KLine": 7, "k120KLine": 8, "kDayKline": 9,
    "kWeekKline": 10, "kMonthKline": 11, "kSeasonKline": 12,
    "kYearKline": 13, "kTickExecution": 14, "kSnapshot": 15,
}
_SUBDATA = {
    "kNone": 0, "k1MinKline": 1, "k3MinKline": 2, "k5MinKline": 3,
    "k10MinKline": 4, "k15MinKline": 5, "k30MinKline": 6, "k60MinKline": 7,
    "k120MinKline": 8, "kSnapshotDerive": 9, "kSnapshot": 10,
    "kOptionSnapshot": 11, "kHKTSnapshot": 12, "kIndexSnapshot": 13,
    "kAfterHourFixedPriceSnapshot": 14, "kCSIIndexSnapshot": 15,
    "kCnIndexSnapshot": 16, "kHKTRealtimeLimit": 17,
    "kHKTProductStatus": 18, "kHKTVCM": 19, "kFutureSnapshot": 20,
    "kSnapshotL2": 21, "kTickOrder": 22, "kTickExecution": 23,
    "kOrderQueue": 24,
}
_VARIETY = {
    "kNone": 0, "kStock": 1, "kFund": 2, "kBond": 3, "kOption": 4,
    "kIndex": 5, "kHKT": 6, "kFutureOption": 7, "kCFETSRMB": 8,
    "kHKEx": 9, "kOthers": 10,
}
# 订阅权限表是位掩码，占位值按 1<<n 排，保证按位与不会撞车。
_COLOCATION_PERM = {
    n: 1 << i for i, n in enumerate([
        "k1MinKline", "k3MinKline", "k5MinKline", "k10MinKline",
        "k15MinKline", "k30MinKline", "k60MinKline", "k120MinKline",
        "kDayKline", "kWeekKline", "kMonthKline", "kSeasonKline",
        "kYearKline", "kSnapshotDerive", "kTick", "kOrderQueue",
        "kStockSnapshotL2", "kIndexSnapshotL2", "kOptionSnapshot",
        "kHKTSnapshot", "kFutureTickL1", "kFutureTickL2", "kCodeTable",
        "kSecurityInfo", "kExFactorTable", "kStockSnapshotL1",
        "kIndexSnapshotL1",
    ])
}
_INTERNET_PERM = {
    n: 1 << i for i, n in enumerate([
        "kSnapshotDerive", "kStockSnapshot", "kIndexSnapshot",
        "kHKTSnapshot", "kOptionSnapshot", "kFutureSnapshot",
        "kCSIIndexSnapshot", "kAfterHourFixedPriceSnapshot", "k1MinKline",
        "k3MinKline", "k5MinKline", "k10MinKline", "k15MinKline",
        "k30MinKline", "k60MinKline", "k120MinKline", "kDayKline",
        "kWeekKline", "kMonthKline", "kSeasonKline", "kYearKline",
        "kHKExSnapshot", "kHKExTickExcution", "kExFactor", "kCodeTable",
        "kSecurityInfo",
    ])
}

_TABLES: dict[str, dict[str, int]] = {
    "ApiMode": {"kColocationMode": 1, "kInternetMode": 2},
    "ColocatChannelMode": {"kTCP": 1, "kQTCP": 2, "kRTCP": 4},
    "ColocationDataPermission": _COLOCATION_PERM,
    "ErrorCode": _ERROR,
    "EventCode": {
        n: i for i, n in enumerate([
            "kUMSConnectSuccess", "kUMSConnectFailed", "kUMSLogonSuccess",
            "kUMSLogonFailed", "kUMSHeartbeatTimeout", "kChannelTCPInitSuccess",
            "kChannelTCPInitFailed", "kChannelTCPConnectSuccess",
            "kChannelTCPConnectFailed", "kChannelTCPLogonSuccess",
            "kChannelTCPLogonFailed", "kChannelTCPSessionClosed",
            "kChannelTCPHeartbeatTimeout", "kOrderBookRejonConnectFailed",
            "kIncompleteData", "kDerivedDataLogonSuccess",
            "kDerivedDataLogonFailed",
        ], start=1)
    },
    "EventLevel": {"kInfo": 1, "kWarn": 2, "kError": 3},
    "HistoryTaskStatus": {
        n: i for i, n in enumerate([
            "kSuccess", "kFailed", "kTaskCancel", "kTaskWaiting", "kTaskTimeOut",
        ])
    },
    "InternetDataPermission": _INTERNET_PERM,
    "LogLevel": {
        n: i for i, n in enumerate(
            ["kTrace", "kDebug", "kInfo", "kWarn", "kError", "kFatal"])
    },
    "MDDatatype": _MDDATATYPE,
    "MarketType": _MARKET,
    "OrderBookType": {"kNone": 0, "kServerOrderBook": 1},
    "SubscribeDataType": _SUBDATA,
    "SubscribeDerivedDataType": {"kOrderBook": 1, "kOrderBookSnapshot": 2},
    "SubscribeOrderBookDataType": {
        "kNone": 0, "kOrderBook": 1, "kOrderBookSnapshot": 2,
    },
    "SubscribeType": {
        n: i for i, n in enumerate(["kSet", "kAdd", "kDel", "kCancelAll"])
    },
    "VarietyCategory": _VARIETY,
}


def _load_mined() -> dict[str, type] | None:
    """从 `tgwpy.enums` 取全部枚举；缺任何一个名字就返回 None。"""
    try:
        from tgwpy import enums as mined
    except ImportError:
        return None
    missing = sorted(n for n in _ENUM_NAMES if not hasattr(mined, n))
    if missing:
        raise ImportError(
            f"tgwpy.enums 存在但缺少 {missing}；请与 compat 需要的枚举名对齐，"
            "或先删除该文件让本模块退回占位定义。"
        )
    return {name: getattr(mined, name) for name in _ENUM_NAMES}


_mined = _load_mined()
if _mined is not None:
    MINED_VALUES_VERIFIED = True
    VALUES_SOURCE = "tgwpy.enums（libtgw.so 挖掘结果）"
    globals().update(_mined)
else:
    for _enum_name, _members in _TABLES.items():
        globals()[_enum_name] = IntEnum(_enum_name, _members)
    del _enum_name, _members
