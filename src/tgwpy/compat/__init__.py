"""纯 Python 的 `tgw` 替身，让 AmazingData 业务层在 arm macOS 上原生跑。

用法：

    import sys; sys.path.insert(0, "src")
    from tgwpy.compat import install
    install()                      # 装进 sys.modules["tgw"]
    import AmazingData              # 顶层包和 10 个依赖 tgw 的子包都能导入

    from tgwpy.compat import uninstall
    uninstall()                    # 摘掉替身

替身不带网络实现。接上传输层后下载才真正可用：

    from tgwpy.compat import tgw_module
    tgw_module.set_transport(client)          # client 需有 send(bytes) / login(...)
    # 收到响应时 client 调：
    tgw_module.deliver_response(task_id, data_dict, status)

没接传输层时，所有查询接口都不会静默返回空数据：同步形态抛
`tgw_module.NotConnected`，异步形态用字符串状态回调把失败送回业务层的
`error_list`。理由见 `tgw_module` 的模块文档。
"""

from __future__ import annotations

from tgwpy.compat import structs
from tgwpy.compat.tgw_module import (
    MINED_VALUES_VERIFIED,
    VALUES_SOURCE,
    Close,
    GetErrorMsg,
    GetTaskID,
    Login,
    Logout,
    NotConnected,
    deliver_response,
    install,
    is_installed,
    pending_task_ids,
    session,
    set_transport,
    uninstall,
)

__all__ = [
    "install",
    "uninstall",
    "is_installed",
    "Login",
    "Logout",
    "Close",
    "GetTaskID",
    "GetErrorMsg",
    "NotConnected",
    "session",
    "set_transport",
    "deliver_response",
    "pending_task_ids",
    "MINED_VALUES_VERIFIED",
    "VALUES_SOURCE",
    "structs",
    "tgw_module",
]


def __getattr__(name: str):
    """`tgw_module` 延迟导入，避免包初始化时就把 frames 层拖进来。"""
    if name == "tgw_module":
        from tgwpy.compat import tgw_module as mod

        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
