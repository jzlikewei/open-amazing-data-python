"""纯 Python 的 `tgw` 替身，装进 `sys.modules` 供 AmazingData 业务层使用。

**为什么能有替身。** AmazingData 的 80 个模块里只有 10 个 `import tgw`
（逐模块扫 `IMPORT_NAME` 指令核实，名单见模块末尾），其余 70 个是纯计算。
顶层 `__init__.py` 的急切 import 把所有子包都拖下水；换掉这 10 个模块背后的
`tgw`，业务层一行不改就能在 arm macOS 上跑。

**异步契约。** `download_data/download_info_data.py` 的模式是：

    spi  = XxxSpi(...)
    tid  = tgw.GetTaskID()
    tgw.SetThirdInfoParam(tid, "function_id", "A010070001")
    tgw.QueryThirdInfo(tid, query_spi=spi.OnResponse)
    EnvXxx.wait_event.wait()          # ← 阻塞在这里

`spi.OnResponse` 的签名是 `(self, data, status)`——info_spi.pyc 里全部 50 个
Spi 的 `argcount=3, varnames=('self','data','status')` 一致。它对两个入参的
处理决定了替身该回什么：

* `data` —— 业务层直接 `data['MARKET_CODE']` 取值，另有三个 Spi 走
  `json.dumps(data)`。**是已解析好的 dict，不是原始字节**；逆向记录写的
  「原始字节由业务层 json.loads」与字节码不符，以字节码为准。
* `status` —— 四个分支（`HistCodeListSpi.OnResponse` 完整反汇编）：
  `None` → 成功且有数据；`isinstance(status, str)` → 出错，进 `error_list`；
  `status in (kSuccess, kDataEmpty)` → 也进 `error_list`；其它整数 → 不记错误。

**两条硬规则。**

1. *异步形态绝不能阻塞。* 业务层丢弃查询接口的返回值，紧接着
   `wait_event.wait()`。不回调就是永久挂死。所以异步路径无论成功失败都
   一定会调一次 `query_spi`，失败时 `data=None`、`status=<消息字符串>`
   ——字符串是业务层唯一的「记错误」分支，`EnvXxx.error_list` 会如实收到。
2. *同步形态宁可抛异常也不返回空数据。* `base_data.get_code_list` 拿到
   `(code_table_df, error)` 后**丢掉 error**，紧接着 `code_table_df['security_code']`
   （base_data.pyc 第 106–111 行）。回 `None` 会在几十行之外炸成
   `TypeError: 'NoneType' object is not subscriptable`，看不出是谁没实现；
   直接抛 `NotImplementedError` 才能让栈顶指着 `tgw.QuerySecuritiesInfo`。
   空 DataFrame 更糟——那正是「以为下载成功、拿到空表」。

依赖 tgw 的 10 个模块（字节码核实）：
    download_data/download_info_data  download_data/info_spi
    download_data/market_spi          login/tgw_login
    query_api/base_data               query_api/info_data
    query_api/market_data             subscribe_api/on_data
    utils/constant                    utils/convert
"""

from __future__ import annotations

import json
import os

import sys
import threading
import time
import types
from typing import Any, Callable

from tgwpy.compat import structs
from tgwpy.compat._enums_stub import (
    MINED_VALUES_VERIFIED,
    VALUES_SOURCE,
    ApiMode,
    ColocatChannelMode,
    ColocationDataPermission,
    ErrorCode,
    EventCode,
    EventLevel,
    HistoryTaskStatus,
    InternetDataPermission,
    LogLevel,
    MDDatatype,
    MarketType,
    OrderBookType,
    SubscribeDataType,
    SubscribeDerivedDataType,
    SubscribeOrderBookDataType,
    SubscribeType,
    VarietyCategory,
)
from tgwpy.compat.structs import (
    # 下面这一长串是**再导出**：install() 逐个从本模块 globals 取名字挂到
    # tgw 上，所以它们必须在本模块的名字空间里存在，不能只靠 structs.__all__。
    Cfg,
    ColocaCfg,
    Execution,
    IGMDSpi,
    ILogSpi,
    IPushSpi,
    Kline,
    MDFutureSnapshot,
    MDHKTSnapshot,
    MDIndexSnapshot,
    MDKLine,
    MDOptionSnapshot,
    MDOrderQueue,
    MDSnapshotL1,
    MDSnapshotL2,
    MDTickExecution,
    MDTickOrder,
    Order,
    OrderQueue,
    ReqDefault,
    ReqKline,
    Snapshot,
    SnapshotFuture,
    SnapshotHKT,
    SnapshotIndex,
    SnapshotL2,
    SnapshotOption,
    SubCodeTableItem,
    SubscribeItem,
    TGWBaseClass,
    UpdatePassWordReq,
)

__all__ = [
    "install", "uninstall", "is_installed",
    "Login", "Logout", "Close", "GetVersion", "GetErrorMsg", "GetTaskID",
    "SetLogSpi", "SetThirdInfoParam", "QueryThirdInfo", "QueryKline",
    "QuerySnapshot", "QuerySecuritiesInfo", "QueryETFInfo",
    "QueryExFactorTable", "QueryCodeTable", "Subscribe", "UnSubscribe",
    "IGMDApi_UpdatePassWord", "UpdatePassWord", "ReplayRequest", "CancelTask",
    "deliver_response", "deliver_logon", "set_transport", "pending_task_ids",
    "session", "NotConnected", "LOGON_TIMEOUT",
    "ApiMode", "ColocatChannelMode",
    "ColocationDataPermission", "ErrorCode", "EventCode", "EventLevel",
    "HistoryTaskStatus", "InternetDataPermission", "LogLevel", "MDDatatype",
    "MarketType", "OrderBookType", "SubscribeDataType",
    "SubscribeDerivedDataType", "SubscribeOrderBookDataType", "SubscribeType",
    "VarietyCategory",
] + structs.__all__


# --- 异常 -------------------------------------------------------------------


class NotConnected(NotImplementedError):
    """替身没有传输层，或登录未完成。

    同步形态的查询接口直接抛这个；异步形态改用回调把同样的消息送回业务层。
    """


_TRANSPORT_HINT = (
    "需要 tgwpy.compat.set_transport(transport) 注入客户端，"
    "再由它回调 tgwpy.compat.tgw_module.deliver_response(task_id, data, status)"
)
NO_TRANSPORT = f"tgwpy 替身未接传输层，无法发出查询；{_TRANSPORT_HINT}"
NOT_LOGGED_IN = f"未登录：先调 tgw.Login(cfg, tgw.ApiMode.kInternetMode)；{_TRANSPORT_HINT}"

#: 传输层在、但**没等到服务端的应答**。跟「没注入 transport」是两回事：
#: 集成方最容易在这里绕远路——看到「需要 set_transport 注入客户端」就回头
#: 去查注入代码，而传输层其实早就接好了，是服务端那边不吭声
#: （实测 `code_info --type EXTRA_ETF` 恒回 -82、无回包，就是这种）。
NO_RESPONSE = (
    "已注入传输层且已登录，但没等到服务端应答。"
    "这多半不是接入问题——先用官方客户端以同样的参数试一次，"
    "确认服务端对这个 method/参数是否真的给数据。"
    "若确实应该有数据，再看是不是被网关限流或会话已掉线。"
)


# --- 会话状态 ---------------------------------------------------------------


#: 资讯族翻到第几页还没完就判失败。1000 行一页，200 页是 20 万行——单只代码
#: 单个接口远到不了，到了多半是服务端在原地打转。`TGW_INFO_MAX_PAGES` 覆盖。
DEFAULT_INFO_MAX_PAGES = 200


class _Pager:
    """`ReqGetThirdInfo` 的 offset 翻页（U4）。

    请求里的 `count` 缺省 1000（原生库 `query_404280.c:269` 的缺省值），服务端
    单次最多回 `count` 行，「还有下一页」只由「本页行数 == count」隐式表达，
    没有续页标志（2026-10-11 网关调研 R1 探针实测：日历 offset
    0/1000/2000 三页 8738 行无缺口无重复）。厂商原生库只发一页——满 1000 行的
    结果被**静默截断**（沪深 300 成分全历史恒回 1000 行）。

    这里在替身层翻页：满页就把 offset 推进一页再发，直到短页；拼好后才交给
    业务层，业务层看到的仍是一次请求、一次回调。两种情况明确失败，不交截断的
    结果：后一页与前一页内容完全相同（服务端忽略了 offset），页数超上限。
    满页之后紧跟一页空结果（`status=-100`）算正常结束。
    """

    def __init__(self, count: int, offset: int) -> None:
        self.count = count
        self.offset = offset
        self.pages: list[list] = []
        self.seen: set[str] = set()
        self.max_pages = int(os.environ.get("TGW_INFO_MAX_PAGES") or DEFAULT_INFO_MAX_PAGES)

    def rows(self) -> list:
        return [row for page in self.pages for row in page]


class _Task:
    """一个在途查询：暂存的参数、认领它的回调、编码好的待发字节。

    `transform` 是回调前的数据整形钩子。传输层把线上的原始载荷
    （行情族是 CSV 字符串数组）交给 `deliver_response`，整形是业务侧
    的活，钩子让这一步发生在进回调之前，签名不用变。
    """

    __slots__ = ("task_id", "params", "callback", "return_df_format", "wire_bytes",
                 "transform", "explain_status", "pager")

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.params: list[tuple[str, str]] = []
        self.callback: Callable[[Any, Any], None] | None = None
        self.return_df_format = True
        self.wire_bytes: bytes | None = None
        self.transform: Callable[[Any], Any] | None = None
        self.explain_status: Callable[[str], str] | None = None
        #: 资讯族的翻页状态（`_Pager`）。行情族为 None。
        self.pager: _Pager | None = None


class Session:
    """全局会话：登录态、传输层、任务表。

    业务层在主线程批量发起查询、由接收线程回调，两条路径都读写 `_tasks`，
    所以任务表用锁保护。传输层与 SPI 引用在登录期固定，读多写少，
    单独用一把锁避免把热路径串行化。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[int, _Task] = {}
        self._transport: Any = None
        self._log_spi: Any = None
        self._push_spi: Any = None
        self.logon_event = threading.Event()
        self.logon_message = ""
        self.logged_in = False
        self.api_mode: ApiMode | None = None
        self.username = ""
        self.token = ""

    # -- 登录握手 -------------------------------------------------------------

    def begin_logon(self, api_mode, username: str) -> None:
        """`Login` 发出认证帧前调用，开一个干净的等待窗口。"""
        self.api_mode = api_mode
        self.username = username
        self.logged_in = False
        self.logon_message = ""
        self.logon_event.clear()

    def finish_logon(self, ok: bool, message: str = "", token: str = "") -> None:
        """传输层回报认证结果。先写状态再 set，唤醒方读到的一定是最终值。

        `token` 是网关在认证应答的 `headers.token` 里给的。**必须一路传进来**，
        后面 `encode_third_info_request` / `build_request` 拼信封时读的是
        `_SESSION.token`——没人填它的话，每条查询都会带空 token 出去，
        服务端回 `status=-87`（kDbAndCodeTableNoCode）。
        """
        self.logged_in = ok
        self.logon_message = message
        if ok and token:
            self.token = token
        self.logon_event.set()

    # -- 任务表 --------------------------------------------------------------

    def task(self, task_id: int) -> _Task:
        """取在途任务，没有就建。`SetThirdInfoParam` 逐次追加参数，
        必须复用同一个对象，否则后一次会抹掉前几次。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                t = _Task(task_id)
                self._tasks[task_id] = t
            return t

    def pop_task(self, task_id: int) -> _Task | None:
        with self._lock:
            return self._tasks.pop(task_id, None)

    def pending_task_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._tasks)

    # -- 传输层 / 回调 --------------------------------------------------------

    @property
    def transport(self):
        return self._transport

    def set_transport(self, transport) -> None:
        self._transport = transport

    @property
    def push_spi(self):
        return self._push_spi

    def set_push_spi(self, push_spi) -> None:
        self._push_spi = push_spi

    def set_log_spi(self, log_spi) -> None:
        self._log_spi = log_spi

    def log(self, level: int, message: str) -> None:
        """日志走 ILogSpi；没挂 SPI 就打到 stderr，不吞。"""
        spi = self._log_spi
        if spi is None:
            print(f"[tgw-compat] {message}", file=sys.stderr)
            return
        try:
            spi.OnLog(int(level), message, len(message))
        except Exception as exc:  # noqa: BLE001 - 日志失败不能连累主流程
            print(f"[tgw-compat] OnLog 抛异常，已忽略: {exc!r}", file=sys.stderr)

    def reset(self) -> None:
        with self._lock:
            self._tasks.clear()
        self._transport = None
        self._log_spi = None
        self._push_spi = None
        self.logged_in = False
        self.logon_message = ""
        self.logon_event.clear()
        self.api_mode = None
        self.username = ""
        self.token = ""


_SESSION = Session()


def session() -> Session:
    return _SESSION


# --- task_id ----------------------------------------------------------------
# 逐字翻译 `galaxy::tgw::TgwImpl::GetTaskID` @ 0x2123a0（逆向记录事实 5）。
# (mon+1) 落在 1e11 位而 (year+1900) 落在 1e10 位，年份位比月份位低——
# 原实现就是这样，不是抄错。

_TASK_ID_LOCK = threading.Lock()
_last_task_id = 0
_task_id_dedup = 0


def GetTaskID() -> int:  # noqa: N802 - 与厂商 API 同名同风格
    """生成查询任务号。复刻 `TgwImpl::GetTaskID` @ 0x2123a0（逆向记录事实 5）。

    逆向记录给的伪码末句是 `if (id == last) lock add [counter], 1`，
    自增之后如何参与最终 id 的赋值没写全（未验证）。这里取「同秒重复调用
    必须拿到不同任务号」这个唯一能确定的约束，实现成严格递增：

        id = v * 1e6 + 1
        if id <= 上次发出的值:  id = 上次发出的值 + 1

    只按逆向记录字面「等于上次才自增」会在 base 和 base+k 之间来回震荡
    （第 3 次调用又回到 base），任务号重复会让响应认错领——服务端按
    `headers.id` 派发，两次同号查询的响应会串。顺带也覆盖了时间基值
    跨过 `%= 1e10` 回绕后倒退的情况。
    """
    global _last_task_id, _task_id_dedup

    tm = time.localtime()
    v = (
        tm.tm_sec * 1000
        + tm.tm_min * 100_000
        + tm.tm_hour * 10_000_000
        + tm.tm_mday * 1_000_000_000
        + (tm.tm_mon + 1) * 100_000_000_000
        + (tm.tm_year + 1900) * 10_000_000_000
    )
    v >>= 3
    v %= 10_000_000_000

    with _TASK_ID_LOCK:
        task_id = v * 1_000_000 + 1
        if task_id <= _last_task_id:
            # 原生实现是 lock 保护的原子自增，这里用同一把锁串行化
            _task_id_dedup += 1
            task_id = _last_task_id + _task_id_dedup
        _last_task_id = task_id
    return task_id


# --- 版本与错误码 -----------------------------------------------------------


def GetVersion() -> str:  # noqa: N802
    return f"tgwpy-compat (原生 tgw 1.0.9.2 接口子集, 枚举来源: {VALUES_SOURCE})"


_ERROR_MSG = {
    ErrorCode.kSuccess: "成功",
    ErrorCode.kFailure: "失败",
    ErrorCode.kUnInited: "未初始化",
    ErrorCode.kNullSpi: "空指针",
    ErrorCode.kParamIllegal: "参数非法",
    ErrorCode.kNetError: "网络异常",
    ErrorCode.kPermissionError: "数据无权限",
    ErrorCode.kLogonFailed: "未登录",
    ErrorCode.kAllocateMemoryFailed: "分配内存失败",
    ErrorCode.kChannelError: "通道错误",
    ErrorCode.kOverLoad: "查询服务端hqs任务队列溢出",
    ErrorCode.kLogoned: "账号已登录",
    ErrorCode.kHqsError: "查询服务端HQS系统错误",
    ErrorCode.kDqsError: "查询服务端DQS系统错误",
    ErrorCode.kNonQueryTimePeriod: "非查询时间段(非查询时间段不支持查询)",
    ErrorCode.kDbAndCodeTableNoCode: "数据库和代码表中没有指定的代码",
    ErrorCode.kIllegalMode: "api模式非法",
    ErrorCode.kThreadBusy: "超过最大可用线程资源",
    ErrorCode.kParseDataError: "数据解析出错",
    ErrorCode.kTimeout: "获取数据超时",
    ErrorCode.kFlowOverLimit: "周流量耗尽",
    ErrorCode.kCodeTableCacheNotAvailable: "代码表缓存不可用",
    ErrorCode.kOverMaxSubLimit: "超过最大订阅限制",
    ErrorCode.kLostConnection: "丢失连接",
    ErrorCode.kOverMaxQueryLimit: "超过最大查询数（含代码表）",
    ErrorCode.kFunctionIdNull: "三方资讯查询未设置功能号",
    ErrorCode.kDataEmpty: "数据为空",
    ErrorCode.kUserNotExist: "用户不存在",
    ErrorCode.kVerifyFailure: "账号/密码错误",
    ErrorCode.kApiInterfaceUsing: "api接口不能同时多次调用",
    ErrorCode.kTaskIdRepeat: "任务id重复",
}


def GetErrorMsg(error_code) -> str:  # noqa: N802
    """错误码转中文文案。抄自 `tgw/error_code.py`，覆盖全部 31 个码。"""
    try:
        return _ERROR_MSG[error_code]
    except (KeyError, TypeError):
        return "unknown error code"


def SetLogSpi(log_spi) -> None:  # noqa: N802
    _SESSION.set_log_spi(log_spi)


# --- 安装 / 卸载 ------------------------------------------------------------
# 厂商的 tgw 是包，`__init__.py` 依次 star-import 了 interface / tmp_spi /
# error_code / base_struct / server_spi。替身照同样的形状摆子模块，
# 免得谁写了 `from tgw.base_struct import Kline` 就踩空。

_SUBMODULES = ("base_struct", "interface", "tmp_spi", "error_code", "server_spi")


def install() -> None:
    """把替身装进 `sys.modules["tgw"]`。已有真 tgw 时不动它。"""
    if "tgw" in sys.modules:
        return
    me = sys.modules[__name__]
    pkg = types.ModuleType("tgw")
    pkg.__doc__ = __doc__
    pkg.__path__ = []  # type: ignore[attr-defined]  # 标成包，允许 tgw.xxx 导入
    pkg.__tgwpy_compat__ = True
    for name in __all__:
        setattr(pkg, name, globals()[name])
    pkg.enums = _make_submodule("enums", _enums_module())
    pkg.base_struct = _make_submodule("base_struct", structs, structs.__all__)
    pkg.interface = _make_submodule("interface", me)
    pkg.error_code = _make_submodule("error_code", me, ("GetErrorMsg", "ErrorCode"))
    pkg.server_spi = _make_submodule(
        "server_spi", me, ("ILogSpi", "IPushSpi", "IGMDSpi")
    )
    pkg.tmp_spi = _make_submodule("tmp_spi", me, ("IGMDSpi",))
    sys.modules["tgw"] = pkg
    for sub in ("enums", *_SUBMODULES):
        sys.modules[f"tgw.{sub}"] = getattr(pkg, sub)


def uninstall() -> None:
    """从 `sys.modules` 摘掉替身。已 import 的业务层模块不受影响，
    它们早就把 `tgw` 的名字绑到自己模块的全局里去了。"""
    for key in [k for k in sys.modules if k == "tgw" or k.startswith("tgw.")]:
        del sys.modules[key]


def is_installed() -> bool:
    return getattr(sys.modules.get("tgw"), "__tgwpy_compat__", False) is True


def _make_submodule(name: str, source: types.ModuleType, names=()) -> types.ModuleType:
    mod = types.ModuleType(f"tgw.{name}")
    mod.__package__ = "tgw"
    for attr in names or getattr(source, "__all__", ()):
        if hasattr(source, attr):
            setattr(mod, attr, getattr(source, attr))
    return mod


def _enums_module() -> types.ModuleType:
    from tgwpy.compat import _enums_stub

    mod = types.ModuleType("tgw.enums")
    for name in _enums_stub.__all__:
        setattr(mod, name, getattr(_enums_stub, name))
    mod.MINED_VALUES_VERIFIED = MINED_VALUES_VERIFIED
    mod.VALUES_SOURCE = VALUES_SOURCE
    return mod


# --- 登录 -------------------------------------------------------------------

#: `Login` 发出认证帧后等多久要认证结果。超时按失败处理——业务层
#: `tgw_login.login` 拿到 False 会 `print('login fail')` 后 `exit(0)`，
#: 干等不如明确失败。
LOGON_TIMEOUT = 10.0


def _auth_frames(config: Cfg, api_mode) -> tuple[bytes, ...]:
    """拼出登录要发的帧。

    **互联网模式（`kInternetMode`）走 JSON 信封**，不是 AMA 的二进制帧。
    这一点踩过坑：`frames.ama.build_auth_frame` 产出的是 8536 字节的
    `AMAClientImpl` 认证帧，那是**推送侧**的路径；公网 WSS 网关对它
    静默丢弃（实测 72 组参数组合全部无回包）。公网正确的登录报文是
    `SendLogonRequest` 构造的 `ReqLogon` JSON。

    托管模式（`kColocationMode`）才走专线 TCP，用 AMA 帧。
    """
    if api_mode == ApiMode.kColocationMode:
        from tgwpy.frames.ama import build_auth_frame

        frames = build_auth_frame(
            config.username, config.password,
            channel_mode=getattr(config, "coloca_cfg", None)
            and config.coloca_cfg.channel_mode or 0,
        )
        return frames if isinstance(frames, tuple) else (frames,)

    from tgwpy.client import logon_request

    return (json.dumps(
        logon_request(config.username, config.password),
        separators=(",", ":"),
    ).encode(),)


def Login(config: Cfg, api_mode, path: str = "") -> bool:  # noqa: N802
    """登录。签名与 `tgw/interface.py::Login` 一致。

    流程：拼认证帧 -> 交给传输层发出去 -> 等传输层回报认证结果
    （`deliver_logon`）。没接传输层、拼帧失败、发送异常、等超时，
    一律返回 False 并把原因写进日志 SPI。**不抛异常也不返回 True**——
    业务层拿到 False 会 `print('login fail')` 后 `exit(0)`；
    让它误判成功比让它失败危险得多。
    """
    for field in ("server_vip", "server_port", "username", "password"):
        if not getattr(config, field, None):
            _SESSION.log(LogLevel.kError, f"Cfg.{field} 为空，登录参数不完整")
            return False
    if api_mode is None:
        _SESSION.log(LogLevel.kError, "api_mode 为空，登录参数不完整")
        return False
    if _SESSION.transport is None:
        _SESSION.log(LogLevel.kError, f"登录失败：{NOT_LOGGED_IN}")
        return False

    try:
        frames = _auth_frames(config, api_mode)
    except Exception as exc:  # noqa: BLE001 - 拼帧失败是配置问题，报清楚
        _SESSION.log(
            LogLevel.kError, f"登录失败：认证帧构造失败 {type(exc).__name__}: {exc}"
        )
        return False

    _SESSION.begin_logon(api_mode, config.username)
    try:
        for frame in frames:
            _SESSION.transport.send(frame)
    except Exception as exc:  # noqa: BLE001
        _SESSION.log(
            LogLevel.kError, f"登录失败：认证帧发送失败 {type(exc).__name__}: {exc}"
        )
        return False

    if not _SESSION.logon_event.wait(LOGON_TIMEOUT):
        _SESSION.log(LogLevel.kError, f"登录失败：{LOGON_TIMEOUT}s 内没等到认证结果")
        return False
    if not _SESSION.logged_in:
        _SESSION.log(
            LogLevel.kError, f"登录失败：{_SESSION.logon_message or '服务端拒绝'}"
        )
    return _SESSION.logged_in


def deliver_logon(ok: bool, message: str = "", token: str = "") -> None:
    """传输层收到认证结果后调它，唤醒 `Login` 里等待的那一方。

    `token` 是可选的——传了就写进会话，不传则沿用会话里已有的值。
    签名保持向后兼容，老的传输层实现只传前两个参数也能用。
    """
    _SESSION.finish_logon(bool(ok), message, token)


def Close() -> None:  # noqa: N802
    """断开当前会话，但**保留传输层**。

    业务层的 `set_cfg()` 开头就调 `Close()`（`login()` / `logout()` 也会），
    如果这里把 `_SESSION.transport` 一起置空，后面 `Login()` 的前置检查
    「transport is None」就会失败，业务层只看到 `login fail`，看不到原因。

    分层：会话（token / task 表 / 登录态）是 per-login 的，传输层
    （怎么连网关）是常驻的。拆掉后者就等于把「怎么连」也一起丢了。
    `WssTransport.close()` 只标记关闭，下次 `send()` 会自动重连。
    """
    transport = _SESSION.transport
    _SESSION.reset()
    if transport is not None:
        _SESSION.set_transport(transport)
        close = getattr(transport, "close", None)
        if callable(close):
            close()


def Logout() -> None:  # noqa: N802
    """登出。厂商侧登出就是断链，等价于 `Close()`。"""
    Close()


# --- 传输层对接 --------------------------------------------------------------


def set_transport(transport) -> None:
    """注入传输层。对象需提供 `send(bytes)`；登录/订阅还需要
    `login(cfg, api_mode, path)`，可选 `close()`。收到响应后它负责调
    `deliver_response(task_id, data, status)`。"""
    _SESSION.set_transport(transport)


def deliver_response(task_id: int, data, status=None) -> None:
    """传输层收到响应后调它认领任务并回调业务层。

    `data` 的形状由该查询自己定：资讯族是已解析的 dict（业务层按名字
    取键），行情族是线上原始的 CSV 字符串数组，任务的 `transform`
    钩子先把它整成业务层的 DataFrame 再回调。`status` 语义见模块文档。
    任务表里没有这个 task_id，说明是迟到或重复的包，只记日志不碰业务层。
    """
    task = _SESSION.pop_task(int(task_id))
    if task is None:
        _SESSION.log(
            LogLevel.kWarn, f"收到 task_id={task_id} 的响应，任务表里没有它，丢弃"
        )
        return
    if task.pager is not None:
        data, status, more = _next_page(task, data, status)
        if more:
            return
    if task.transform is not None and data is not None:
        # `data is None` 一律是失败信号（`_reject` 走的就是这条路），
        # 别拿整形器去处理它——那会把「失败原因」盖成「整形异常」。
        try:
            data = task.transform(data)
        except Exception as exc:  # noqa: BLE001 - 整形失败仍是可见失败，不是崩溃
            msg = f"task_id={task_id} 的响应整形失败: {type(exc).__name__}: {exc}"
            _SESSION.log(LogLevel.kError, msg)
            data, status = None, msg
    if task.explain_status is not None and isinstance(status, str):
        # 服务端错误码的解释钩子。放在这里而不是各入口的失败分支：
        # 异步形态的失败是传输层直接 `deliver_response(task_id, None, msg)`，
        # 不经过查询入口的代码，钩子挂在入口上就漏掉了。
        status = task.explain_status(status)
    _invoke(task, data, status)


def _is_empty_status(status) -> bool:
    text = str(status)
    return "-100" in text or "DataEmpty" in text


def _next_page(task: _Task, data, status) -> tuple[Any, Any, bool]:
    """处理一页资讯回包。返回 `(交给业务层的 data, status, 是否已发下一页)`。"""
    import hashlib
    import json as _json

    pager = task.pager
    assert pager is not None
    if status:
        if pager.pages and _is_empty_status(status):
            return pager.rows(), None, False      # 满页之后是空页：正好取完
        if pager.pages:
            status = f"{status}（第 {len(pager.pages) + 1} 页失败，前 {len(pager.pages)} 页作废）"
        return None, status, False
    page = list(data) if isinstance(data, (list, tuple)) else data
    if not isinstance(page, list):
        return data, status, False
    fingerprint = hashlib.sha256(
        _json.dumps(page, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    if fingerprint in pager.seen:
        return None, (f"服务端忽略了 offset：第 {len(pager.pages) + 1} 页与之前某页完全相同，"
                      f"无法取全（已取 {len(pager.rows())} 行），不交出截断的结果"), False
    pager.seen.add(fingerprint)
    pager.pages.append(page)
    if len(page) < pager.count:
        return pager.rows(), status, False
    if len(pager.pages) >= pager.max_pages:
        return None, (f"资讯翻页到 {pager.max_pages} 页（{len(pager.rows())} 行）仍未取完，"
                      "判定失败；缩小日期区间或调大 TGW_INFO_MAX_PAGES"), False

    pager.offset += pager.count
    task.params = [(k, v) for k, v in task.params if k != "offset"] + [("offset", str(pager.offset))]
    if (err := _encode(task)) is not None:
        return None, f"翻页请求编码失败：{err}", False
    with _SESSION._lock:                                   # noqa: SLF001
        _SESSION._tasks[task.task_id] = task               # noqa: SLF001
    transport = _SESSION.transport
    if transport is None:
        _SESSION.pop_task(task.task_id)
        return None, f"翻页时 {NO_TRANSPORT}", False

    def _send() -> None:
        # 回包是在传输层的接收线程上回调的；`send` 会在节流闸门上睡，
        # 不能在那条线程上发。
        try:
            transport.send(task.wire_bytes)
        except Exception as exc:  # noqa: BLE001 - 发送失败必须变成可见的失败
            deliver_response(task.task_id, None, f"翻页发送失败: {type(exc).__name__}: {exc}")

    threading.Thread(target=_send, name=f"tgw-page-{task.task_id}", daemon=True).start()
    return None, None, True


def pending_task_ids() -> tuple[int, ...]:
    """当前在途的 task_id，供诊断用。"""
    return _SESSION.pending_task_ids()


def _invoke(task: _Task, data, status) -> None:
    """调业务层回调。回调抛异常只记日志——不能让接收线程陪葬。"""
    if task.callback is None:
        return
    try:
        task.callback(data, status)
    except Exception as exc:  # noqa: BLE001
        _SESSION.log(
            LogLevel.kError, f"task_id={task.task_id} 的回调抛异常: {exc!r}"
        )


def _reject(reason: str, query_spi, task_id: int | None = None) -> tuple[Any, str]:
    """查询失败的统一出口，`reason` 写清到底卡在哪一步。

    * 异步形态（`query_spi` 非空）：**必须回调**，否则业务层卡在
      `wait_event.wait()` 上永远不醒。失败用字符串状态，业务层的
      `isinstance(status, str)` 分支会把它收进 `error_list`。
    * 同步形态：抛 `NotConnected`。见模块文档「两条硬规则」第 2 条。

    `task_id` 非空表示这个查询已在任务表里登记过，先出表再回调，
    否则任务会一直挂在表里等一个永远不来的响应。
    """
    msg = f"{reason}；{_TRANSPORT_HINT}"
    _SESSION.log(LogLevel.kError, msg)
    if task_id is not None:
        deliver_response(task_id, None, msg)
        return False, msg
    if query_spi is None:
        raise NotConnected(msg)
    try:
        query_spi(None, msg)
    except Exception as exc:  # noqa: BLE001
        _SESSION.log(LogLevel.kError, f"回调抛异常: {exc!r}")
    return False, msg


def _unimplemented(name: str, request, query_spi) -> tuple[Any, str]:
    """协议层尚未实现该查询接口时的出口。"""
    detail = f"{type(request).__name__}" if request is not None else "无参数"
    return _reject(f"tgwpy 替身未实现 {name}（{detail}）", query_spi)


# --- 三方资讯（53 个下载方法的主通道）----------------------------------------


def SetThirdInfoParam(task_id, key, value):  # noqa: N802
    """暂存一个下载参数。复刻 `QueryRequestCache::SetThirdInfoParam` @ 0x3f7bc0。

    按调用顺序存，顺序会原样进 `params.item` 数组。同一个 task_id 重复调用
    是追加而非覆盖——业务层一次下载要连打 0..n 个参数。
    """
    _SESSION.task(int(task_id)).params.append((str(key), str(value)))
    return ErrorCode.kSuccess


def _page_params(params: list[tuple[str, str]]) -> tuple[int, int]:
    """业务层显式设了 count / offset 就照它的，否则取原生缺省（1000 / 0）。"""
    from tgwpy.frames.query import DEFAULT_COUNT

    found = dict(params)
    try:
        count = int(found.get("count", DEFAULT_COUNT))
        offset = int(found.get("offset", 0))
    except ValueError:
        count, offset = DEFAULT_COUNT, 0
    return max(count, 1), offset


def _encode(task: _Task) -> str | None:
    """把暂存参数编码成待发字节。返回错误消息，None 表示成功。

    `tgwpy.frames` 由协议 agent 维护，可能还没落地或正在改。导入放进
    try 里，这样它缺席或报错只会让本次查询失败并回调业务层，而不会
    抛到 `QueryThirdInfo` 外面去——那会让业务层停在 `wait_event.wait()`
    上永远不醒。"""
    try:
        from tgwpy.frames.query import encode_third_info_request

        task.wire_bytes = encode_third_info_request(
            task.task_id, task.params,
            username=_SESSION.username, token=_SESSION.token,
        )
    except Exception as exc:  # noqa: BLE001 - 缺 function_id 等在这里暴露
        return f"请求编码失败: {type(exc).__name__}: {exc}"
    return None


def QueryThirdInfo(task_id, query_spi=None, return_df_format: bool = True):  # noqa: N802
    """三方资讯查询，53 个下载方法全部收敛到这里（逆向记录事实 6）。

    `query_spi` 是**可调用对象**（业务层传 `spi.OnResponse` 绑定方法），
    不是 SPI 实例。给了就异步回调，没给就同步等结果。

    **两条路径都在回调前把记录整成 DataFrame**，整形挂在任务的
    `transform` 钩子上（`deliver_response` 统一调），而不是各自分支里各写一遍。
    少了这一步，SPI 存进 `EnvXxx.data[code]` 的就是原始 list，
    业务层紧接着的 `.empty` / `.astype` 直接炸——
    实测 `get_history_stock_status` 报 `'list' object has no attribute 'empty'`。

    证据：`info_spi.dis.txt` 的 `HistStockStatusSpi.OnResponse`（line 311）
    是 `EnvHistStockStatus.data[self._code] = data`，**原样存不转换**，
    所以 DataFrame 必须在进 SPI 之前就做好；
    `download_info_data.dis.txt:1872`（源码行 1623）读的却是 `.empty`。
    """
    task = _SESSION.task(int(task_id))
    task.return_df_format = return_df_format
    task.transform = lambda records: _to_frame(records, None, return_df_format)
    task.pager = _Pager(*_page_params(task.params))

    # 同步形态：`query_spi` 为空时业务层要**当场拿到结果**。
    # 发完就 `return True, None` 会让调用方拿到 bool，
    # 紧接着 `df["TRADE_DAYS"]` 就炸 `TypeError: 'bool' object is not subscriptable`。
    # 官方 `QueryKline`（`tgw/interface.py:208-219`）也是这个形状：自造 WaitSpi、
    # 同步等、按 `return_df_format` 出 DataFrame。本函数 docstring 写的就是
    # 「没给就同步等结果」，实现先前与文档不一致。
    waiter = None if query_spi is not None else _Waiter()
    task.callback = waiter if waiter is not None else query_spi

    if not task.params:
        return _reject(
            f"QueryThirdInfo 未设置任何参数（{GetErrorMsg(ErrorCode.kFunctionIdNull)}）",
            query_spi, task.task_id,
        )
    if not _SESSION.logged_in:
        return _reject(f"QueryThirdInfo {NOT_LOGGED_IN}", query_spi, task.task_id)
    if (err := _encode(task)) is not None:
        return _reject(f"QueryThirdInfo {err}", query_spi, task.task_id)
    if _SESSION.transport is None:
        return _reject(f"QueryThirdInfo {NO_TRANSPORT}", query_spi, task.task_id)

    try:
        _SESSION.transport.send(task.wire_bytes)
    except Exception as exc:  # noqa: BLE001 - 发送失败必须变成可见的失败
        return _reject(
            f"QueryThirdInfo 发送失败: {type(exc).__name__}: {exc}",
            query_spi, task.task_id,
        )

    if waiter is None:
        return True, None
    data, status = waiter.wait(MARKET_TIMEOUT)
    if status:
        return None, status
    # `data` 已经过了 `transform`，形状就是 `return_df_format` 要的那个。
    return data, None


# --- 行情 / 代码表查询 -------------------------------------------------------

#: 同步形态等一个 task_id 的响应要多久。到点按失败处理——业务层那句
#: `wait_event.wait()` 不带超时，替身替它兜住。
#: 取值照 `LOGON_TIMEOUT` 的量级定，**未验证**：真实网关的响应时延没量过。
MARKET_TIMEOUT = 60.0

#: `req_kline_cfg.cyc_type`（业务层 `Period` 的值，恒等于 `tgw.MDDatatype`）
#: → JSON 线上协议的 `period_type`。**两套编码**，别把前者填进后者。
#:
#: 证据链：`market_data.py:202` 存的是 `Period` 成员；
#: `utils/constant.py:27-40` 把 `Period` 的每个成员直接绑定到
#: `tgw.MDDatatype.kXxxKline`；抓包实证里 `Period.day` 对应的
#: `period_type` 是 10100。
#: 13 个值逐个列出，不靠「+100」这种算术外推。
#:
#: 分钟线那一段两边数值恰好相同（`k1KLine` 与 `min1` 都是 10000），
#: 日线及以上差 100（`kDayKline`=10008 → `day`=10100）。**别把这张表
#: 当恒等映射删掉。**
_CYCTYPE_TO_PERIOD_TYPE: dict[int, int] | None = None


def _period_table() -> dict[int, int]:
    """周期码表惰性建一次。`tgwpy.frames` 由协议层维护，导入失败不该
    拖垮整个替身的导入，所以放到第一次查询时再建。"""
    global _CYCTYPE_TO_PERIOD_TYPE
    if _CYCTYPE_TO_PERIOD_TYPE is not None:
        return _CYCTYPE_TO_PERIOD_TYPE
    from tgwpy.frames.market import PERIOD_TYPE

    _CYCTYPE_TO_PERIOD_TYPE = {
        int(cyc): PERIOD_TYPE[name] for cyc, name in (
            (MDDatatype.k1KLine, "min1"), (MDDatatype.k3KLine, "min3"),
            (MDDatatype.k5KLine, "min5"), (MDDatatype.k10KLine, "min10"),
            (MDDatatype.k15KLine, "min15"), (MDDatatype.k30KLine, "min30"),
            (MDDatatype.k60KLine, "min60"), (MDDatatype.k120KLine, "min120"),
            (MDDatatype.kDayKline, "day"), (MDDatatype.kWeekKline, "week"),
            (MDDatatype.kMonthKline, "month"),
            (MDDatatype.kSeasonKline, "season"), (MDDatatype.kYearKline, "year"),
        )
    }
    return _CYCTYPE_TO_PERIOD_TYPE


class _Waiter:
    """同步形态的收集器，形状照官方 tgw 的 `TmpQueryKlineWaitSpi`。

    官方 `QueryKline`（`tgw/interface.py:208-219`）在 `query_spi` 为空时
    自己造一个 WaitSpi，注册进中间层 spi，等 `wait_event` 落下再
    `GetResult()` 拿 `(result, err)`。替身不需要两层，一个够用的收集器。
    """

    __slots__ = ("_event", "data", "status")

    def __init__(self) -> None:
        self._event = threading.Event()
        self.data: Any = None
        self.status: Any = None

    def __call__(self, data, status) -> None:
        self.data, self.status = data, status
        self._event.set()

    def wait(self, timeout: float) -> tuple[Any, Any]:
        if not self._event.wait(timeout):
            # 超时的第一嫌疑是「服务端根本不给数据」，不是「传输层没接好」
            # ——后者早就该在 send 之前就报 NOT_LOGGED_IN / NO_TRANSPORT 了。
            return None, f"{timeout}s 内没等到响应。{NO_RESPONSE}"
        return self.data, self.status


def _run_market_query(name: str, task_id: int, build_wire, build_result,
                      query_spi, return_df_format: bool, *,
                      send_method: str = "send_market",
                      sync_raises: bool = False,
                      explain_status: Callable[[str], str] | None = None
                      ) -> tuple[Any, Any]:
    """查询的共用骨架：登记任务 → 造字节 → 发 → 等结果。

    每一步失败都走 `_reject`，绝不把异常抛给业务层——异步形态下那会让
    `market_data.py` 的 `wait_event.wait()` 永远醒不过来（模块文档
    「两条硬规则」第 1 条）。

    `build_wire(task_id) -> bytes` 与 `build_result(rows)` 由各入口自行
    传入：前者把业务层的配置结构体转成线上报文，后者把线上响应转成
    业务层的 DataFrame。两处都在 try 里，构造失败同样是可见失败。

    三个开关对应两类业务层的失败契约，行情族全用默认值：

    * `send_method` —— 走哪条通道。行情族 `send_market`（`/amd/dgw/dgw`），
      代码表族 `send`（`/amd/dgw/` 资讯通道）。抓包实证：
抓包实证。
    * `sync_raises` —— 同步形态的失败是抛还是回 `(None, 文案)`。只有
      「拿到返回值就丢掉 error、紧接着下标」的业务层需要抛，见 `fail`。
    * `explain_status` —— 服务端拒绝的文案整形钩子，各入口自己带证据。
    """
    task = _SESSION.task(task_id)
    task.return_df_format = return_df_format
    waiter = _Waiter() if query_spi is None else None
    task.callback = waiter if waiter is not None else query_spi

    def fail(reason: str) -> tuple[Any, Any]:
        """失败出口。异步形态回 `(False, 消息)`，同步形态按 `sync_raises` 分岔。

        默认（`sync_raises=False`）下同步形态回 `(None, 消息)`，照官方
        `tgw/interface.py:214-219` 的契约——业务层靠 `result is None` 判失败，
        回 `False` 判不出来。

        `sync_raises=True` 用于「丢掉 error 直接下标」的业务层
        （`base_data.py` 的代码表查询）：回 `(None, 消息)` 会在几十行之外
        炸成 `TypeError: 'NoneType' object is not subscriptable`，看不出是哪个
        接口失败。见模块文档「两条硬规则」第 2 条。**只对同步形态生效**——
        异步形态抛出去会让 `wait_event.wait()` 永远醒不过来。
        """
        if waiter is not None and sync_raises:
            # 先出表，否则任务挂着等一个永远不来的响应
            _SESSION.pop_task(task_id)
            _reject(reason, None, None)      # query_spi 为空 → 抛 NotConnected
        rc, err = _reject(reason, query_spi, task_id)
        return (None if waiter is not None else rc), err

    try:
        task.transform = lambda rows: build_result(rows)
    except Exception as exc:  # noqa: BLE001 - pragma: no cover - 闭包不会失败
        return fail(f"{name} 整形器构造失败: {exc!r}")

    # 服务端错误码的解释钩子挂在任务上，由 `deliver_response` 统一施加，
    # 这样异步形态（失败由传输层直接投递）也拿得到同一份文案。
    task.explain_status = explain_status

    if not _SESSION.logged_in:
        return fail(f"{name} {NOT_LOGGED_IN}")
    if (transport := _SESSION.transport) is None:
        return fail(f"{name} {NO_TRANSPORT}")

    try:
        wire = build_wire(task_id)
    except Exception as exc:  # noqa: BLE001 - 构造失败也要走可见失败
        return fail(f"{name} 请求构造失败: {type(exc).__name__}: {exc}")
    task.wire_bytes = wire

    try:
        getattr(transport, send_method)(wire)
    except Exception as exc:  # noqa: BLE001 - 发送失败必须变成可见的失败
        return fail(f"{name} 发送失败: {type(exc).__name__}: {exc}")

    if waiter is None:
        return True, None
    data, status = waiter.wait(MARKET_TIMEOUT)
    if data is None:
        # data 为空一定是失败：整形失败、任务取消，或等超时。
        # status 可能是字符串（业务层的记错分支，已经过 explain_status 整形），
        # 也可能是 None（传输层只回了状态没回数据），两种都要给看得懂的文案。
        return fail(status if isinstance(status, str) else f"{name} 没有返回数据")
    return data, None


# --- 行情响应 → 业务层 DataFrame --------------------------------------------
#
# **业务层吃的是 tgw 原生语义，不是线上 CSV 的可读视图。**
#
# `convert_history_kline`（`utils/convert.py:769-784`）拿到替身给的
# DataFrame 后自己再除一遍：`open_price.div(1000000)`、
# `volume_trade.div(100)`、`value_trade.div(100000)`。
# `convert_history_tick_stock`（`640-664`）同样如此，并且
# `pd.to_datetime(snapshot_df['orig_time'].astype(str), format='%Y%m%d%H%M%S%f')`
# 要求 `orig_time` 是 **17 位**串——`%f` 吃末 3 位毫秒。
#
# 所以 CSV 里的 `1403000000` 不能以 `1403.0` 的形态交出去，再被除一次
# 就成了 1.4e-6。下面把 `frames/market` 解出的可读值**乘回**除数，还原
# 成 tgw 交给业务层的整数。缩放系数一律取自 `frames/market` 自己的
# 常量表，不在这里二次硬编码。


def _unscale(value: Any, divisor: float) -> Any:
    """把 `frames/market` 解出的可读值还原成线上整数。"""
    if not divisor or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    return int(round(value * divisor))


def _to_frame(records: list, columns, return_df_format: bool):
    """按 `return_df_format` 出 DataFrame 或等价于 `json_normalize` 的记录列表。

    官方中间层（`tgw/tmp_spi.py:539-544`）的分岔就在这里：`True` →
    `json_normalize(json_list)`，`False` → 原始 `json_list`。

    `columns` 非空时列序照它显式钉住——`pd.DataFrame(记录字典)` 靠键序，
    多半对，但不必赌。**`columns=None` 表示列集由服务端给**：代码表查询
    在当前账号上恒回 `-82`，返回列从未实测到，钉死一列集就是编数据。
    """
    import pandas as pd

    if not return_df_format:
        return list(records)
    if columns is None:
        return pd.json_normalize(records)
    return pd.DataFrame(records, columns=list(columns))


#: K 线 DataFrame 的列。前 7 个是 `convert_history_kline` 逐个读的名字，
#: 末列是 CSV 自带的 `security_code`。
#:
#: 刻意**不给** `frames/market.KLINE_FIELDS` 那 11 项里的
#: `orig_time` / `variety_category`：线上 K 线 CSV 只有 9 段，这两个字段
#: 没有来源，补 0 是编数据。业务层不读它们；少给不影响 `pd.concat`
#: ——各分片列一致即可。
_KLINE_COLUMNS: tuple[str, ...] = (
    "kline_time", "open_price", "high_price", "low_price", "close_price",
    "volume_trade", "value_trade", "security_code",
)

#: 快照单值列：（tgw 原生列名，`frames/market` 解出后的键）。
#: 名字全部来自 `convert_history_tick_stock` 640-664 逐列读的表达式。
_SNAPSHOT_SCALARS: tuple[tuple[str, str], ...] = (
    ("pre_close_price", "pre_close"),
    ("open_price", "open"),
    ("high_price", "high"),
    ("low_price", "low"),
    ("close_price", "close"),
    ("last_price", "last"),
    ("high_limited", "high_limited"),
    ("low_limited", "low_limited"),
    ("IOPV", "iopv"),
    ("num_trades", "num_trades"),
    ("total_volume_trade", "volume"),
    ("total_value_trade", "amount"),
    ("trading_phase_code", "trading_phase_code"),
)

#: 档位前缀是 `offer_` 不是 `ask_`：`convert_history_tick_stock` 第 647-650
#: 读的是 `offer_price{i}` / `offer_volume{i}`，而线上 CSV 段 12/13 叫 ask。
#: 这里跟业务层走。
_SNAPSHOT_SIDES: tuple[tuple[str, str, str], ...] = (
    ("bid", "bid_prices", "bid_volumes"),
    ("offer", "ask_prices", "ask_volumes"),
)
#: `convert_history_tick_stock` 第 646 行是 `range(1, 7)`，读 1..6 档。
_SNAPSHOT_BOOK_LEVELS = 6


def _snapshot_columns() -> tuple[str, ...]:
    cols = ["orig_time", "security_code"]
    cols += [name for name, _ in _SNAPSHOT_SCALARS]
    for side, _, _ in _SNAPSHOT_SIDES:
        cols += [f"{side}_price{i + 1}" for i in range(_SNAPSHOT_BOOK_LEVELS)]
        cols += [f"{side}_volume{i + 1}" for i in range(_SNAPSHOT_BOOK_LEVELS)]
    return tuple(cols)


_SNAPSHOT_COLUMNS: tuple[str, ...] = _snapshot_columns()


def _kline_time_int(value: Any) -> int:
    """K 线时间 → 业务层要的整数。

    **要的是线上原样的数字位数，不是一个好看的日期串。**
    业务层按周期选解析格式，再对 `astype(str)` 的结果解析：

        kline_time_format = '%Y%m%d%H%M'
        if period in (day, week, month, season, year):
            kline_time_format = '%Y%m%d'
        kline_convert_df['kline_time'] = pd.to_datetime(
            kline_df['kline_time'].astype(str), format=kline_time_format)

    ——`recovered/AmazingData/utils/convert.dis.txt` 源码行 770-772 / 775。

    所以分钟线必须是 **12 位** `202609290930`，日线是 **8 位** `20260420`，
    正好是线上 CSV 的原始位数（`frames/market.py::_parse_kline_time` 的位长表）。
    中间插分隔符会把 `202609290930` 变成 `20260929 09:30`，`%Y%m%d%H%M` 匹配不上，
    实测报 `ValueError: invalid literal for int()`。
    """
    if value is None:
        return 0
    return int("".join(ch for ch in str(value) if ch.isdigit()) or 0)


def _kline_records(rows: list) -> list[dict]:
    """线上 K 线 CSV → 业务层 DataFrame 的记录列表。"""
    from tgwpy.frames.market import (
        KLINE_CSV_DIVISOR, KLINE_CSV_FIELDS, decode_kline_rows,
    )

    div = dict(zip(KLINE_CSV_FIELDS, KLINE_CSV_DIVISOR))
    out = []
    for rec in decode_kline_rows(rows):
        out.append({
            "kline_time": _kline_time_int(rec.get("kline_time")),
            "open_price": _unscale(rec.get("open"), div["open"]),
            "high_price": _unscale(rec.get("high"), div["high"]),
            "low_price": _unscale(rec.get("low"), div["low"]),
            "close_price": _unscale(rec.get("close"), div["close"]),
            "volume_trade": _unscale(rec.get("volume"), div["volume"]),
            "value_trade": _unscale(rec.get("amount"), div["amount"]),
            "security_code": rec.get("security_code"),
        })
    return out


def _snapshot_records(rows: list) -> list[dict]:
    """线上快照 CSV → 业务层 DataFrame 的记录列表。

    **逐行喂 `decode_snapshot_rows`**，为的是让解码结果与原始行天然对齐：
    整批传进去时它会跳过段数不符的行，返回下标就和 `rows` 对不上了。

    `orig_time` 取原始行的第 2 段，不取解码后的 `trade_time`——
    `_decode_time` 把末 3 位毫秒丢了，而业务层按 `%Y%m%d%H%M%S%f` 解析，
    缺 `%f` 会直接抛错。
    """
    from tgwpy.frames.market import SNAPSHOT_CSV_FIELDS, decode_snapshot_rows

    # `SNAPSHOT_CSV_FIELDS` 是 ((字段名, 除数), …)，转成 dict 后值就是除数本身
    div = dict(SNAPSHOT_CSV_FIELDS)
    out = []
    for raw in rows:
        if not isinstance(raw, str) or len(raw.split(",")) != len(SNAPSHOT_CSV_FIELDS):
            continue
        parts = raw.split(",")
        decoded = decode_snapshot_rows([raw])
        if not decoded:
            continue
        rec = decoded[0]
        row: dict[str, Any] = {
            "orig_time": parts[2],
            "security_code": rec.get("security_code"),
        }
        for name, decode_key in _SNAPSHOT_SCALARS:
            row[name] = _unscale(rec.get(decode_key), div[decode_key])
        for side, price_key, volume_key in _SNAPSHOT_SIDES:
            prices = rec.get(price_key) or []
            volumes = rec.get(volume_key) or []
            price_div = div[price_key]
            volume_div = div[volume_key]
            for lv in range(_SNAPSHOT_BOOK_LEVELS):
                row[f"{side}_price{lv + 1}"] = _unscale(
                    prices[lv] if lv < len(prices) else 0, price_div)
                row[f"{side}_volume{lv + 1}"] = _unscale(
                    volumes[lv] if lv < len(volumes) else 0, volume_div)
        out.append(row)
    return out


def QueryKline(req_kline_cfg, query_spi=None, return_df_format: bool = True):  # noqa: N802
    """K 线查询。`market_data.py` 传 `query_spi=KlineSpi.OnResponse`。

    **请求构造**：`market_data.py:199-208` 逐字段赋值 → 抓包原文。

    | `req_kline_cfg` | 线上 `params` | 依据 |
    | --- | --- | --- |
    | `security_code` | `security_code` | 直传。带 `.SH` 后缀会报 20002 |
    | `market_type` | `market_type` | 整数，`get_tgw_type_code` 从 `600519.SH` 拆出 101 |
    | `cq_flag` | `cq_flag` | 业务层恒置 0 |
    | `auto_complete` | `auto_complete` | 业务层恒置 1 |
    | `cyc_type` | `period_type` | **两套编码**，经 `_period_table()` 查表 |
    | `begin_date` / `end_date` | 同名 | 直传 |
    | `begin_time` / `end_time` | 同名 | 业务层已填 900/1700（夜盘 2000/1700） |
    | —— | `QueryBandWidth` | `KlineRequest.to_params()` 自己补，实测可省 |

    `ReqKline` 另有 `cq_date` / `cyc_def` / `qj_flag` 三个字段，线上 JSON
    协议没有对应键，不编。
    """
    from tgwpy.frames import market

    table = _period_table()

    def build_wire(task_id: int) -> bytes:
        cyc = table[int(req_kline_cfg.cyc_type)]
        return market.build_kline_request(
            market.KlineRequest(
                security_code=req_kline_cfg.security_code,
                market_type=int(req_kline_cfg.market_type),
                period_type=cyc,
                begin_date=int(req_kline_cfg.begin_date),
                end_date=int(req_kline_cfg.end_date),
                begin_time=int(req_kline_cfg.begin_time),
                end_time=int(req_kline_cfg.end_time),
                cq_flag=int(req_kline_cfg.cq_flag),
                auto_complete=int(req_kline_cfg.auto_complete),
            ),
            task_id, username=_SESSION.username, token=_SESSION.token,
        )

    def build_result(rows) -> Any:
        return _to_frame(_kline_records(rows), _KLINE_COLUMNS, return_df_format)

    return _run_market_query(
        "QueryKline", GetTaskID(), build_wire, build_result,
        query_spi, return_df_format,
    )


def QuerySnapshot(req_tick_cfg, query_spi=None, return_df_format: bool = True):  # noqa: N802
    """历史快照（Level-1）查询。`market_data.py` 传 `query_spi=SnapshotSpi.OnResponse`。

    **请求构造**：`market_data.py:64-69` 逐字段赋值 → 抓包原文。

    | `req_tick_cfg` | 线上 `params` | 依据 |
    | --- | --- | --- |
    | `security_code` | `security_code` | 直传 |
    | `market_type` | `market_type` | 整数 |
    | `date` | `date` | 直传 |
    | `begin_time` | `begin_time` | 业务层已填 90000000（夜盘 200000000），**比 K 线大十万倍** |
    | `end_time` | `end_time` | 业务层已填 170000000 |
    | `data_type` | `data_type` | `ReqDefault` 留 0，直传 |
    | —— | `QueryBandWidth` | `SnapshotRequest.to_params()` 自己补 |

    `ReqDefault.level_type` 在线上协议里没有对应键，丢弃。
    """
    from tgwpy.frames import market

    def build_wire(task_id: int) -> bytes:
        return market.build_snapshot_request(
            market.SnapshotRequest(
                security_code=req_tick_cfg.security_code,
                market_type=int(req_tick_cfg.market_type),
                date=int(req_tick_cfg.date),
                begin_time=int(req_tick_cfg.begin_time),
                end_time=int(req_tick_cfg.end_time),
                data_type=int(req_tick_cfg.data_type),
            ),
            task_id, username=_SESSION.username, token=_SESSION.token,
        )

    def build_result(rows) -> Any:
        return _to_frame(
            _snapshot_records(rows), _SNAPSHOT_COLUMNS, return_df_format)

    return _run_market_query(
        "QuerySnapshot", GetTaskID(), build_wire, build_result,
        query_spi, return_df_format,
    )


#: 复权因子 DataFrame 的列序。前 5 项 = `frames/market.EX_FACTOR_FIELDS`
#: （线上的 5 段），末尾两个 `*_text` 是 `decode_ex_factor_rows` 留下的
#: 18 位小数字面量。
#:
#: **两个因子列都必须在**：`download_backward_factor` 取 `cum_factor`
#: （`download_info_data.dis.txt:12179`），`download_adj_factor` 取
#: `ex_factor`（同文件 `:12746`）——**同一个原生 API 同时供这两条路**。
#: 少给一列，另一条业务路立刻 KeyError。
#:
#: `*_text` 两列业务层不会用：它 `set_index(['ex_date'])` 之后只把因子列
#: 装进新表（`pd.DataFrame(adj_factor['cum_factor'])`），这两个字符串列
#: 到不了 HDF5。
_EX_FACTOR_COLUMNS: tuple[str, ...] = (
    "inner_code", "security_code", "ex_date", "ex_factor", "cum_factor",
    "ex_factor_text", "cum_factor_text",
)


def QueryExFactorTable(security_code, query_spi=None,  # noqa: N802
                       return_df_format: bool = True):
    """复权因子表查询。`download_info_data` 传 `query_spi=spi.OnResponse`。

    签名照厂商 `tgw/interface.py:570` 的 `QueryExFactorTable`。它是
    **行情通道**（`send_market` → `/amd/dgw/dgw`）的方法，不是三方资讯，
    放这一节就是为了别把它当成 `QueryThirdInfo` 的一个 function_id。

    ## 请求

    | 项 | 值 |
    | --- | --- |
    | 线上 method | **`ReqGetExFactor`** |
    | `params` | **只有 `security_code`**，裸代码 `600519` |
    | function_id | **没有**。`SetThirdInfoParam` 在这条路上没有意义 |

    抓包原文（探针实发，66 上）：

        {"headers":{"id":1791694922813,"userName":"…","token":"…"},
         "method":"ReqGetExFactor","params":{"security_code":"600519"}}

    `id` 同样在 `headers` 里（`build_request` 的统一信封），顶层不放。
    **入参带 `.SH` 后缀会回 `status=-100` / `tag=DataEmpty`**——业务层
    `get_backward_factor_data` 用 `get_tgw_type_code(code)` 拆出裸码传
    进来，替身原样送线上，不替它加后缀也不替它剥。

    ## 返回形状的来历

    厂商中间层 `TmpQueryExFactorSpi.OnMDExFactor`
    （`tgw/tmp_spi.py:963-972`）是
    `json_normalize(json.loads(Tools_ExFactorTableToJson(...)))`，
    官方 `return_df_format=False` 时给 `json_list`。所以同步形态回
    `(DataFrame, None)`，异步形态回调 `(DataFrame, None)`。

    **业务层吃这张表的方式**（`download_info_data.dis.txt:12179-12204`）：

        adj_factor.set_index(['ex_date'], inplace=True)
        adj_factor.sort_index(inplace=True)
        adj_factor_code = pd.DataFrame(adj_factor['cum_factor'])

    `ex_date` 必须是 int（`get_calendar` 给的 `calendar` 是
    `list(TRADE_DAYS.astype(int))`），否则 `.loc[calendar_index]` 取不到
    值，整张表被 `fillna(1)` 填成 1.0，**复权静默失效**。

    ## 失败形态

    厂商 `OnStatus` 回来的 `error_code`（int）与整形异常里的 `str(e)`
    在替身这里统一变成 `data=None` + **字符串** status：业务层
    `BackwardFactorSpi.OnResponse` 的 `isinstance(status, str)` 分支
    （`info_spi.dis.txt:741-754`）会把它记进 `EnvBackwardFactor.error_list`
    并触发重试。异步形态必须回调，否则 `wait_event.wait()` 永远醒不过来。
    同步形态按官方契约回 `(None, 错误文案)`（`tgw/interface.py:576`）。
    """
    from tgwpy.frames import market

    def build_wire(task_id: int) -> bytes:
        return market.build_ex_factor_request(
            security_code, task_id,
            username=_SESSION.username, token=_SESSION.token,
        )

    def build_result(rows) -> Any:
        return _to_frame(
            market.decode_ex_factor_rows(rows), _EX_FACTOR_COLUMNS,
            return_df_format,
        )

    return _run_market_query(
        "QueryExFactorTable", GetTaskID(), build_wire, build_result,
        query_spi, return_df_format,
    )


def QueryCodeTable(query_spi=None, return_df_format: bool = True):  # noqa: N802
    return _unimplemented("QueryCodeTable", None, query_spi)


# --- 代码表查询（证券信息 / ETF）--------------------------------------------
#
# **线上 method 名跟 SDK 方法名对不上，这是这块最大的坑。**
#
# `QuerySecuritiesInfo` / `QueryETFInfo` 是 `libtgw_python3*.so` 的 C++ 包装名，
# 网关上根本没有这两个字符串。MITM 抓参照客户端的真实流量也确认过，
# 线上发的是：
#
# | SDK 方法名 | 线上 method | params |
# | --- | --- | --- |
# | `QuerySecuritiesInfo` | `ReqGetCodeTableList` | `{"Security": "\|102"}` |
# | `QueryETFInfo` | `ReqGetETFCodeTableList` | `{"Security": "510050\|101"}` |
#
# 照 SDK 名发会**静默失败**——网关不回错误码也不断链。
#
# `Security` 的值是 `"<代码>|<市场码>"`，管道符分隔。三种实发形态：
#
# * 带代码：ETF 侧 `510050|101`（`get_etf_pcf` 逐个 code 建 item）
# * 不带代码：证券信息侧 `|102`（`item.security_code` 业务层置空串）
# * 市场码：取 `MarketType` 的**线上值**（SSE=101 / SZSE=102 / NEEQ=2），
#   不是 `_enums_stub` 的占位序号。抓包里 `|102` 就是 `kSZSE`。
#
# **服务端恒定回 `status=-82`（`kFlowOverLimit`）。** 官方 SDK 自己发的也是
# 同一份报文、同样回 `-82`，7 种 `security_type` 的报文逐字节相同；
# 带代码 / 不带代码 / 换市场码 / 换 method 一律 `-82`（同文档 §2.4）。
# 这是服务端侧限制，**不是实现问题**。代码表接口在 8600 端口取不到数，
# 证券基础信息要走 `InfoData.get_stock_basic`（`A010010001`）那条路。
# 所以本实现的验收判据是「`-82` 被如实上报成可见的失败」，不是「取到数据」。

#: 线上 method 名。别改成 SDK 的包装名。
METHOD_CODE_TABLE_LIST = "ReqGetCodeTableList"
METHOD_ETF_CODE_TABLE_LIST = "ReqGetETFCodeTableList"

#: `params` 里唯一的业务键。抓包原文，两条报文都是它。
KEY_SECURITY = "Security"

#: 线上返回的 `tag`。证券信息 109、ETF 111，区分这两条通道。
TAG_CODE_TABLE = "109"
TAG_ETF_CODE_TABLE = "111"

#: 这两个 method 在当前账号 / 端口上的固定答复。
#: `-82` 的枚举名是 `kFlowOverLimit`（`tgwpy/enums.py`，`ErrorCode` 实测值）。
FLOW_OVER_LIMIT = -82


def _market_code(item) -> int:
    """`SubCodeTableItem.market` → 线上市场码。

    业务层传的是 `tgw.MarketType` 成员（`base_data.py` 里
    `item.market = tgw.MarketType.kSZSE`），值经 `tgwpy.enums` 落地就是
    线上市场码：SSE=101 / SZSE=102 / NEEQ=2。抓包里 `|102` 对应的正是
    `kSZSE`，取值一致，直接取整型即可。

    **不接受 0**：`kNone` 不是合法市场，发 `|0` 只会让服务端再回一个
    没头没尾的 `-82`，把「参数错」和「服务端限制」混成一条。
    """
    code = int(getattr(item, "market", 0) or 0)
    if code == 0:
        raise ValueError("SubCodeTableItem.market 是 kNone(0)，不是合法市场码")
    return code


def _security_param(item) -> str:
    """`SubCodeTableItem` → 线上 `Security` 的值 `"<代码>|<市场码>"`。

    `security_code` 为空串时拼出 `"|102"`——业务层 `get_code_list` 就是
    这么置的（`base_data.dis.txt:1018-1022` 存 `LOAD_CONST ''`），
    线上报文也确实是这样。
    """
    return f"{getattr(item, 'security_code', '') or ''}|{_market_code(item)}"


def _code_table_wire(method: str, item, task_id: int) -> bytes:
    """拼代码表查询的 JSON 请求帧。

    信封走 `frames.market.build_request`——它与 `ReqGetThirdInfo` 同一族
    （同一个 rapidjson writer 家族），`id` 同样在 `headers` 里。抓包原文：

        {"headers": {"id": 1, "userName": "...", "token": "..."},
         "method": "ReqGetCodeTableList",
         "params": {"Security": "|102"}}
    """
    from tgwpy.frames.market import build_request

    return build_request(
        method, {KEY_SECURITY: _security_param(item)}, task_id,
        username=_SESSION.username, token=_SESSION.token,
    )


def _flow_over_limit_note(name: str, method: str) -> Callable[[str], str]:
    """造一个把 `status=-82` 的服务端答复解释清楚的文案整形器。

    **`kFlowOverLimit`（周流量耗尽）在这里名不副实**：登录回包里
    `used_week_flow=743216632 / total_control=1000000000`，额度只用了 74%
    （抓包实证，额度只用了 74%）。原样报上去会误导人去查配额。

    文案里带上调用方函数名和线上 method 名：`-82` 本身信息量太低，
    只看它分不清是哪个接口、走的哪条 method。
    """
    def explain(reason: str) -> str:
        if str(FLOW_OVER_LIMIT) not in reason:
            return reason
        return (
            f"{name}（线上 method={method}）被服务端拒绝：{reason}"
            f"（-82 kFlowOverLimit，名不副实——账号周流量只用了 74%；"
            f"官方 SDK 发同样的报文也回 -82）。"
            f"证券基础信息请改走 InfoData.get_stock_basic(A010010001)，"
            f"该接口在当前账号上是通的。"
        )

    return explain


#: `get_etf_pcf` 把返回的每个元素当**二元组**用：`etf_pcf[0]` 是申赎信息表
#: （`etf_pcf[0]['market_type'].iloc[0]`），`etf_pcf[1]` 是成分股表
#: （`etf_pcf[1].apply(..., axis=1)` 再存 `['code']`）。所以一个 code 的回包
#: 要拆成两帧，不是一帧。
#:
#: 两组列名逐字抄自 `base_data.dis.txt:200` 与 `:208` 的两个 `LOAD_CONST`
#: 元组——**分帧规则是从字节码来的，不是猜的**。分组按「记录里出现哪些列名」
#: 判：命中信息列的进 `[0]`，命中成分列的进 `[1]`。
#:
#: **未验证**：这两个 method 在 8600 端口恒回 `-82`，成功回包的形状从未见过。
#: 真拿到数据时分帧规则对不对，只能等那一刻才知道。
_ETF_INFO_COLUMNS: frozenset[str] = frozenset((
    "creation_redemption_unit", "max_cash_ratio", "publish", "creation",
    "redemption", "creation_redemption_switch", "record_num", "total_record_num",
    "estimate_cash_component", "trading_day", "pre_trading_day",
    "cash_component", "nav_per_cu", "nav", "symbol", "fund_management_company",
    "underlying_security_id", "underlying_security_id_source", "dividend_per_cu",
    "creation_limit", "redemption_limit", "creation_limit_per_user",
    "redemption_limit_per_user", "net_creation_limit", "net_redemption_limit",
))
_ETF_CONSTITUENT_COLUMNS: frozenset[str] = frozenset((
    "underlying_symbol", "component_share", "substitute_flag", "premium_ratio",
    "discount_ratio", "creation_cash_substitute", "redemption_cash_substitute",
    "substitution_cash_amount", "underlying_security_id",
))


def _split_etf_pair(rows, return_df_format: bool) -> list:
    """一个 code 的回包 → `[[信息表], [成分表]]`。

    见 `_ETF_INFO_COLUMNS` 的说明：分帧依据是业务层那两组列名。
    两组都没命中的记录按信息表收——`get_etf_pcf` 第 529 行无条件取
    `etf_pcf[0]` 当信息表用，落空的话那一步就炸。
    """
    info_rows, constituent_rows = [], []
    for rec in rows or ():
        if not isinstance(rec, dict):
            continue
        keys = rec.keys()
        if keys & _ETF_CONSTITUENT_COLUMNS and not keys & (
                _ETF_INFO_COLUMNS - _ETF_CONSTITUENT_COLUMNS):
            constituent_rows.append(rec)
        else:
            info_rows.append(rec)
    return [
        _to_frame(info_rows, None, return_df_format),
        _to_frame(constituent_rows, None, return_df_format),
    ]


def QuerySecuritiesInfo(req_security_info_cfg, query_spi=None,  # noqa: N802
                        return_df_format: bool = True):
    """证券资料查询。`base_data.py` **同步**调用，期望 `(code_table_df, error)`。

    **请求构造**：`base_data.py` 五处调用点（`get_code_list` /
    `get_code_info` / `get_future_code_list` / `get_option_code_list` …）
    逐字段赋值 → 抓包原文。

    | `req_security_info_cfg` | 线上 `params` | 依据 |
    | --- | --- | --- |
    | `security_code` | `Security` 的代码段 | 直传，业务层置 `''` 取全量 |
    | `market` | `Security` 的市场段 | `MarketType` 实值，kSZSE → `102` |

    线上 method 是 **`ReqGetCodeTableList`**，不是本函数名。

    **失败形态**：服务端恒回 `-82`。同步形态按「两条硬规则」第 2 条抛
    `NotConnected`——`get_code_list` 拿到 `(code_table_df, error)` 后
    **丢掉 error**，紧接着 `code_table_df['security_code']`
    （`base_data.py` 第 106–111 行），回 `None` 会在几十行之外炸成
    `TypeError: 'NoneType' object is not subscriptable`，看不出是谁失败。
    异步形态照常回调，`data=None` + 字符串 status 进业务层 `error_list`。
    """
    def build_wire(task_id: int) -> bytes:
        return _code_table_wire(METHOD_CODE_TABLE_LIST, req_security_info_cfg,
                                task_id)

    def build_result(rows) -> Any:
        # 线上 `data` 是记录数组，列名由服务端给（`security_code` /
        # `security_type` / `code_market` / `english_name`，见 base_data
        # 对返回值的逐列下标）。**不钉死列集**：取不到数据时钉列等于编数据。
        return _to_frame(rows, None, return_df_format)

    return _run_market_query(
        "QuerySecuritiesInfo", GetTaskID(), build_wire, build_result,
        query_spi, return_df_format,
        send_method="send", sync_raises=True,
        explain_status=_flow_over_limit_note(
            "QuerySecuritiesInfo", METHOD_CODE_TABLE_LIST),
    )


def QueryETFInfo(req_etf_info_cfg, query_spi=None, return_df_format: bool = True):  # noqa: N802
    """ETF 申赎资料查询。`base_data.py` **同步**调用，返回 `(etf_pcf_list, _)`。

    **入参是列表**，不是单个 `SubCodeTableItem`——`get_etf_pcf` 先按
    `code_list` 逐个 `get_tgw_type_code` 拆出 `(market_type, security_code)`
    填进 `SubCodeTableItem`，攒成 `security_cfg_list` 一次性传进来
    （`base_data.dis.txt:483-188`）。所以这里逐个发请求，回包按入参顺序拼。

    **请求构造**：`base_data.py:485-488` 逐字段赋值 → 抓包原文。

    | `req_etf_info_cfg[i]` | 线上 `params` | 依据 |
    | --- | --- | --- |
    | `security_code` | `Security` 的代码段 | `get_tgw_type_code` 拆出的裸码 |
    | `market` | `Security` 的市场段 | 同上，510050.SH → `101` |

    线上 method 是 **`ReqGetETFCodeTableList`**，不是本函数名。

    **失败形态**与 `QuerySecuritiesInfo` 一致：服务端恒回 `-82`
    （`tag=111`，与证券信息的 `tag=109` 区分）。
    """
    items = list(req_etf_info_cfg or ())
    if not items:
        # 空列表要在发任何东西之前就失败。`query_spi` 必须原样传下去：
        # 漏掉的话异步形态收不到回调，业务层卡在 `wait_event.wait()` 上。
        return _reject("QueryETFInfo 入参列表为空", query_spi)

    def build_result(rows) -> Any:
        # 每个元素是 `[信息表, 成分表]` 二元组——业务层 `get_etf_pcf` 逐个
        # `etf_pcf[0]['market_type']` / `etf_pcf[1].apply(...)`，只给一帧它接不上。
        return _split_etf_pair(rows, return_df_format)

    # 逐个 code 发一次。业务层只用同步形态；异步形态下每个 item 各自回调一次
    # （`_run_market_query` 的契约：有 `query_spi` 就回 `(True, None)`）。
    # 同步形态下第一项失败即抛 `NotConnected`——「两条硬规则」第 2 条，
    # 半截结果交回去只会让业务层在别处炸出看不懂的 TypeError。
    results = [_run_etf_one(item, build_result, query_spi, return_df_format)
               for item in items]
    if query_spi is not None:
        return True, None
    return ([r[0] for r in results], None)


def _run_etf_one(item, build_result, query_spi, return_df_format):
    """一个 ETF code 的一次查询，形态与 `QuerySecuritiesInfo` 完全一致。"""
    def build_wire(task_id: int) -> bytes:
        return _code_table_wire(METHOD_ETF_CODE_TABLE_LIST, item, task_id)

    return _run_market_query(
        "QueryETFInfo", GetTaskID(), build_wire, build_result,
        query_spi, return_df_format,
        send_method="send", sync_raises=True,
        explain_status=_flow_over_limit_note(
            "QueryETFInfo", METHOD_ETF_CODE_TABLE_LIST),
    )


# --- 订阅 -------------------------------------------------------------------


def Subscribe(sub_item, push_spi=None):  # noqa: N802
    """订阅。返回 ErrorCode——`on_data.py::run` 拿它和 `kSuccess` 比。"""
    if push_spi is not None:
        _SESSION.set_push_spi(push_spi)
    if _SESSION.transport is None:
        _SESSION.log(LogLevel.kError, f"Subscribe 失败：{NO_TRANSPORT}")
        return ErrorCode.kFailure
    return ErrorCode.kSuccess


def UnSubscribe(sub_item, push_spi=None):  # noqa: N802
    return ErrorCode.kSuccess


def ReplayRequest(replay_cfg, replay_spi=None,  # noqa: N802
                  return_df_format: bool = True):
    return _unimplemented("ReplayRequest", replay_cfg, replay_spi)


def CancelTask(task_id):  # noqa: N802
    """取消在途任务。被取消的任务不会回调业务层——调用方自己知道结果。"""
    return ErrorCode.kSuccess if _SESSION.pop_task(int(task_id)) else ErrorCode.kFailure


# --- 改密 -------------------------------------------------------------------


def IGMDApi_UpdatePassWord(update_password_req):  # noqa: N802
    """改密。没接传输层时回 `kFailure`——`update_password` 打印错误码后返回，
    不会误报成功。"""
    if _SESSION.transport is None:
        _SESSION.log(LogLevel.kError, f"IGMDApi_UpdatePassWord 失败：{NO_TRANSPORT}")
        return ErrorCode.kFailure
    return ErrorCode.kSuccess


def UpdatePassWord(update_password_req):  # noqa: N802
    return IGMDApi_UpdatePassWord(update_password_req)
