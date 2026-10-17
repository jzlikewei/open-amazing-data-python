"""tgw 替身的验收测试。

四条判据（任务书指定）：

1. 装上替身后 `AmazingData` 顶层包和四个依赖 tgw 的子模块全部能 import
2. `Cfg` 能构造，username / password / server_vip / server_port 可写可读
3. 业务层的 SPI 继承链能建立：`class MySpi(tgw.IGMDSpi)` 能定义
4. `GetTaskID()` 符合逆向记录事实 5 的算法

外加两条针对替身行为契约的判据（模块文档「两条硬规则」）：

5. 异步查询接口一定会回调，不会让业务层卡在 `wait_event.wait()` 上
6. 同步查询接口抛 `NotConnected`，不返回空数据冒充成功

跑法（本机没装 pytest，两种都支持）：

    .venv/bin/python -m pytest tests/test_compat_import.py -v
    .venv/bin/python tests/test_compat_import.py
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from conftest import needs_business_layer  # noqa: E402
from tgwpy.compat import install, is_installed, uninstall  # noqa: E402
from tgwpy.compat import tgw_module  # noqa: E402

#: 必须能 import 的模块。前四个是任务书点名的，第 5 个是顶层包本身。
#: 全部 `import tgw`（10 个依赖模块里最吃紧的一批，见 tgw_module 文档）。
TARGET_MODULES = [
    "AmazingData",
    "AmazingData.query_api.info_data",
    "AmazingData.query_api.market_data",
    "AmazingData.query_api.base_data",
    "AmazingData.download_data.download_info_data",
    "AmazingData.download_data.info_spi",
    "AmazingData.download_data.market_spi",
    "AmazingData.login.tgw_login",
    "AmazingData.subscribe_api.on_data",
    "AmazingData.utils.convert",
    "AmazingData.utils.constant",
]

_installed = False


def _ensure_installed() -> None:
    global _installed
    if not _installed:
        install()
        _installed = True


@needs_business_layer
def test_1_business_modules_import() -> None:
    """判据 1：装上替身后 AmazingData 全部依赖 tgw 的模块都能导入。"""
    _ensure_installed()
    assert is_installed(), "install() 没有把替身装进 sys.modules"
    assert sys.modules["tgw"].__tgwpy_compat__ is True

    # 顶层包急切 import 了 login / query_api / download_data，
    # 装之前它会在 import tgw 处断掉
    failed: list[str] = []
    for name in TARGET_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 要看到每个失败的具体原因
            failed.append(f"{name} -> {type(exc).__name__}: {exc}")
    assert not failed, "以下模块导入失败:\n  " + "\n  ".join(failed)


def test_2_cfg_fields_writable() -> None:
    """判据 2：Cfg 构造 + 四个字段可写可读。"""
    _ensure_installed()
    import tgw

    cfg = tgw.Cfg()
    cfg.username = "someuser"
    cfg.password = "somepassword"
    cfg.server_vip = "101.230.159.234"
    cfg.server_port = 8600

    assert cfg.username == "someuser"
    assert cfg.password == "somepassword"
    assert cfg.server_vip == "101.230.159.234"
    assert cfg.server_port == 8600
    # 其余两个字段也必须存在且可写
    cfg.force_logout = True
    assert cfg.force_logout is True
    assert isinstance(cfg.coloca_cfg, tgw.ColocaCfg)
    cfg.coloca_cfg.channel_mode = tgw.ColocatChannelMode.kTCP | tgw.ColocatChannelMode.kQTCP
    assert cfg.coloca_cfg.channel_mode


def test_3_spi_inheritance() -> None:
    """判据 3：SPI 基类可被继承，虚函数可被重写。"""
    _ensure_installed()
    import tgw

    class MyGmdSpi(tgw.IGMDSpi):
        def __init__(self) -> None:
            super().__init__()
            self.hits: list[tuple] = []

        def OnMDSnapshot(self, data, err):  # noqa: N802
            self.hits.append(("snapshot", data, err))

    class MyLogSpi(tgw.ILogSpi):
        def OnLog(self, level, log, len):  # noqa: A002
            pass

        def OnLogon(self, data):
            pass

    class MyPushSpi(tgw.IPushSpi):
        def OnKLine(self, data, kline_type, err):  # noqa: N802
            pass

    # 厂商 TwgLogSpi 的写法：默认参数 + super().__init__()
    class VendorStyleSpi(tgw.ILogSpi):
        def __init__(self, log_level: int = 4) -> None:
            self.log_level = log_level
            super().__init__()
            self.max_limitation = False

    spi = MyGmdSpi()
    assert isinstance(spi, tgw.IGMDSpi)
    spi.OnMDSnapshot({"p": 1}, None)
    assert spi.hits == [("snapshot", {"p": 1}, None)]

    assert MyLogSpi().__class__.__mro__[1] is tgw.ILogSpi
    # IPushSpi.__init__ 必须能零参调用（SubscribeData 构造后立刻 super().__init__()）
    assert MyPushSpi()._IsDfFormat() is True
    vs = VendorStyleSpi()
    assert vs.log_level == 4 and vs.max_limitation is False


def test_4_get_task_id_matches_algorithm() -> None:
    """判据 4：GetTaskID 复刻 `TgwImpl::GetTaskID` @ 0x2123a0。"""
    _ensure_installed()
    import tgw

    task_id = tgw.GetTaskID()
    assert isinstance(task_id, int) and not isinstance(task_id, bool)
    assert task_id > 0

    # 独立重算公式（逆向记录事实 5），逐项对齐。
    # 原式末尾 `if (id == last) lock add [counter], 1` 会在同秒重复调用时
    # 往上加，所以真值只能是 base*1e6+1 加上一个很小的去重偏移。
    base = _task_id_base()
    offset = task_id - (base * 1_000_000 + 1)
    assert 0 <= offset < 10_000, f"task_id={task_id} 偏离公式，偏移 {offset}"

    # 连打 200 次必须互不相同：去重分支生效，且不会撞号
    ids = [tgw.GetTaskID() for _ in range(200)]
    assert len(set(ids)) == 200, "GetTaskID 产生了重复任务号"


def _task_id_base() -> int:
    """按事实 5 的公式独立重算 `GetTaskID` 的基值。"""
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
    return v


def test_5_async_query_always_calls_back() -> None:
    """判据 5：异步查询一定回调，业务层不会卡在 wait_event 上。

    照抄业务层 `download_info_data.get_block_trading` 的骨架：
    设参数 -> 发起 -> 等事件。
    """
    _ensure_installed()
    import tgw

    calls: list[tuple] = []
    wait_event = threading.Event()
    req_list_len = 1

    def spi_on_response(data, status):  # noqa: N807 - 业务层的命名
        nonlocal req_list_len
        req_list_len -= 1
        calls.append((data, status))
        if req_list_len == 0:
            wait_event.set()

    task_id = tgw.GetTaskID()
    tgw.SetThirdInfoParam(task_id, "function_id", "A010070001")
    tgw.SetThirdInfoParam(task_id, "market_code", "600000.SH")
    tgw.QueryThirdInfo(task_id, query_spi=spi_on_response)

    assert wait_event.wait(5), "异步查询没有回调，业务层会永久阻塞"
    data, status = calls[0]
    assert data is None, "未实现的查询不得回传假数据"
    assert isinstance(status, str), (
        "失败必须用字符串状态——业务层只有 isinstance(status, str) "
        "这一个分支会把失败记进 error_list"
    )
    # 失败的任务必须已出表，不会二次回调
    assert task_id not in tgw.pending_task_ids()


def test_6_sync_query_raises_instead_of_empty() -> None:
    """判据 6：同步查询抛 NotConnected，不返回空数据冒充成功。

    `base_data.get_code_list` 拿到返回值后丢掉 error 直接
    `code_table_df['security_code']`，回 None 会在几十行外炸成
    「NoneType not subscriptable」，看不出是谁没实现。
    """
    _ensure_installed()
    import tgw

    item = tgw.SubCodeTableItem()
    item.market = tgw.MarketType.kSSE
    item.security_code = ""
    try:
        tgw.QuerySecuritiesInfo(item)
    except tgw_module.NotConnected as exc:
        assert "QuerySecuritiesInfo" in str(exc)
    else:
        raise AssertionError("同步查询未实现时必须抛 NotConnected，不能返回空数据")


def test_7_login_without_transport_fails_loudly() -> None:
    """判据 7：没接传输层时 Login 返回 False，不假装登录成功。"""
    _ensure_installed()
    import tgw

    cfg = tgw.Cfg()
    cfg.username = "someuser"
    cfg.password = "somepassword"
    cfg.server_vip = "101.230.159.234"
    cfg.server_port = 8600
    assert tgw.Login(cfg, tgw.ApiMode.kInternetMode) is False
    # 参数不全也必须拒
    empty = tgw.Cfg()
    assert tgw.Login(empty, tgw.ApiMode.kInternetMode) is False


def test_8_deliver_response_reaches_callback() -> None:
    """判据 8：接上传输层后，登录握手和响应投递都真的跑通。"""
    _ensure_installed()
    import tgw

    sent: list[bytes] = []

    class FakeTransport:
        """收到认证帧就回报登录成功——真实传输层由服务端响应驱动。"""

        def send(self, payload: bytes) -> None:
            sent.append(payload)
            if len(sent) == 1:  # 只对第一帧（认证帧）回报，查询帧不回
                threading.Thread(
                    target=tgw_module.deliver_logon, args=(True, "ok"), daemon=True
                ).start()

        def close(self) -> None:
            pass

    # 先断言未接传输层时登录失败，接上后成功
    cfg = tgw.Cfg()
    cfg.username, cfg.password = "u", "p"
    cfg.server_vip, cfg.server_port = "101.230.159.234", 8600
    assert tgw.Login(cfg, tgw.ApiMode.kInternetMode) is False

    tgw_module.set_transport(FakeTransport())
    try:
        assert tgw.Login(cfg, tgw.ApiMode.kInternetMode) is True
        assert sent, "登录应当真的把认证帧发出去"
        assert all(isinstance(f, bytes) and f for f in sent), "认证帧应当是字节"

        got: list[tuple] = []
        task_id = tgw.GetTaskID()
        tgw.SetThirdInfoParam(task_id, "function_id", "A010010001")
        tgw.SetThirdInfoParam(task_id, "market_code", "A")
        tgw.QueryThirdInfo(task_id, query_spi=lambda d, s: got.append((d, s)))

        assert sent[-1] != sent[0], "查询帧应当另发一次"
        assert b"A010010001" in sent[-1], "function_id 应当进了请求体"
        assert task_id in tgw.pending_task_ids(), "在途任务应当留在任务表里"

        tgw_module.deliver_response(task_id, [{"MARKET_CODE": "A"}], None)
        # SPI 拿到的是 **DataFrame**，不是原始记录列表。业务层的
        # `HistStockStatusSpi.OnResponse` 原样存进 `EnvXxx.data[code]`，
        # 消费方紧接着就 `.empty` / `.astype`——给 list 必炸。
        # 证据：info_spi.dis.txt:311（line 311 原样存）+
        # download_info_data.dis.txt:1872（line 1623 读 `.empty`）。
        assert len(got) == 1
        data, status = got[0]
        import pandas as pd

        assert isinstance(data, pd.DataFrame)
        assert data["MARKET_CODE"].tolist() == ["A"]
        assert status is None
        assert task_id not in tgw.pending_task_ids(), "响应到达后任务应当出表"
    finally:
        tgw_module.set_transport(None)
        tgw.Close()


def _run_all() -> int:
    """没有 pytest 时的独立跑法。"""
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()
        else:
            print(f"OK    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(_run_all())
    finally:
        if _installed:
            uninstall()
