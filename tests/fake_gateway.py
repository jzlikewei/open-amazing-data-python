"""测试用的假网关：替身 tgw 的传输层，按请求回放夹具。

接在 `tgwpy.compat.set_transport()` 上，位置和 `tgwpy.wire.WssTransport`
完全相同：compat 发出线上 JSON 帧，本类解出 `headers.id` 与 `method`，
从 `capability_spec.SERVER` 取回包，再经 `deliver_response` /
`deliver_logon` 送回 compat。业务层（.pyc 或还原的 .py）看到的就是
一个真网关。

回包放在单独的线程上投递。真传输层是在后台事件循环线程里回调的，
业务层先发完一批请求、再 `wait_event.wait()`。同一线程里同步回调会
把这个时序改掉。
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

#: 线上 status 非 0 时，`wire._pump_loop` 交给 compat 的是 `msg` 或这个形状。
def _status_text(status: int, msg: str = "") -> str:
    return msg or f"status={status}"


class FakeGateway:
    """compat 的传输层契约：`send` / `send_market` / `close` / `shutdown`。"""

    def __init__(self, server) -> None:
        self.server = server
        self.requests: list[dict] = []
        #: 夹具里没有的请求。测试据此报「规范漏了这个接口」，不让它静默变空。
        self.unfixtured: list[str] = []
        self._outbox: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._pump, daemon=True)
        self._worker.start()

    # -- compat 接口 ---------------------------------------------------------

    def send(self, payload: bytes) -> None:
        self._receive(payload)

    def send_market(self, payload: bytes) -> None:
        self._receive(payload)

    def close(self) -> None:
        """业务层每次 `set_cfg()` 都会 `Close()`，假网关没有连接可关。"""

    def shutdown(self, drain: float = 0.0) -> None:
        self._outbox.put(None)

    # -- 内部 ----------------------------------------------------------------

    def _receive(self, payload: bytes) -> None:
        env = json.loads(payload)
        headers = env.get("headers") or {}
        if env.get("method") == "ReqLogon":
            self._outbox.put(("logon", None, None))
            return
        request = {k: v for k, v in env.items() if k != "headers"}
        self.requests.append(request)
        reply = self.server.reply(request)
        if reply is None:
            self.unfixtured.append(json.dumps(request, ensure_ascii=False, sort_keys=True))
            reply = (None, _status_text(-1, "假网关没有这个接口的夹具"))
        data, status = reply
        self._outbox.put(("response", int(headers["id"]), (data, status)))

    def _pump(self) -> None:
        from tgwpy.compat import tgw_module as tm

        while True:
            item = self._outbox.get()
            if item is None:
                return
            kind, task_id, body = item
            if kind == "logon":
                tm.deliver_logon(True, "", "<token>")
                continue
            data, status = body
            tm.deliver_response(task_id, data, status)


def request_params(request: dict[str, Any]) -> dict[str, str]:
    """资讯族参数在 `item` 数组里，行情族在 `params` 对象里。合成一张表。"""
    out: dict[str, str] = {}
    if "function_id" in request:
        out["function_id"] = str(request["function_id"])
    for pair in request.get("item") or ():
        out[str(pair.get("key"))] = str(pair.get("value"))
    params = request.get("params") or {}
    if isinstance(params, dict):
        for key, value in params.items():
            if key == "item":
                continue
            out.setdefault(str(key), str(value))
    return out
