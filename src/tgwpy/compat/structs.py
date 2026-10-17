"""tgw 对外暴露的类：配置、请求、SPI 基类、行情数据结构。

字段名和字段集合逐个抄自两处权威来源，不做增删：

* `build/tgw-1.0.9.2-py3-none-any/tgw/server_spi.py` —— `ILogSpi` / `IPushSpi`，
  厂商就是纯 Python 写的，没有原生依赖，照抄即可。
* `build/tgw-1.0.9.2-py3-none-any/tgw/base_struct.py` 与
  `linux_py313_x64_package/tgw.py` 的 `__swig_setmethods__` —— 其余结构的字段。

SWIG 的 `new_X()` 走 calloc，默认值全 0；字符串字段 SWIG 侧是定长 char 数组，
Python 侧表现为 `""`。默认值按这个还原。
"""

from __future__ import annotations

__all__ = [
    "TGWBaseClass",
    "ILogSpi",
    "IPushSpi",
    "IGMDSpi",
    "ColocaCfg",
    "Cfg",
    "UpdatePassWordReq",
    "SubscribeItem",
    "SubCodeTableItem",
    "ReqKline",
    "ReqDefault",
    "MDKLine",
    "MDSnapshotL1",
    "MDSnapshotL2",
    "MDIndexSnapshot",
    "MDOptionSnapshot",
    "MDHKTSnapshot",
    "MDFutureSnapshot",
    "MDTickOrder",
    "MDTickExecution",
    "MDOrderQueue",
    # 简写别名，见文件末尾
    "Kline",
    "Snapshot",
    "SnapshotIndex",
    "SnapshotFuture",
    "SnapshotOption",
    "SnapshotHKT",
    "SnapshotL2",
    "Order",
    "Execution",
    "OrderQueue",
]


class TGWBaseClass:
    """厂商 `TGWBaseClass` 的复刻，只为 `__str__` 便于排查。"""

    def __str__(self) -> str:
        attrs = ", ".join(f"{k}={v!r}" for k, v in vars(self).items())
        return f"<{type(self).__name__}: {attrs}>"


# --- SPI 基类 ---------------------------------------------------------------


class ILogSpi:
    """日志回调基类。厂商 server_spi.py 第 1 行起，纯 Python。"""

    def __init__(self) -> None:
        pass

    def OnLog(self, level, log, len):  # noqa: A002 - 参数名跟厂商一致
        pass

    def OnLogon(self, data):
        pass

    def OnIndicator(self, indicator):
        pass

    def OnEvent(self, level, code, event_msg):
        pass


class IPushSpi:
    """行情推送回调基类。厂商 server_spi.py 第 16 行起，纯 Python。

    `SubscribeData(tgw.IPushSpi)`（on_data.pyc 模块级字节码核实）构造后立刻
    `super().__init__()`，所以 `return_df_format` 必须有默认值。
    """

    def __init__(self, return_df_format: bool = True) -> None:
        self._return_df_format = return_df_format

    def SetDfFormat(self, return_df_format: bool = True):
        self._return_df_format = return_df_format

    def _IsDfFormat(self) -> bool:
        return bool(self._return_df_format)

    def OnMDSnapshot(self, data, err):
        pass

    def OnMDTickOrder(self, data, err):
        pass

    def OnMDTickExecution(self, data, err):
        pass

    def OnMDOrderQueue(self, data, err):
        pass

    def OnMDIndexSnapshot(self, data, err):
        pass

    def OnMDOptionSnapshot(self, data, err):
        pass

    def OnMDHKTSnapshot(self, data, err):
        pass

    def OnMDAfterHourFixedPriceSnapshot(self, data, err):
        pass

    def OnMDCSIIndexSnapshot(self, data, err):
        pass

    def OnMDCnIndexSnapshot(self, data, err):
        pass

    def OnMDHKTRealtimeLimit(self, data, err):
        pass

    def OnMDHKTProductStatus(self, data, err):
        pass

    def OnMDHKTVCM(self, data, err):
        pass

    def OnMDFutureSnapshot(self, data, err):
        pass

    def OnKLine(self, data, kline_type, err):
        pass

    def OnSnapshotDerive(self, data, err):
        pass

    def OnFactor(self, data, err):
        pass

    def OnMDOrderBook(self, data, err):
        pass

    def OnMDOrderBookSnapshot(self, data, err):
        pass


class IGMDSpi:
    """`libtgw` 的行情 SPI 基类。虚函数表照抄 SWIG 生成的 `IGMDSpi`。

    厂商版每个方法都直通 C++ 虚表；这里全部是空实现，因为替身不产生推送。
    业务层目前没有直接继承本类（`TgwLogSpi` 继承 `ILogSpi`、`SubscribeData`
    继承 `IPushSpi`，均经 on_data.pyc / tgw_login.pyc 字节码核实），
    保留它是为了让 frames 层和将来的调用方有地方挂。
    """

    def __init__(self) -> None:
        pass

    def OnLog(self, level, log, len):  # noqa: A002
        pass

    def OnLogon(self, data):
        pass

    def OnIndicator(self, indicator, len):  # noqa: A002
        pass

    def OnEvent(self, level, code, event_msg, len):  # noqa: A002
        pass

    def OnMDSnapshot(self, *args):
        pass

    def OnMDIndexSnapshot(self, data, cnt):
        pass

    def OnMDOptionSnapshot(self, data, cnt):
        pass

    def OnMDHKTSnapshot(self, data, cnt):
        pass

    def OnMDAfterHourFixedPriceSnapshot(self, data, cnt):
        pass

    def OnMDCSIIndexSnapshot(self, data, cnt):
        pass

    def OnMDCnIndexSnapshot(self, data, cnt):
        pass

    def OnMDHKTRealtimeLimit(self, data, cnt):
        pass

    def OnMDHKTProductStatus(self, data, cnt):
        pass

    def OnMDHKTVCM(self, data, cnt):
        pass

    def OnMDFutureSnapshot(self, data, cnt):
        pass

    def OnKLine(self, data, cnt, kline_type):
        pass

    def OnSnapshotDerive(self, data, cnt):
        pass

    def OnFactor(self, data):
        pass

    def OnMDOrderBook(self, order_book):
        pass

    def OnMDOrderBookSnapshot(self, order_book_snapshots, cnt):
        pass

    def OnMDTickOrder(self, tick_orders, cnt):
        pass

    def OnMDTickExecution(self, tick_execs, cnt):
        pass

    def OnMDOrderQueue(self, order_queues, cnt):
        pass


# --- 配置 -------------------------------------------------------------------


class ColocaCfg(TGWBaseClass):
    """托管机房模式参数。字段取自 SWIG 的 `ColocaCfg_*_set`。"""

    def __init__(self) -> None:
        self.channel_mode = 0
        self.qtcp_channel_thread = 0
        self.qtcp_req_time_out = 0
        self.qtcp_max_req_cnt = 0
        self.enable_order_book = 0
        self.entry_size = 0
        self.order_queue_size = 0
        self.order_book_deliver_interval_microsecond = 0


class Cfg(TGWBaseClass):
    """登录配置。`set_cfg`（tgw_login.pyc）逐字段赋值，故全部可读写。"""

    def __init__(self) -> None:
        self.server_vip = ""
        self.server_port = 0
        self.username = ""
        self.password = ""
        self.force_logout = False
        self.coloca_cfg = ColocaCfg()


class UpdatePassWordReq(TGWBaseClass):
    """改密请求。"""

    def __init__(self) -> None:
        self.username = ""
        self.old_password = ""
        self.new_password = ""


# --- 请求参数 ----------------------------------------------------------------


class SubscribeItem(TGWBaseClass):
    """订阅项。`on_data.py::run` 写 flag / category_type / security_code / market。"""

    def __init__(self) -> None:
        self.flag = 0
        self.category_type = 0
        self.security_code = ""
        self.market = 0


class SubCodeTableItem(TGWBaseClass):
    """代码表查询项。`base_data.py` 六个查询方法都只写 market / security_code。"""

    def __init__(self) -> None:
        self.market = 0
        self.security_code = ""


class ReqKline(TGWBaseClass):
    """K 线查询。字段取自 SWIG 的 `ReqKline_*_set`，共 12 个。"""

    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.begin_date = 0
        self.end_date = 0
        self.begin_time = 0
        self.end_time = 0
        self.cq_flag = 0
        self.cyc_type = 0
        self.auto_complete = 0
        self.cq_date = 0
        self.cyc_def = 0
        self.qj_flag = 0


class ReqDefault(TGWBaseClass):
    """快照类查询。字段取自 SWIG 的 `ReqDefault_*_set`。"""

    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.date = 0
        self.begin_time = 0
        self.end_time = 0
        self.data_type = 0
        self.level_type = 0


# --- 行情数据结构 -----------------------------------------------------------
# 字段集合抄自 base_struct.py 的 TGW* 系列，并与 utils/convert.py 的
# convert_realtime_* 实际读取的属性逐项对齐（该模块从这些对象上取属性转
# DataFrame）。业务层只读不写，写入由 frames 层的解析器负责。

_DEPTH_FIELDS = ("bid_price", "bid_volume", "offer_price", "offer_volume")


def _init_depth(self, count: int) -> None:
    """在实例上铺开 N 档买卖盘。

    放在 `__init__` 里逐实例赋值，不能挂成类属性——那样所有实例共用同一个
    list，一个实例改了盘口就污染其余的。厂商 base_struct.py 也是每个实例
    新建 list。
    """
    for name in _DEPTH_FIELDS:
        setattr(self, name, [0] * count)


class MDKLine(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.orig_time = 0
        self.kline_time = 0
        self.open_price = 0
        self.high_price = 0
        self.low_price = 0
        self.close_price = 0
        self.volume_trade = 0
        self.value_trade = 0
        self.variety_category = 0


class MDSnapshotL1(TGWBaseClass):
    def __init__(self) -> None:
        self.level_type = 1
        self.market_type = 0
        self.security_code = ""
        self.variety_category = 0
        self.orig_time = 0
        self.trading_phase_code = ""
        self.pre_close_price = 0
        self.open_price = 0
        self.high_price = 0
        self.low_price = 0
        self.last_price = 0
        self.close_price = 0
        self.num_trades = 0
        self.total_volume_trade = 0
        self.total_value_trade = 0
        self.IOPV = 0
        self.high_limited = 0
        self.low_limited = 0
        _init_depth(self, 5)


class MDSnapshotL2(TGWBaseClass):
    def __init__(self) -> None:
        self.level_type = 2
        self.market_type = 0
        self.security_code = ""
        self.orig_time = 0
        self.trading_phase_code = ""
        self.pre_close_price = 0
        self.open_price = 0
        self.high_price = 0
        self.low_price = 0
        self.last_price = 0
        self.close_price = 0
        self.num_trades = 0
        self.total_volume_trade = 0
        self.total_value_trade = 0
        self.total_bid_volume = 0
        self.total_offer_volume = 0
        self.weighted_avg_bid_price = 0
        self.weighted_avg_offer_price = 0
        self.IOPV = 0
        self.yield_to_maturity = 0
        self.high_limited = 0
        self.low_limited = 0
        self.price_earning_ratio1 = 0
        self.price_earning_ratio2 = 0
        self.change1 = 0
        self.change2 = 0
        self.etf_buy_number = 0
        self.etf_buy_amount = 0
        self.etf_buy_money = 0
        self.etf_sell_number = 0
        self.etf_sell_amount = 0
        self.etf_sell_money = 0
        _init_depth(self, 10)


class MDIndexSnapshot(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.orig_time = 0
        self.trading_phase_code = ""
        self.pre_close_index = 0
        self.open_index = 0
        self.high_index = 0
        self.low_index = 0
        self.last_index = 0
        self.close_index = 0
        self.total_volume_trade = 0
        self.total_value_trade = 0
        self.variety_category = 0


class MDOptionSnapshot(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.orig_time = 0
        self.trading_phase_code = ""
        self.total_long_position = 0
        self.total_volume_trade = 0
        self.total_value_trade = 0
        self.pre_settle_price = 0
        self.pre_close_price = 0
        self.open_price = 0
        self.auction_price = 0
        self.auction_volume = 0
        self.high_price = 0
        self.low_price = 0
        self.last_price = 0
        self.close_price = 0
        self.high_limited = 0
        self.low_limited = 0
        self.settle_price = 0
        self.ref_price = 0
        self.contract_type = ""
        self.expire_date = 0
        self.underlying_security_code = ""
        self.exercise_price = 0
        self.variety_category = 0
        _init_depth(self, 5)


class MDHKTSnapshot(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.orig_time = 0
        self.trading_phase_code = ""
        self.total_volume_trade = 0
        self.total_value_trade = 0
        self.pre_close_price = 0
        self.nominal_price = 0
        self.high_price = 0
        self.low_price = 0
        self.last_price = 0
        self.ref_price = 0
        self.high_limited = 0
        self.low_limited = 0
        self.bid_price_limit_up = 0
        self.bid_price_limit_down = 0
        self.offer_price_limit_up = 0
        self.offer_price_limit_down = 0
        self.variety_category = 0
        _init_depth(self, 5)


class MDFutureSnapshot(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.orig_time = 0
        self.action_day = 0
        self.last_price = 0
        self.pre_settle_price = 0
        self.pre_close_price = 0
        self.pre_open_interest = 0
        self.open_price = 0
        self.high_price = 0
        self.low_price = 0
        self.total_volume_trade = 0
        self.total_value_trade = 0
        self.open_interest = 0
        self.close_price = 0
        self.settle_price = 0
        self.high_limited = 0
        self.low_limited = 0
        self.pre_delta = 0
        self.curr_delta = 0
        self.average_price = 0
        self.trading_day = 0
        self.variety_category = 0
        self.latest_volume_trade = 0
        self.init_volume_trade = 0
        self.change_volume_trade = 0
        self.bid_imply_volume = 0
        self.offer_imply_volume = 0
        self.total_bid_volume_trade = 0
        self.total_ask_volume_trade = 0
        self.exchange_inst_id = ""
        _init_depth(self, 5)


class MDTickOrder(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.appl_seq_num = 0
        self.channel_no = 0
        self.order_time = 0
        self.order_price = 0
        self.order_volume = 0
        self.side = ""
        self.order_type = ""
        self.md_stream_id = ""
        self.product_status = ""
        self.orig_order_no = 0
        self.biz_index = 0
        self.traded_order_volume = 0


class MDTickExecution(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.exec_time = 0
        self.channel_no = 0
        self.appl_seq_num = 0
        self.exec_price = 0
        self.exec_volume = 0
        self.value_trade = 0
        self.bid_appl_seq_num = 0
        self.offer_appl_seq_num = 0
        self.side = ""
        self.exec_type = ""
        self.md_stream_id = ""
        self.biz_index = 0
        self.variety_category = 0
        self.cancel_flag = 0


class MDOrderQueue(TGWBaseClass):
    def __init__(self) -> None:
        self.market_type = 0
        self.security_code = ""
        self.order_time = 0
        self.side = ""
        self.order_price = 0
        self.order_volume = 0
        self.num_of_orders = 0
        self.items = 0
        self.volume = [0] * 50
        self.channel_no = 0
        self.md_stream_id = ""


# --- 简写别名 ---------------------------------------------------------------
# 逆向记录把这些短名列为「tgw 需要提供的数据结构」。实际上
# AmazingData.utils.constant 里的 Kline / Snapshot / SnapshotIndex 等是它
# 自己的 pydantic 模型（constant.pyc 模块级 IMPORT_FROM 核实），
# 从来没从 tgw 取过。tgw 侧的正式名是上面的 MD* 系列，短名是任务书里的
# 简称。留别名是为了让 frames 层按任务书的名字写也能对上同一个类。

Kline = MDKLine
Snapshot = MDSnapshotL1
SnapshotL2 = MDSnapshotL2
SnapshotIndex = MDIndexSnapshot
SnapshotFuture = MDFutureSnapshot
SnapshotOption = MDOptionSnapshot
SnapshotHKT = MDHKTSnapshot
Order = MDTickOrder
Execution = MDTickExecution
OrderQueue = MDOrderQueue
