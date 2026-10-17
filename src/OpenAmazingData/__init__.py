"""OpenAmazingData：AmazingData SDK 查询接口的自研实现，arm macOS 原生可用。

    import OpenAmazingData as ad
    ad.login(username, password, host, port)
    ad.InfoData().get_income(["600519.SH"], local_path, is_local=False)

对外接口（类名、方法名、参数位置、返回形状、本地缓存格式）与厂商
AmazingData 1.1.15 一致（厂商 .pyc 字节码抽取的接口事实 + 逐方法流程规格，
在私有记录仓里用等价门禁逐项核对）。与厂商的差异见 `docs/vendor-differences.md`。
协议层是 `tgwpy`。
"""

from __future__ import annotations

__version__ = "0.1.0"

from . import _clock, constant
from ._session import login, logout, update_password
from .base import BaseData
from .info import InfoData
from .market import MarketData

__all__ = ["__version__", "constant", "login", "logout", "update_password",
           "BaseData", "InfoData", "MarketData"]
