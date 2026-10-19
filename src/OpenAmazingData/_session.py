"""登录、登出、改密码。

行为照厂商 `login/tgw_login.pyc`：

* 用户名以 `tgw_` 开头直接抛 `Exception('username is illegal')`；
* 登录前先断开旧会话；
* 登录失败且日志里出现「连接数超限」时，带 `force_logout=True` 再试最多 5 次，
  每次失败等 2 秒；
* 最终失败打印 `login fail` 并 `SystemExit(0)`，成功打印 `login success`
  并返回 True。

厂商的原生库自己建连接。这里在会话还没有传输层时按 `host` / `port` 建一条
`tgwpy.wire.WssTransport`；已经有了（批处理、测试里的假网关）就沿用。
"""

from __future__ import annotations

import time
from ._diagnostics import emit_failure, remember_credentials

from tgwpy.compat import structs
from tgwpy.compat import tgw_module as tgw

_MAX_LIMITATION = "Connections of this user exceed the max limitation"

_COLOCATION_DEFAULT = {"qtcp_channel_thread": 8, "qtcp_max_req_cnt": 3000,
                       "qtcp_req_time_out": 10, "enable_order_book": 0,
                       "entry_size": 20, "order_queue_size": 50}


class _LogWatcher(structs.ILogSpi):
    """保留脱敏错误日志，并识别服务端连接数超限。"""

    def __init__(self, log_level: int = 4) -> None:
        super().__init__()
        self.log_level = log_level
        self.max_limitation = False

    def OnLog(self, level, log, len):  # noqa: A002, N802 - 与厂商回调同名同参
        if level >= self.log_level and _MAX_LIMITATION in log:
            self.max_limitation = True

        if level >= self.log_level:
            emit_failure("sdk_error", level=level, message=log)

    def OnLogon(self, data):  # noqa: N802
        pass


def _ensure_transport(username: str, password: str, host: str, port) -> None:
    if tgw.session().transport is not None:
        return
    from tgwpy.wire import WssTransport

    tgw.set_transport(WssTransport(str(host), str(port), username, password))


def _config(username, password, host, port, api_mode, colocation, force_logout):
    remember_credentials(username, password)
    tgw.Close()
    watcher = _LogWatcher()
    tgw.SetLogSpi(watcher)
    cfg = structs.Cfg()
    cfg.username = username
    cfg.password = password
    cfg.server_vip = host
    cfg.server_port = port
    cfg.force_logout = force_logout
    if api_mode == "kInternetMode":
        mode = tgw.ApiMode.kInternetMode
    elif api_mode == "kColocationMode":
        mode = tgw.ApiMode.kColocationMode
        para = colocation if colocation is not None else _COLOCATION_DEFAULT
        cfg.coloca_cfg.channel_mode = tgw.ColocatChannelMode.kTCP | tgw.ColocatChannelMode.kQTCP
        for key in _COLOCATION_DEFAULT:
            setattr(cfg.coloca_cfg, key, para[key])
    else:
        mode = api_mode
    return cfg, mode, watcher


def login(username, password, host, port, api_mode="kInternetMode", kColocationMode_para=None):  # noqa: N803
    if isinstance(username, str) and username.startswith("tgw_"):
        raise Exception("username is illegal")
    _ensure_transport(username, password, host, port)
    cfg, mode, watcher = _config(username, password, host, port, api_mode,
                                 kColocationMode_para, force_logout=False)
    success = tgw.Login(cfg, mode)
    if not success and watcher.max_limitation:
        for _ in range(5):
            cfg, mode, watcher = _config(username, password, host, port, api_mode,
                                         kColocationMode_para, force_logout=True)
            success = tgw.Login(cfg, mode)
            if success:
                break
            time.sleep(2)
    if not success:
        print("login fail")
        raise SystemExit(0)
    print("login success")
    return success


def logout(username):
    tgw.Close()


def update_password(username, old_password, new_password):
    req = structs.UpdatePassWordReq()
    req.username = username
    req.old_password = old_password
    req.new_password = new_password
    code = tgw.IGMDApi_UpdatePassWord(req)
    if code != tgw.ErrorCode.kSuccess:
        print("UpdatePassWord failed, error code is : ", code)
        return None
    print("UpdatePassWord success")
