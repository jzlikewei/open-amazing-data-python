"""把 :class:`TgwClient` 接到 :mod:`tgwpy.compat` 的传输槽上。

compat 层已经抽象好了：

* ``set_transport(transport)`` —— 注入对象，提供 ``send(bytes)``
* ``deliver_logon(ok, message)`` —— 登录结果
* ``deliver_response(task_id, data, status)`` —— 按 task_id 分发查询结果

**同步问题**：compat 和 AmazingData 业务层都是同步的（用
``threading.Event.wait()`` 等结果），而 WebSocket 客户端是异步的。
这里用一个**后台事件循环线程**解决：业务线程调 ``send()`` 时把协程
投递给那个循环，读帧也在那里跑。

**一条连接。** 网关按连接认会话：同一账号第二次登录必然被拒——实测
``status=-98``，``ForceLogout`` 取 0 或 1 都一样；两条连接里第二条若不
登录、复用第一条的 token，则被服务端主动断连。四种拓扑只有「单连接走
``/amd/dgw/dgw``、自己登录一次」能通，而这一条连接同时能承载资讯
（``ReqGetThirdInfo``，34 个已实测的 function_id）与行情
（``ReqGetKline`` / ``ReqGetSnapshot``）两族。数据见
传输栈实测（查出双连接设计是错的）。

**登录只有一条路径：compat 的 ``Login()``。** ``send()`` 把认证帧发出去，
读帧协程认出 ``tag == "OnRspLogon"`` 后调 ``deliver_logon``，token 从
应答的 ``headers.token`` 落到 client 上。传输层自己再发一次 ``ReqLogon``
就是那个 ``-98`` 的来源，所以这里刻意没有第二条登录路径。

用法：

    from tgwpy.wire import install_transport
    install_transport()     # 装 compat 替身 + 注入传输

    import OpenAmazingData as ad
    ad.login(...)           # ← 这一步发 ReqLogon 并等 token
    ad.InfoData().get_income(code_list=['600519.SH'], is_local=False)
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable

from .client import PATH_MARKET, TgwClient
from .guard import RequestGate

# compat 的 deliver_* 回调，由 tgwpy.compat.install() 注册
_deliver_logon: Callable[[bool, str], None] | None = None
_deliver_response: Callable[[int, Any, Any], None] | None = None


def _hooks() -> tuple[Callable | None, Callable | None]:
    """从 compat 模块取回注册好的回调。延迟导入避免循环依赖。"""
    global _deliver_logon, _deliver_response
    if _deliver_logon is None:
        from .compat import tgw_module as m

        _deliver_logon = m.deliver_logon
        _deliver_response = m.deliver_response
    return _deliver_logon, _deliver_response



#: 网关 JSON 信封里的「这组参数查不到数据」（`tag='DataEmpty'`），
#: 2026-10-11 网关调研探针实测。不是失败。
_EMPTY_STATUS = -100

#: 暂时性错误：由调用方按 `guard.transient_delay` 重试（交易日历是
#: `OpenAmazingData.base._fetch_window`），不进 `RequestGate` 的停手计数——
#: 两套计数混在一起，重试几次就会把整轮判成「网关停手」（退出码 5）。
_TRANSIENT_STATUSES = frozenset({-89})

class WssTransport:
    """把 compat 的 ``send(bytes)`` 落到真实网关。

    自带后台事件循环线程，因此对同步调用方（compat / AmazingData 业务层）
    表现为**阻塞式**接口：

    * :meth:`send`（资讯/下载族）与 :meth:`send_market`（行情族）发的是
      **同一条连接**，两者语义一致，保留两个名字只为不动 compat 的调用点
    * 登录由 compat 的 ``Login()`` 发起，本类不代劳
    """

    def __init__(self, host: str, port: str, user: str, password: str,
                 gate: RequestGate | None = None):
        # 全程只有这一个 client。资讯与行情共用它（见模块文档「一条连接」）
        self.client = TgwClient(host, port, user, password, path=PATH_MARKET)
        #: 节流 + 失败退避。挂在**唯一发送路径**上，资讯族和行情族共用
        #: ——分开放两个闸门等于有一个形同虚设。详见 guard.RequestGate。
        #: 构造时可以换掉，协议层测试用零间隔的闸门，不必替生产节奏买单。
        self.gate = gate if gate is not None else RequestGate()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        #: 读帧协程，只有一个，且只与 `client._ws` 当前那条连接配对
        self.pump: asyncio.Task | None = None
        #: 连接已作废，等下一次 send() 重连
        self.closed = True
        #: 等待后续分包的响应，task_id → 已收到的行。读帧协程（事件循环
        #: 线程）往里塞，`close()`（业务线程）整表作废
        self.pending: dict[int, list] = {}
        #: 护「建连 + 起读帧协程」这段。两个业务线程同时首次 `send()`
        #: 时没有它会各建一条连接，而网关只认一条，第二条登录立刻 -98
        self._open_lock = threading.Lock()

    # -- 生命周期 ------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="tgw-wss", daemon=True
        )
        self._thread.start()
        return self._loop

    def _submit(self, coro) -> None:
        """把协程丢到后台循环，立刻返回。"""
        loop = self._ensure_loop()
        asyncio.run_coroutine_threadsafe(coro, loop)

    def close(self) -> None:
        """标记连接已关闭，让下一次 send()/send_market() 自动重连。

        **不要在这里停后台循环**：业务层 `set_cfg()` 开头会调 `tgw.Close()`，
        compat 的 `Close()` 随即把 transport 置空。若这里真把循环停掉，
        后续 `Login()` 就再也连不上（实测报 "未登录" 前置检查失败）。
        """
        self.closed = True
        # 断链后半截响应永远等不到后续分包，直接作废。
        # 不回填错误：close() 是业务层主动断的，会话表已经一起清空了。
        self.pending.clear()
        ws, self.client._ws = self.client._ws, None
        pump, self.pump = self.pump, None
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # 取消协程与关连接都得回循环线程里做
        try:
            if pump is not None:
                loop.call_soon_threadsafe(pump.cancel)
            if ws is not None:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
        except RuntimeError:  # 循环在检查之后停了，连接交给回收
            pass

    def shutdown(self, drain: float = 2.0) -> None:
        """真正收工：停后台循环。进程退出前用。

        停之前**等一小会儿**让关连接的协程跑完。不等的话事件循环带着
        `Connection.close()` 和 `keepalive` 两个未完成的 task 被销毁，
        asyncio 会往 stderr 吐一串 `Task was destroyed but it is pending!`
        ——批处理的日志里混进这种噪音，人会以为出了故障。
        """
        self.close()
        if self._loop is not None and not self._loop.is_closed():
            deadline = time.monotonic() + drain
            while time.monotonic() < deadline:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                # 只剩自己这个 shutdown 就不等了
                if len(pending) <= 1:
                    break
                time.sleep(0.02)
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=drain)
        self._loop = None
        self._thread = None

    def __enter__(self) -> "WssTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 建连 ----------------------------------------------------------------

    def reopen(self, timeout: float = 30.0) -> bool:
        """确保连接可用，已连上则什么都不做。返回是否可用。

        已连上时**不重开**：网关只认一个会话，多一条连接会让后续的登录拿到
        ``status=-98``。断链与 ``close()`` 之后，由下一次 ``send()`` 自愈。
        """
        return self._ensure_ready(timeout)

    def _ensure_ready(self, timeout: float = 30.0) -> bool:
        """「一条连接 + 一个读帧协程」的保证点，已就绪就直接返回。"""
        with self._open_lock:
            if (not self.closed and self.client._ws is not None
                    and self.pump is not None):
                return True
            loop = self._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(self._open(), loop)
            try:
                fut.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - 建连失败由调用方决定怎么报
                return False
            return True

    async def _open(self) -> None:
        """建连 + 起读帧协程。**不登录**——登录由 compat 的 ``Login()`` 驱动。

        认证应答由这个读帧协程认：compat 的 ``Login()`` 走 ``send()``，
        而 ``send()`` 自己会先 ``_ensure_ready()`` 把连接建起来。
        """
        await self.client.__aenter__()
        self.closed = False
        self.pump = asyncio.create_task(self._pump_loop())

    # -- compat 接口 ---------------------------------------------------------

    def send(self, payload: bytes) -> None:
        """compat 的资讯 / 下载查询入口（``QueryThirdInfo`` 等 53 个下载方法）。"""
        self._dispatch(payload)

    def send_market(self, payload: bytes) -> None:
        """compat 的行情查询入口（``QueryKline`` / ``QuerySnapshot``）。

        与 :meth:`send` 发的是**同一条连接**。两个方法名只为不动 compat 的
        调用点：那边两族走两个 API，传输层这边只有一条会话。
        """
        self._dispatch(payload)

    def _dispatch(self, payload: bytes) -> None:
        """两族唯一的发送路径：确保连接就绪，再把帧投给后台循环。

        业务层每次 `set_cfg()` 都先 `Close()`，所以认证帧发出来时连接必然
        处于关闭状态——必须能自恢复。

        `gate.wait()` 在**建连之前**：连不上就不该占着节流的配额，
        而且它自己会判断「连续失败够多了没有」，够多就直接停手。
        """
        self.gate.wait()
        if not self._ensure_ready():
            self.gate.record_failure(f"建连失败 {self.client.url}")
            raise ConnectionError(f"连不上网关 {self.client.url}")
        # send() 是在**业务线程**调用的，那里没有运行中的事件循环，
        # 所以不能用 asyncio.create_task，必须投给后台循环。
        self._submit(self.client._ws.send(payload))

    # -- 读帧循环 ------------------------------------------------------------

    async def _pump_loop(self) -> None:
        """持续读帧，按 headers.id 分发回对应 task。"""
        _, rsp_cb = _hooks()
        ws = self.client._ws
        while True:
            try:
                raw = await ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接断了要让业务层知道
                self._on_link_down(ws, f"连接中断: {type(exc).__name__}: {exc}")
                return

            # 帧是 BINARY（opcode 0x2），但 websockets 两种都能收，
            # 底下这层不该假设拿到的就是 bytes。
            if isinstance(raw, str):
                raw = raw.encode()
            env = TgwClient._decode_frame(raw)
            if env is None:
                continue

            headers = env.get("headers") or {}
            status = env.get("status", 0)

            # 登录应答：必须走 deliver_logon，不能当普通查询分发，
            # 否则 compat 的 Login() 等不到结果，业务层会 print('login fail')
            if headers.get("tag") == "OnRspLogon":
                self._on_logon(headers, status, env)
                continue

            task_id = headers.get("id")

            if status != 0:
                # 业务错误：回传 msg 字符串，让业务层记进 error_list
                if task_id is not None:
                    self.pending.pop(int(task_id), None)
                    # 带上 status：业务层拿到的只有这串字，有 msg 时原先就丢了码，
                    # 上层没法区分「暂时性错误」和「参数错」。
                    raw_msg = env.get("msg")
                    msg = f"{raw_msg}（status={status}）" if raw_msg else f"status={status}"
                    if status == _EMPTY_STATUS:
                        # 空结果说明后端活着，不是失败；记成失败的话连续 5 只
                        # 没数据的代码就会被判「网关不可用」退出码 5。
                        self.gate.record_success()
                    elif status not in _TRANSIENT_STATUSES:
                        self.gate.record_failure(msg)
                    if rsp_cb:
                        rsp_cb(int(task_id), None, msg)
                continue

            if task_id is None or not rsp_cb:
                continue

            self.gate.record_success()
            rows = TgwClient._extract_records(env)
            # 分包：两族共用这条连接，收齐才回填。行情单日快照实测 4 帧
            # 合计 5432 行，资讯 `A010050002` 单帧。判据是 headers.all_pack_num
            # ——缺它表示服务端没分包，立刻回填——判据只有 `_is_last_pack`
            # 这一处，与 `TgwClient.query` 同源。
            if not TgwClient._is_last_pack(env):
                self.pending.setdefault(int(task_id), []).extend(rows)
                continue
            merged = self.pending.pop(int(task_id), [])
            merged.extend(rows)
            rsp_cb(int(task_id), merged, 0)

    def _on_logon(self, headers: dict, status: int, env: dict) -> None:
        """认证应答交给 compat。

        token 要落**两处**：compat 的会话（拼信封时读它）和 client 自己
        （`TgwClient.query` 这类自走路径读它）。只落一处的话，另一条路
        会带空 token 出去，服务端回 `status=-87`。
        """
        logon_cb, _ = _hooks()
        if status != 0:
            if logon_cb:
                logon_cb(False, str(env.get("msg") or f"status={status}"))
            return
        token = headers.get("token") or ""
        self.client.token = token
        if logon_cb:
            logon_cb(True, "", token)

    def _on_link_down(self, ws, message: str) -> None:
        """断链收尾：连接作废（下一条查询重连），半截响应按失败回填。

        不回填的话，差几包的任务会一直挂在 compat 的任务表里——业务层那边
        是 ``Event.wait()``，永远醒不过来。
        """
        if self.client._ws is not ws:
            return  # 已经换新连接了，旧连接的收尾不该动它的状态
        self.closed = True
        self.pump = None
        self.client._ws = None
        # 断链算失败。实测批量打快了之后的表现就是连不上，
        # 这一条正是把它喂给 gate 的唯一入口。
        self.gate.record_failure(message)
        self._flush_pending(message)

    def _flush_pending(self, message: str) -> None:
        """把还差几包的响应按失败回填。连接断了它们永远等不到后续帧。"""
        _, rsp_cb = _hooks()
        for task_id in list(self.pending):
            self.pending.pop(task_id, None)
            if rsp_cb:
                rsp_cb(task_id, None, message)


def install_transport(host: str | None = None, port: str | None = None,
                      user: str | None = None, password: str | None = None) -> WssTransport:
    """装上纯 Python tgw 替身并接好传输层。

    凭据默认从环境变量 ``TGW_HOST`` / ``TGW_PORT`` / ``TGW_USER`` / ``TGW_PASSWORD`` 读。
    返回的 transport 已经注入 compat；认证帧由 compat 的 ``Login()``
    （业务层那边是 ``ad.login()``）发出，本类不代发。
    """
    from .compat import install as install_compat, set_transport

    install_compat()
    t = WssTransport(
        host or os.environ["TGW_HOST"],
        port or os.environ["TGW_PORT"],
        user or os.environ["TGW_USER"],
        password or os.environ["TGW_PASSWORD"],
    )
    set_transport(t)
    return t
