"""对外常量。`Period` 的值就是线上 `MDDatatype` 的值（厂商 `utils/constant.pyc`
把每个成员直接绑到 `tgw.MDDatatype.kXxx`），行情查询原样传给协议层。

其余的 pydantic 模型是行情族对外的数据模型：字段名、字段顺序、类型标注
照厂商 `utils/constant.pyc` 抄，行情查询用它定输出列（`Kline.model_fields`
给 K 线表定列序）。厂商这些字段多数写成 `Optional[X]` 却不给默认值，
在 pydantic 2 里等于必填、可以不填；这里原样保留，没有实例化它们的地方。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, unique
from typing import Optional

from pydantic import BaseModel

from tgwpy.compat import tgw_module as _tgw

_MD = _tgw.MDDatatype


@unique
class Period(Enum):
    snapshotL2 = 20001
    order = 20002
    execution = 20003
    order_queue = 20004
    snapshotHKT = 20005
    snapshotfuture = 20007
    snapshotoption = 20008
    snapshot = _MD.kSnapshot
    day = _MD.kDayKline
    min1 = _MD.k1KLine
    min3 = _MD.k3KLine
    min5 = _MD.k5KLine
    min10 = _MD.k10KLine
    min15 = _MD.k15KLine
    min30 = _MD.k30KLine
    min60 = _MD.k60KLine
    min120 = _MD.k120KLine
    week = _MD.kWeekKline
    month = _MD.kMonthKline
    season = _MD.kSeasonKline
    year = _MD.kYearKline


class Order(BaseModel):
    code: str
    order_time: Optional[datetime]
    appl_seq_num: Optional[int]
    channel_no: Optional[int]
    order_price: Optional[float]
    order_volume: Optional[int]
    side: str
    order_type: str
    md_stream_id: str
    product_status: str
    orig_order_no: Optional[int]
    biz_index: Optional[int]


class Execution(BaseModel):
    code: str
    exec_time: Optional[datetime]
    channel_no: Optional[int]
    appl_seq_num: Optional[int]
    exec_price: Optional[float]
    exec_volume: Optional[int]
    value_trade: Optional[float]
    bid_appl_seq_num: Optional[int]
    offer_appl_seq_num: Optional[int]
    side: str
    exec_type: str
    md_stream_id: str
    biz_index: Optional[int]


class OrderQueue(BaseModel):
    code: str
    order_time: Optional[datetime]
    side: str
    order_price: Optional[float]
    order_volume: Optional[int]
    num_of_orders: Optional[int]
    items: Optional[int]
    volume1: Optional[int]
    volume2: Optional[int]
    volume3: Optional[int]
    volume4: Optional[int]
    volume5: Optional[int]
    volume6: Optional[int]
    volume7: Optional[int]
    volume8: Optional[int]
    volume9: Optional[int]
    volume10: Optional[int]
    volume11: Optional[int]
    volume12: Optional[int]
    volume13: Optional[int]
    volume14: Optional[int]
    volume15: Optional[int]
    volume16: Optional[int]
    volume17: Optional[int]
    volume18: Optional[int]
    volume19: Optional[int]
    volume20: Optional[int]
    volume21: Optional[int]
    volume22: Optional[int]
    volume23: Optional[int]
    volume24: Optional[int]
    volume25: Optional[int]
    volume26: Optional[int]
    volume27: Optional[int]
    volume28: Optional[int]
    volume29: Optional[int]
    volume30: Optional[int]
    volume31: Optional[int]
    volume32: Optional[int]
    volume33: Optional[int]
    volume34: Optional[int]
    volume35: Optional[int]
    volume36: Optional[int]
    volume37: Optional[int]
    volume38: Optional[int]
    volume39: Optional[int]
    volume40: Optional[int]
    volume41: Optional[int]
    volume42: Optional[int]
    volume43: Optional[int]
    volume44: Optional[int]
    volume45: Optional[int]
    volume46: Optional[int]
    volume47: Optional[int]
    volume48: Optional[int]
    volume49: Optional[int]
    volume50: Optional[int]
    channel_no: Optional[int]
    md_stream_id: str


class SnapshotL2(BaseModel):
    code: str
    trade_time: Optional[datetime]
    pre_close: Optional[float]
    last: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[int]
    amount: Optional[float]
    num_trades: Optional[int]
    high_limited: Optional[float]
    low_limited: Optional[float]
    weighted_avg_bid_price: Optional[float]
    weighted_avg_offer_price: Optional[float]
    change1: Optional[float]
    change2: Optional[float]
    ask_price1: Optional[float]
    ask_volume1: Optional[int]
    bid_price1: Optional[float]
    bid_volume1: Optional[int]
    ask_price2: Optional[float]
    ask_volume2: Optional[int]
    bid_price2: Optional[float]
    bid_volume2: Optional[int]
    ask_price3: Optional[float]
    ask_volume3: Optional[int]
    bid_price3: Optional[float]
    bid_volume3: Optional[int]
    ask_price4: Optional[float]
    ask_volume4: Optional[int]
    bid_price4: Optional[float]
    bid_volume4: Optional[int]
    ask_price5: Optional[float]
    ask_volume5: Optional[int]
    bid_price5: Optional[float]
    bid_volume5: Optional[int]
    ask_price6: Optional[float]
    ask_volume6: Optional[int]
    bid_price6: Optional[float]
    bid_volume6: Optional[int]
    ask_price7: Optional[float]
    ask_volume7: Optional[int]
    bid_price7: Optional[float]
    bid_volume7: Optional[int]
    ask_price8: Optional[float]
    ask_volume8: Optional[int]
    bid_price8: Optional[float]
    bid_volume8: Optional[int]
    ask_price9: Optional[float]
    ask_volume9: Optional[int]
    bid_price9: Optional[float]
    bid_volume9: Optional[int]
    ask_price10: Optional[float]
    ask_volume10: Optional[int]
    bid_price10: Optional[float]
    bid_volume10: Optional[int]
    iopv: Optional[float]
    trading_phase_code: str


class SnapshotOption(BaseModel):
    code: str
    trade_time: Optional[datetime]
    trading_phase_code: str
    total_long_position: Optional[int]
    amount: Optional[float]
    volume: Optional[int]
    pre_settle: Optional[float]
    pre_close: Optional[float]
    auction_price: Optional[float]
    auction_volume: Optional[int]
    last: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    settle: Optional[float]
    high_limited: Optional[float]
    low_limited: Optional[float]
    ask_price1: Optional[float]
    ask_volume1: Optional[int]
    bid_price1: Optional[float]
    bid_volume1: Optional[int]
    ask_price2: Optional[float]
    ask_volume2: Optional[int]
    bid_price2: Optional[float]
    bid_volume2: Optional[int]
    ask_price3: Optional[float]
    ask_volume3: Optional[int]
    bid_price3: Optional[float]
    bid_volume3: Optional[int]
    ask_price4: Optional[float]
    ask_volume4: Optional[int]
    bid_price4: Optional[float]
    bid_volume4: Optional[int]
    ask_price5: Optional[float]
    ask_volume5: Optional[int]
    bid_price5: Optional[float]
    bid_volume5: Optional[int]
    ref_price: Optional[float]
    contract_type: str
    expire_date: str
    underlying_security_code: str
    exercise_price: Optional[float]


class SnapshotFuture(BaseModel):
    code: str
    trade_time: Optional[datetime]
    action_day: str
    last: Optional[float]
    pre_settle: Optional[float]
    pre_close: Optional[float]
    pre_open_interest: Optional[int]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    volume: Optional[int]
    amount: Optional[float]
    open_interest: Optional[int]
    close: Optional[float]
    settle: Optional[float]
    high_limited: Optional[float]
    low_limited: Optional[float]
    ask_price1: Optional[float]
    ask_volume1: Optional[int]
    bid_price1: Optional[float]
    bid_volume1: Optional[int]
    ask_price2: Optional[float]
    ask_volume2: Optional[int]
    bid_price2: Optional[float]
    bid_volume2: Optional[int]
    ask_price3: Optional[float]
    ask_volume3: Optional[int]
    bid_price3: Optional[float]
    bid_volume3: Optional[int]
    ask_price4: Optional[float]
    ask_volume4: Optional[int]
    bid_price4: Optional[float]
    bid_volume4: Optional[int]
    ask_price5: Optional[float]
    ask_volume5: Optional[int]
    bid_price5: Optional[float]
    bid_volume5: Optional[int]
    average_price: Optional[float]
    trading_day: str


class SnapshotHKT(BaseModel):
    code: str
    trade_time: Optional[datetime]
    pre_close: Optional[float]
    nominal_price: Optional[float]
    ref_price: Optional[float]
    bid_price_limit_up: Optional[float]
    bid_price_limit_down: Optional[float]
    offer_price_limit_up: Optional[float]
    offer_price_limit_down: Optional[float]
    last: Optional[float]
    high: Optional[float]
    low: Optional[float]
    volume: Optional[int]
    amount: Optional[float]
    high_limited: Optional[float]
    low_limited: Optional[float]
    ask_price1: Optional[float]
    ask_volume1: Optional[int]
    bid_price1: Optional[float]
    bid_volume1: Optional[int]
    ask_price2: Optional[float]
    ask_volume2: Optional[int]
    bid_price2: Optional[float]
    bid_volume2: Optional[int]
    ask_price3: Optional[float]
    ask_volume3: Optional[int]
    bid_price3: Optional[float]
    bid_volume3: Optional[int]
    ask_price4: Optional[float]
    ask_volume4: Optional[int]
    bid_price4: Optional[float]
    bid_volume4: Optional[int]
    ask_price5: Optional[float]
    ask_volume5: Optional[int]
    bid_price5: Optional[float]
    bid_volume5: Optional[int]
    trading_phase_code: str


class Snapshot(BaseModel):
    code: str
    trade_time: Optional[datetime]
    pre_close: Optional[float]
    last: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[int]
    amount: Optional[float]
    num_trades: Optional[int]
    high_limited: Optional[float]
    low_limited: Optional[float]
    ask_price1: Optional[float]
    ask_volume1: Optional[int]
    bid_price1: Optional[float]
    bid_volume1: Optional[int]
    ask_price2: Optional[float]
    ask_volume2: Optional[int]
    bid_price2: Optional[float]
    bid_volume2: Optional[int]
    ask_price3: Optional[float]
    ask_volume3: Optional[int]
    bid_price3: Optional[float]
    bid_volume3: Optional[int]
    ask_price4: Optional[float]
    ask_volume4: Optional[int]
    bid_price4: Optional[float]
    bid_volume4: Optional[int]
    ask_price5: Optional[float]
    ask_volume5: Optional[int]
    bid_price5: Optional[float]
    bid_volume5: Optional[int]
    iopv: Optional[float]
    trading_phase_code: str


class SnapshotIndex(BaseModel):
    code: str
    trade_time: Optional[datetime]
    last: Optional[float]
    pre_close: Optional[float]
    high: Optional[float]
    open: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[int]
    amount: Optional[float]
    trading_phase_code: str


class Kline(BaseModel):
    code: str
    kline_time: Optional[datetime]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[int]
    amount: Optional[float]
