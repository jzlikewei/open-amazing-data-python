"""在本进程里把自研包 `OpenAmazingData` 接到假网关上，给单元测试用。

    def reply(request):            # 收到一条线上请求，回 (data, status) 或 None
        ...
    with open_sdk(reply) as (ad, gateway):
        ad.InfoData().get_income(["600519.SH"], str(tmp_path) + "/", False)
        gateway.requests           # 发出去的请求（去掉 headers）

不依赖厂商发行物，也不依赖接口规范，随公开仓发布。`reply` 返回 None 表示
「夹具里没有这个接口」，网关回失败并把请求记进 `gateway.unfixtured`。
"""

from __future__ import annotations

import contextlib
import io
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fake_gateway import FakeGateway, request_params  # noqa: E402

__all__ = ["open_sdk", "request_params"]


class _Server:
    def __init__(self, reply: Callable[[dict], Any]) -> None:
        self._reply = reply

    def reply(self, request: dict):
        return self._reply(request)


@contextlib.contextmanager
def _no_network() -> Iterator[None]:
    def refuse(*_a, **_k):
        raise OSError("单元测试禁止出网")

    saved = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = saved


@contextlib.contextmanager
def open_sdk(reply: Callable[[dict], Any], today: str = "20251231"):
    import OpenAmazingData as ad
    from tgwpy.compat import tgw_module as tgw

    gateway = FakeGateway(_Server(reply))
    tgw.set_transport(gateway)
    saved_today = ad._clock.fixed_today
    ad._clock.fixed_today = today
    try:
        with _no_network(), contextlib.redirect_stdout(io.StringIO()):
            ad.login("unit", "unit", "127.0.0.1", "1")
        gateway.requests.clear()
        with _no_network():
            yield ad, gateway
    finally:
        ad._clock.fixed_today = saved_today
        gateway.shutdown()
        tgw.Close()
