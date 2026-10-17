"""AmazingData 业务层用到的全部枚举常量，数值从 x86_64 Linux 原生库反解而来。

证据来源
--------
全部数值都取自 SWIG 扩展 `libtgw_python313.so` 的模块初始化函数
`PyInit_libtgw_python313` @ `0x166f60`（size 9163）。SWIG 把每个 `%constant`
以固定模式注册：

    mov  <立即数>, edi            <- 枚举值
    call SWIG_NewIntegerObj      @ 0xb48a0 / 0xb9d80
    lea  rsi, [rip + <名字串>]
    mov  rdx, rax
    mov  rdi, rbx                <- 模块句柄，不是值
    call SwigPyModule_add_object @ 0xcbf00

每个成员注释里的 `@ 0x…` 就是那次 `SwigPyModule_add_object` 调用的地址，
可逐条复核。反编译 + 运行时两路都能全量重现这 242 个值。

每个值都有反编译与运行时两路交叉验证。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "ErrorCode",
    "MarketType",
    "ApiMode",
    "ColocatChannelMode",
    "MDDatatype",
    "Period",
    "SubscribeDataType",
    "VarietyCategory",
    "LogLevel",
    "EventLevel",
    "EventCode",
    "HistoryTaskStatus",
    "SubscribeType",
    "OrderBookType",
    "SubscribeOrderBookDataType",
    "SubscribeDerivedDataType",
    "ColocationDataPermission",
    "InternetDataPermission",
    "ConstField",
    "FUTURE_SECURITY_TYPE",
]


class ErrorCode(IntEnum):
    """SDK 返回码。0 是成功，其余全部是 -100 起的连续负数。

    -72 / -71 在原生库里没有对应成员，编号空着。原样保留这个空洞，
    免得日后有人「顺手补齐」把后面的值整体挪位。
    """

    kFailure = -100  # @ 0x167b54
    kUnInited = -99  # @ 0x167b72
    kNullSpi = -98  # @ 0x167b90
    kParamIllegal = -97  # @ 0x167bae
    kNetError = -96  # @ 0x167bcc
    kPermissionError = -95  # @ 0x167bea
    kLogonFailed = -94  # @ 0x167c08
    kAllocateMemoryFailed = -93  # @ 0x167c26
    kChannelError = -92  # @ 0x167c44
    kOverLoad = -91  # @ 0x167c62
    kLogoned = -90  # @ 0x167c80
    kHqsError = -89  # @ 0x167c9e
    kNonQueryTimePeriod = -88  # @ 0x167cbc
    kDbAndCodeTableNoCode = -87  # @ 0x167cda
    kIllegalMode = -86  # @ 0x167cf8
    kThreadBusy = -85  # @ 0x167d16
    kParseDataError = -84  # @ 0x167d34
    kTimeout = -83  # @ 0x167d52
    kFlowOverLimit = -82  # @ 0x167d70
    kCodeTableCacheNotAvailable = -81  # @ 0x167d8e
    kOverMaxSubLimit = -80  # @ 0x167dac
    kLostConnection = -79  # @ 0x167dca
    kOverMaxQueryLimit = -78  # @ 0x167de8
    kFunctionIdNull = -77  # @ 0x167e06
    kDataEmpty = -76  # @ 0x167e24
    kUserNotExist = -75  # @ 0x167e42
    kVerifyFailure = -74  # @ 0x167e60
    kApiInterfaceUsing = -73  # @ 0x167e7e
    kTaskIdRepeat = -70  # @ 0x167e9c
    kDqsError = -69  # @ 0x167eba
    kSuccess = 0  # @ 0x167ed3


class MarketType(IntEnum):
    """市场代码。期货所 2-7，股票所 101-103，板块 201。值直接进网络帧。"""

    kNone = 0  # @ 0x167909
    kNEEQ = 2  # @ 0x167925  新三板
    kSHFE = 3  # @ 0x167941  上期所
    kCFFEX = 4  # @ 0x16795d  中金所
    kDCE = 5  # @ 0x167979  大商所
    kCZCE = 6  # @ 0x167995  郑商所
    kINE = 7  # @ 0x1679b1  上海国际能源交易中心
    kSSE = 101  # @ 0x1679cd  上交所
    kSZSE = 102  # @ 0x1679e9  深交所
    kHKEx = 103  # @ 0x167a05  港交所
    kBK = 201  # @ 0x167a21  板块
    kMax = 255  # @ 0x167a3d


class ApiMode(IntEnum):
    """接入模式。公网下载走 kInternetMode。"""

    kColocationMode = 1  # @ 0x1680cb  专线
    kInternetMode = 2  # @ 0x1680e7  公网


class ColocatChannelMode(IntEnum):
    """专线通道模式。64 位位标志，不是连续编号，组合使用时按位或。"""

    kTCP = 8589934592  # @ 0x168108  1 << 33
    kQTCP = 34359738368  # @ 0x168129  1 << 35
    kRTCP = 68719476736  # @ 0x16814a  1 << 36


class MDDatatype(IntEnum):
    """K 线周期代码。10000 起，QueryKline / MDKLine 帧里 cyc_type 的取值。"""

    k1KLine = 10000  # @ 0x16841f
    k3KLine = 10001  # @ 0x16843b
    k5KLine = 10002  # @ 0x168457
    k10KLine = 10003  # @ 0x168473
    k15KLine = 10004  # @ 0x16848f
    k30KLine = 10005  # @ 0x1684ab
    k60KLine = 10006  # @ 0x1684c7
    k120KLine = 10007  # @ 0x1684e3
    kDayKline = 10008  # @ 0x1684ff
    kWeekKline = 10009  # @ 0x16851b
    kMonthKline = 10010  # @ 0x168537
    kSeasonKline = 10011  # @ 0x168553  季线
    kYearKline = 10012  # @ 0x16856f
    kTickExecution = 10013  # @ 0x16858b
    kSnapshot = 10014  # @ 0x1685a7


class SubscribeDataType(IntEnum):
    """订阅推送的数据类型。0-24 连续。"""

    kNone = 0  # @ 0x168163
    k1MinKline = 1  # @ 0x16817f
    k3MinKline = 2  # @ 0x16819b
    k5MinKline = 3  # @ 0x1681b7
    k10MinKline = 4  # @ 0x1681d3
    k15MinKline = 5  # @ 0x1681ef
    k30MinKline = 6  # @ 0x16820b
    k60MinKline = 7  # @ 0x168227
    k120MinKline = 8  # @ 0x168243
    kSnapshotDerive = 9  # @ 0x16825f
    kSnapshot = 10  # @ 0x16827b
    kOptionSnapshot = 11  # @ 0x168297
    kHKTSnapshot = 12  # @ 0x1682b3
    kIndexSnapshot = 13  # @ 0x1682cf
    kAfterHourFixedPriceSnapshot = 14  # @ 0x1682eb
    kCSIIndexSnapshot = 15  # @ 0x168307
    kCnIndexSnapshot = 16  # @ 0x168323
    kHKTRealtimeLimit = 17  # @ 0x16833f
    kHKTProductStatus = 18  # @ 0x16835b
    kHKTVCM = 19  # @ 0x168377
    kFutureSnapshot = 20  # @ 0x168393
    kSnapshotL2 = 21  # @ 0x1683af
    kTickOrder = 22  # @ 0x1683cb
    kTickExecution = 23  # @ 0x1683e7
    kOrderQueue = 24  # @ 0x168403


class VarietyCategory(IntEnum):
    """品种大类。业务层用它筛 `QuerySecuritiesInfo` 的返回。"""

    kNone = 0  # @ 0x1685c0
    kStock = 1  # @ 0x1685dc
    kFund = 2  # @ 0x1685f8
    kBond = 3  # @ 0x168614
    kOption = 4  # @ 0x168630
    kIndex = 5  # @ 0x16864c
    kHKT = 6  # @ 0x168668
    kFutureOption = 7  # @ 0x168684  期货期权
    kCFETSRMB = 8  # @ 0x1686a0  银行间人民币市场
    kHKEx = 9  # @ 0x1686bc
    kOthers = 255  # @ 0x1686d8


class LogLevel(IntEnum):
    """日志级别，0 最啰嗦。"""

    kTrace = 0  # @ 0x167a56
    kDebug = 1  # @ 0x167a72
    kInfo = 2  # @ 0x167a8e
    kWarn = 3  # @ 0x167aaa
    kError = 4  # @ 0x167ac6
    kFatal = 5  # @ 0x167ae2


class EventLevel(IntEnum):
    kInfo = 1  # @ 0x167afe
    kWarn = 2  # @ 0x167b1a
    kError = 3  # @ 0x167b36


class EventCode(IntEnum):
    """连接与登录事件码，1-17 连续。"""

    kUMSConnectSuccess = 1  # @ 0x167eef
    kUMSConnectFailed = 2  # @ 0x167f0b
    kUMSLogonSuccess = 3  # @ 0x167f27
    kUMSLogonFailed = 4  # @ 0x167f43
    kUMSHeartbeatTimeout = 5  # @ 0x167f5f
    kChannelTCPInitSuccess = 6  # @ 0x167f7b
    kChannelTCPInitFailed = 7  # @ 0x167f97
    kChannelTCPConnectSuccess = 8  # @ 0x167fb3
    kChannelTCPConnectFailed = 9  # @ 0x167fcf
    kChannelTCPLogonSuccess = 10  # @ 0x167feb
    kChannelTCPLogonFailed = 11  # @ 0x168007
    kChannelTCPSessionClosed = 12  # @ 0x168023
    kChannelTCPHeartbeatTimeout = 13  # @ 0x16803f
    kOrderBookRejonConnectFailed = 14  # @ 0x16805b
    kIncompleteData = 15  # @ 0x168077
    kDerivedDataLogonSuccess = 16  # @ 0x168093
    kDerivedDataLogonFailed = 17  # @ 0x1680af


class HistoryTaskStatus(IntEnum):
    kSuccess = 0  # @ 0x1686f1
    kFailed = 1  # @ 0x16870d
    kTaskCancel = 2  # @ 0x168729
    kTaskWaiting = 3  # @ 0x168745
    kTaskTimeOut = 4  # @ 0x168761


class SubscribeType(IntEnum):
    kSet = 0  # @ 0x168dcc
    kAdd = 1  # @ 0x168de8
    kDel = 2  # @ 0x168e04
    kCancelAll = 3  # @ 0x168e20


class OrderBookType(IntEnum):
    kNone = 0  # @ 0x168d97
    kServerOrderBook = 2  # @ 0x168db3  编号 1 未使用


class SubscribeOrderBookDataType(IntEnum):
    kNone = 0  # @ 0x168d46
    kOrderBook = 1  # @ 0x168d62
    kOrderBookSnapshot = 2  # @ 0x168d7e


class SubscribeDerivedDataType(IntEnum):
    kOrderBook = 1  # @ 0x168e3c
    kOrderBookSnapshot = 2  # @ 0x168e58


class ColocationDataPermission(IntEnum):
    """专线订阅权限位，1-27 连续。业务层当前不用，留作接口对齐。"""

    k1MinKline = 1  # @ 0x16877d
    k3MinKline = 2  # @ 0x168799
    k5MinKline = 3  # @ 0x1687b5
    k10MinKline = 4  # @ 0x1687d1
    k15MinKline = 5  # @ 0x1687ed
    k30MinKline = 6  # @ 0x168809
    k60MinKline = 7  # @ 0x168825
    k120MinKline = 8  # @ 0x168841
    kDayKline = 9  # @ 0x16885d
    kWeekKline = 10  # @ 0x168879
    kMonthKline = 11  # @ 0x168895
    kSeasonKline = 12  # @ 0x1688b1
    kYearKline = 13  # @ 0x1688cd
    kSnapshotDerive = 14  # @ 0x1688e9
    kTick = 15  # @ 0x168905
    kOrderQueue = 16  # @ 0x168921
    kStockSnapshotL2 = 17  # @ 0x16893d
    kIndexSnapshotL2 = 18  # @ 0x168959
    kOptionSnapshot = 19  # @ 0x168975
    kHKTSnapshot = 20  # @ 0x168991
    kFutureTickL1 = 21  # @ 0x1689ad
    kFutureTickL2 = 22  # @ 0x1689c9
    kCodeTable = 23  # @ 0x1689e5
    kSecurityInfo = 24  # @ 0x168a01
    kExFactorTable = 25  # @ 0x168a1d
    kStockSnapshotL1 = 26  # @ 0x168a39
    kIndexSnapshotL1 = 27  # @ 0x168a55


class InternetDataPermission(IntEnum):
    """公网订阅权限位。编号不连续（1/5/8/9/10/14/15 缺位），原样照搬。

    与 `ColocationDataPermission` 不是同一套编号，别拿后者去推。
    """

    kSnapshotDerive = 2  # @ 0x168a71
    kStockSnapshot = 3  # @ 0x168a8d
    kIndexSnapshot = 4  # @ 0x168aa9
    kHKTSnapshot = 6  # @ 0x168ac5
    kOptionSnapshot = 7  # @ 0x168ae1
    kFutureSnapshot = 11  # @ 0x168afd
    kCSIIndexSnapshot = 12  # @ 0x168b19
    kAfterHourFixedPriceSnapshot = 13  # @ 0x168b35
    k1MinKline = 16  # @ 0x168b51
    k3MinKline = 17  # @ 0x168b6d
    k5MinKline = 18  # @ 0x168b89
    k10MinKline = 19  # @ 0x168ba5
    k15MinKline = 20  # @ 0x168bc1
    k30MinKline = 21  # @ 0x168bdd
    k60MinKline = 22  # @ 0x168bf9
    k120MinKline = 23  # @ 0x168c15
    kDayKline = 24  # @ 0x168c31
    kWeekKline = 25  # @ 0x168c4d
    kMonthKline = 26  # @ 0x168c69
    kSeasonKline = 27  # @ 0x168c85
    kYearKline = 28  # @ 0x168ca1
    kHKExSnapshot = 29  # @ 0x168cbd
    kHKExTickExcution = 30  # @ 0x168cd9  拼写照抄原生库
    kExFactor = 31  # @ 0x168cf5
    kCodeTable = 32  # @ 0x168d11
    kSecurityInfo = 33  # @ 0x168d2d


class ConstField(IntEnum):
    """协议字段定长。截断规则全靠这些数字，搬的时候别改。"""

    kIPMaxLen = 24  # @ 0x1673cc
    kUMSItemLen = 10  # @ 0x1673e8
    kUsernameLen = 32  # @ 0x167404
    kPasswordLen = 64  # @ 0x167420
    kSecurityCodeLen = 16  # @ 0x16743c
    kHistorySecurityCodeLen = 32  # @ 0x167458
    kPushSecurityCodeLen = 32  # @ 0x167474
    kSecurityNameLen = 32  # @ 0x167490
    kTradingPhaseCodeLen = 8  # @ 0x1674ac
    kExChangeInstIDLen = 31  # @ 0x1674c8
    kPositionLevelLen = 10  # @ 0x1674e4
    kMDStreamIDLen = 6  # @ 0x167500
    kSecurityStatusLen = 24  # @ 0x16751c
    kSecuritySymbolLen = 32  # @ 0x167538
    kSecurityEnglishNameLen = 128  # @ 0x167554
    kSecurityTypeLen = 10  # @ 0x167570
    kCurrencyLen = 4  # @ 0x16758c
    kVolumeTradeLen = 24  # @ 0x1675a8
    kValueTradeLen = 24  # @ 0x1675c4
    kFutureSecurityCodeLen = 32  # @ 0x1675e0
    kMDStreamIDMaxLen = 6  # @ 0x1675fc
    kDataPermission = 128  # @ 0x167618
    kTokenMaxLen = 128  # @ 0x167634
    kQuerySecurityCodeLen = 38  # @ 0x167650
    kTradingStatusLen = 8  # @ 0x16766c
    kSymbolLen = 128  # @ 0x167688
    kSecurityAbbreviationLen = 64  # @ 0x1676a4
    kMaxTypesLen = 16  # @ 0x1676c0
    kTypesLen = 8  # @ 0x1676dc
    kCodeTableSecurityStatusMaxLen = 16  # @ 0x1676f8
    RegularShare = 9  # @ 0x167714
    kConsSecurityCodeLen = 32  # @ 0x167730
    kSymbolETFLen = 128  # @ 0x16774c
    KUnderlyingSecurityID = 4  # @ 0x167768
    KReserved = 30  # @ 0x167784
    kManagmentETFLen = 128  # @ 0x1677a0
    KUnderlyingSecurityIDSource = 4  # @ 0x1677bc
    AllCashAmount = 12  # @ 0x1677d8
    AllCashAremiumRate = 7  # @ 0x1677f4  拼写照抄原生库
    AllCashDiscountRate = 7  # @ 0x167810
    DefaultMaxOBSubNum = 500  # @ 0x16782c
    DefaultMaxOBSnapshotSubNum = 3000  # @ 0x167848
    DefaultMaxOBOrderQueueSize = 50  # @ 0x167864
    DefaultMaxOBEntrySize = 20  # @ 0x167880
    DefaultMaxOBDeliverIntervalMs = 50  # @ 0x16789c
    DefaultMinOBSnapshotDeliverIntervalMs = 10  # @ 0x1678b8
    kDetailLen = 40  # @ 0x1678d4
    kHKExOrderSnapshotLevelLen = 20  # @ 0x1678f0


# ---------------------------------------------------------------------------
# 以下两个不是原生枚举，是业务层自己定的
# ---------------------------------------------------------------------------

#: 期货合约的 security_type 过滤值。
#: 证据：`build/AmazingData-1.1.15-cp313-none-any/AmazingData/query_api/base_data.pyc`
#: 的 `get_future_code_list` / `get_future_code_info`，字节码里是
#: `LOAD_CONST '06007'` → `COMPARE_OP ==` 打在 `df['security_type']` 上。
#: 是 5 位**字符串**，不是整数，也不是原生枚举。
FUTURE_SECURITY_TYPE = "06007"


class Period(IntEnum):
    """K 线周期。AmazingData 自己加的枚举，原生库里没有对应类。

    min1..year 直接取 `MDDatatype` 的值；20001 起是快照/逐笔那几档，
    在 `AmazingData/utils/constant.pyc` 的 `Period` 类体里是字面量。
    """

    # 证据：base_data.pyc 等模块里 `tgw.MDDatatype.k1KLine` 之类的 LOAD_ATTR
    min1 = MDDatatype.k1KLine  # 10000
    min3 = MDDatatype.k3KLine  # 10001
    min5 = MDDatatype.k5KLine  # 10002
    min10 = MDDatatype.k10KLine  # 10003
    min15 = MDDatatype.k15KLine  # 10004
    min30 = MDDatatype.k30KLine  # 10005
    min60 = MDDatatype.k60KLine  # 10006
    min120 = MDDatatype.k120KLine  # 10007
    day = MDDatatype.kDayKline  # 10008
    week = MDDatatype.kWeekKline  # 10009
    month = MDDatatype.kMonthKline  # 10010
    season = MDDatatype.kSeasonKline  # 10011
    year = MDDatatype.kYearKline  # 10012
    snapshot = MDDatatype.kSnapshot  # 10014

    # 证据：AmazingData/utils/constant.pyc `Period` 类体的 LOAD_CONST
    snapshotL2 = 20001
    order = 20002
    execution = 20003
    order_queue = 20004
    snapshotHKT = 20005
    snapshotfuture = 20007  # 20006 缺位
    snapshotoption = 20008
