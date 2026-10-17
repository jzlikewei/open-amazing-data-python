"""复权因子（`QueryExFactorTable`）在 tgw 替身上的验收测试。

**判据来源分三类，别混。**

* 请求字节：探针实发到真实网关的**报文原文**
  （抓包原文 + 与参照实现逐字段对账），逐字段相等。
* 返回 DataFrame 的列与量纲：厂商中间层 `tgw/tmp_spi.py:963-972`
  （`json_normalize(Tools_ExFactorTableToJson(...))`）+ 业务层字节码
  `download_info_data.dis.txt:12179-12204`（`set_index(['ex_date'])` →
  `sort_index` → `pd.DataFrame(adj_factor['cum_factor'])`）。
* 数值：R1 §1.2 的真实回包原文 + §1.4 的 HDF5 逐点对账表。

不碰网络：`send_market` 是记账传输层，回包由测试自己调
`deliver_response` 注入。

跑法（worktree 里没有 .venv，借主仓库的）：

    .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy.compat import tgw_module as tm  # noqa: E402
from tgwpy.frames import market  # noqa: E402

# --- 抓包 / 探针原文（真实网关实证）----------------------------------------

#: 探针实发的报文原文，`headers.id` 是运行时任务号，其余逐字照抄。
CAPTURED_EX_FACTOR_PARAMS = {"security_code": "600519"}

#: 真实回包的 4 条 CSV 原文（R1 §1.2 的首尾样例 + 中间两条）。
RAW_EX_FACTOR_ROWS = [
    "600519.SH,600519,20010827,1.000000000000000000,1.000000000000000000",
    "600519.SH,600519,20020725,1.118280000000000000,1.118280000000000000",
    "600519.SH,600519,20030714,1.108714275494509425,1.239853000000000000",
    "600519.SH,600519,20260626,1.023663926756412223,8.646881000000000000",
]

#: R1 §1.4 逐点对账表里网关回的 cum_factor，HDF5 里的值与它逐点相等。
REFERENCE_CUM_FACTOR = {
    20010827: 1.0,
    20020725: 1.11828,
    20030714: 1.239853,
    20260626: 8.646881,
}

#: R1 §1.5 端到端对账用到的当日因子（2026-04-20，前向填充落在 2003-07-14 之后
#: 的阶梯区间里）与末值因子。
FACTOR_ON_20260420 = 8.446992
FACTOR_LAST = 8.646881


# --- 测试替身：记账传输层 + 已登录会话 ------------------------------------


class _FakeTransport:
    """只记账，不联网。`send_market` 是行情通道的硬契约。"""

    def __init__(self) -> None:
        self.market: list[bytes] = []
        self.info: list[bytes] = []
        self.raise_on_market: Exception | None = None

    def send(self, payload: bytes) -> None:
        self.info.append(payload)

    def send_market(self, payload: bytes) -> None:
        if self.raise_on_market is not None:
            raise self.raise_on_market
        self.market.append(payload)


@pytest.fixture
def wire(monkeypatch):
    """装好一个已登录的会话，产出记账传输层。测完还原全局状态。"""
    tm._SESSION.reset()
    transport = _FakeTransport()
    tm.set_transport(transport)
    tm._SESSION.logged_in = True
    tm._SESSION.username = "600001"
    tm._SESSION.token = "TOKEN"
    tm._SESSION.api_mode = tm.ApiMode.kInternetMode
    yield transport
    tm._SESSION.reset()


def _run_sync(fn, *args, rows, delay=0.05, **kwargs):
    """起线程发同步查询，主线程拿到 task_id 后注入回包。

    回包必须由另一条线程注入，否则同步查询自己就在等自己。
    """
    box: dict[str, object] = {}

    def _call():
        box["result"] = fn(*args, **kwargs)

    worker = threading.Thread(target=_call)
    worker.start()
    deadline = time.time() + 5.0
    while not tm.pending_task_ids() and time.time() < deadline:
        time.sleep(0.005)
    assert tm.pending_task_ids(), "查询没能登记进任务表"
    time.sleep(delay)
    tm.deliver_response(tm.pending_task_ids()[0], rows, None)
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "同步查询没有返回"
    return box["result"]


# --- 判据 1：请求字节与探针报文原文逐字段相等 ------------------------------


def test_ex_factor_wire_matches_capture_field_by_field(wire):
    """`ReqGetExFactor` 的 params 必须与探针实发的报文逐字段相等。"""
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)

    assert len(wire.market) == 1
    payload = json.loads(wire.market[0])
    assert payload["method"] == "ReqGetExFactor"
    assert payload["params"] == CAPTURED_EX_FACTOR_PARAMS


def test_wire_carries_id_in_headers_with_session_credentials(wire):
    """`id` 在 `headers` 里（顶层不放），并带上会话凭据。"""
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)
    payload = json.loads(wire.market[0])

    assert "id" not in payload
    assert payload["headers"]["id"] == tm.pending_task_ids()[0]
    assert payload["headers"]["userName"] == "600001"
    assert payload["headers"]["token"] == "TOKEN"


def test_online_method_is_ex_factor_and_carries_no_function_id(wire):
    """**线上 method 是 `ReqGetExFactor`，且没有 function_id。**

    这条钉死两件事，防的是同一个错误：把它当成 `ReqGetThirdInfo` 体系里
    某个 function_id——先 `SetThirdInfoParam(task_id, "function_id", ...)`，
    再让 `encode_third_info_request` 拼出 `params.item` 数组。那条路线上
    根本没有这个 method（R1 §1.1：MDGA 日志里 `Query ExFactor` 与
    `Query third_info` 是**两个并列的日志分类**），发出去只会静默失败。

    判据取三处：method 名、`params` 的键集合、以及 params 里不出现
    `function_id` / `item` 这两个三方资讯体系的键。
    """
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)
    payload = json.loads(wire.market[0])

    assert payload["method"] == "ReqGetExFactor"
    assert set(payload["params"]) == {"security_code"}
    assert "function_id" not in payload["params"]
    assert "item" not in payload["params"]
    assert "function_id" not in payload and "params" in payload


def test_ex_factor_goes_out_on_market_channel_not_info_channel(wire):
    """走行情通道 `send_market`，不是三方资讯的 `send`。"""
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)

    assert len(wire.market) == 1
    assert wire.info == [], "复权因子不是三方资讯，不该走资讯通道"


def test_bare_security_code_is_passed_through_untouched(wire):
    """裸代码原样送线上——带 `.SH` 后缀实测回 `status=-100` / DataEmpty。

    业务层 `get_backward_factor_data` 用
    `market_type, security_code = get_tgw_type_code(code)` 拆开，传进来的
    本来就是裸码。替身不替它加后缀，也不替它剥。
    """
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)
    assert json.loads(wire.market[0])["params"]["security_code"] == "600519"


# --- 判据 2：CSV 5 列解码，精度不丢 ----------------------------------------


def test_csv_field_order_matches_ex_factor_fields():
    """CSV 段序 = `EX_FACTOR_FIELDS`（`Serialize(MDExFactorTable)` 的 5 项）。"""
    assert tuple(name for name, _ in market.EX_FACTOR_CSV_KINDS) == \
        market.EX_FACTOR_FIELDS
    assert market.EX_FACTOR_FIELDS == (
        "inner_code", "security_code", "ex_date", "ex_factor", "cum_factor")


def test_decode_five_columns_correctly():
    """5 段逐项对上：类型、取值、日期不补零。"""
    rec = market.decode_ex_factor_rows([RAW_EX_FACTOR_ROWS[-1]])[0]

    assert rec["inner_code"] == "600519.SH"
    assert rec["security_code"] == "600519"
    assert rec["ex_date"] == 20260626
    assert rec["ex_date"] == 20260626 and isinstance(rec["ex_date"], int)
    assert rec["ex_factor"] == 1.023663926756412223
    assert rec["cum_factor"] == 8.646881


def test_ex_date_is_int_so_it_can_index_the_calendar():
    """`ex_date` 必须是 int。

    业务层 `backward_factor.loc[calendar_index]`，而 `calendar` 是
    `list(TRADE_DAYS.astype(int))`（`base_data.dis.txt` `get_calendar`
    第 50/53 行）。两边不同类型域，`.loc` 取空，整张表被 `fillna(1)`
    填成 1.0——复权静默失效。
    """
    rec = market.decode_ex_factor_rows(RAW_EX_FACTOR_ROWS)[0]
    assert type(rec["ex_date"]) is int


def test_eighteen_digit_decimals_are_not_scaled_or_rounded():
    """18 位小数按 float 原样解析：不套定点缩放，也不截到固定位数。

    把 `1.023663926756412223` 截成 6 位小数（1.023664）会让复权价在
    第 6 位上出错，所以这里钉住的是**没有中间量化**。
    """
    rec = market.decode_ex_factor_rows([RAW_EX_FACTOR_ROWS[2]])[0]

    assert rec["ex_factor"] == float("1.108714275494509425")
    assert rec["cum_factor"] == float("1.239853")
    # 少一位有效数字就挂：1.108714（截断）≠ 1.1087142754945094
    assert rec["ex_factor"] != 1.108714


def test_raw_text_columns_preserve_every_digit():
    """`*_text` 留着线上的字面量，18 位一个不丢。

    float64 只留 15~17 位有效数字，十进制字面量只在 CSV 里存在一次，
    转成 float 就回不去了。要精确十进制的调用方从这两列重解析。
    """
    rec = market.decode_ex_factor_rows(RAW_EX_FACTOR_ROWS)[2]

    assert rec["ex_factor_text"] == "1.108714275494509425"
    assert rec["cum_factor_text"] == "1.239853000000000000"

    from decimal import Decimal
    assert Decimal(rec["ex_factor_text"]) == Decimal("1.108714275494509425")
    assert Decimal(rec["cum_factor_text"]) == Decimal("1.239853000000000000")


def test_cum_factor_values_match_gateway_reference():
    """解码出的 cum_factor 与 R1 §1.4 的网关真值逐点相等。

    那一节是拿这 4 个点跟 HDF5 里的值对过账的（全部相等）。
    """
    decoded = market.decode_ex_factor_rows(RAW_EX_FACTOR_ROWS)

    assert {rec["ex_date"]: rec["cum_factor"] for rec in decoded} == \
        REFERENCE_CUM_FACTOR


def test_malformed_rows_are_skipped_not_padded():
    """段数对不上就跳过，不硬凑——与 `decode_kline_rows` 同一判据。"""
    rows = [
        RAW_EX_FACTOR_ROWS[0],
        "600519.SH,600519,20020725,1.11828",        # 少一段
        "600519.SH,600519,20030714,1.1,1.2,extra",  # 多一段
        None,                                        # 非字符串
        RAW_EX_FACTOR_ROWS[-1],
    ]
    decoded = market.decode_ex_factor_rows(rows)

    assert [rec["ex_date"] for rec in decoded] == [20010827, 20260626]


def test_merged_multi_frame_rows_decode_in_one_pass(wire):
    """分包收齐后合并成的那一整串，一次性解完，不留半截状态。

    网关对 600519 回 32 条（R1 §1.2）。分包的判据是 `headers.all_pack_num`，
    合并在 `wire.WssTransport` 那一侧做（`wire.py:257-262`，已被
    `tests/test_wire_market.py` 覆盖）；替身这一层收到的是拼好的扁平
    列表，逐行解码之间没有共享状态。
    """
    rows = [f"600519.SH,600519,20010827,1.0,1.0"] * 20
    rows += [
        "600519.SH,600519,20020725,1.11828,1.11828",
        "600519.SH,600519,20030714,1.108714275494509425,1.239853",
        "600519.SH,600519,20260626,1.023663926756412223,8.646881",
    ]
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)
    tm.deliver_response(tm.pending_task_ids()[0], rows, None)

    result, _ = _run_sync(tm.QueryExFactorTable, "600519", rows=rows)
    assert len(result) == len(rows) == 23
    assert result["cum_factor"].iloc[-1] == 8.646881


# --- 判据 3：同步形态（query_spi=None）返回 DataFrame -----------------------


def test_sync_mode_returns_dataframe(wire):
    """`query_spi=None` 时阻塞等回包，返回 `(DataFrame, None)`。"""
    result, err = _run_sync(tm.QueryExFactorTable, "600519", rows=RAW_EX_FACTOR_ROWS)

    assert err is None
    assert result.__class__.__name__ == "DataFrame"
    assert len(result) == 4
    assert list(result.columns) == list(tm._EX_FACTOR_COLUMNS)


def test_sync_mode_matches_gram_of_vendor_middle_layer(wire):
    """列与量纲照厂商中间层 `TmpQueryExFactorSpi` 的分岔。

    `tgw/tmp_spi.py:963-972`：`return_df_format=True` →
    `json_normalize(json_list)`，否则给 `json_list`。判据是业务层那两句
    ——`set_index(['ex_date'])` 要 `ex_date` 是列名，`adj_factor['cum_factor']`
    要因子列在。
    """
    result, _ = _run_sync(tm.QueryExFactorTable, "600519", rows=RAW_EX_FACTOR_ROWS)

    assert "ex_date" in result.columns
    assert "cum_factor" in result.columns
    assert result["ex_date"].dtype.kind == "i"
    assert result["cum_factor"].dtype.kind == "f"
    assert result["ex_factor"].dtype.kind == "f"


def test_both_factor_columns_present_for_two_business_paths(wire):
    """`cum_factor` 和 `ex_factor` 都要有——**同一个 API 供两条业务路**。

    `download_backward_factor` 取 `cum_factor`
    （`download_info_data.dis.txt:12179`），`download_adj_factor` 取
    `ex_factor`（同文件 `:12746`），两者调的都是 `QueryExFactorTable`。
    少一列，另一条路 KeyError。
    """
    result, _ = _run_sync(tm.QueryExFactorTable, "600519", rows=RAW_EX_FACTOR_ROWS)
    first = result.to_dict("records")[0]

    assert first["cum_factor"] == 1.0 and first["ex_factor"] == 1.0
    last = result.to_dict("records")[-1]
    assert last["cum_factor"] == 8.646881
    assert last["ex_factor"] == 1.023663926756412223


def test_return_df_format_false_gives_record_list(wire):
    """`return_df_format=False` 出记录列表，官方中间层也是这个分岔。

    注意业务层**认得**这个形态：`download_backward_factor` 里
    `isinstance(adj_factor, list)` 会把它当空结果处理
    （`download_info_data.dis.txt:12170-12188`）。业务层自己从不传
    `False`，替身照实实现官方分岔即可。
    """
    result, err = _run_sync(tm.QueryExFactorTable, "600519",
                            rows=RAW_EX_FACTOR_ROWS, return_df_format=False)

    assert err is None
    assert isinstance(result, list) and len(result) == 4
    assert list(result[0]) == list(tm._EX_FACTOR_COLUMNS)
    assert result[-1]["cum_factor"] == 8.646881


# --- 判据 4：异步形态（有 query_spi）走回调 --------------------------------


def test_async_mode_calls_back_with_dataframe(wire):
    """给了 `query_spi` 就走回调，返回值只表示发出去了。"""
    seen: list = []
    ok, err = tm.QueryExFactorTable("600519", query_spi=lambda d, s: seen.append((d, s)))

    assert (ok, err) == (True, None)
    assert len(seen) == 0, "回包还没到，不该有回调"

    tm.deliver_response(tm.pending_task_ids()[0], RAW_EX_FACTOR_ROWS, None)
    assert len(seen) == 1
    data, status = seen[0]

    assert status is None
    assert data.__class__.__name__ == "DataFrame"
    assert len(data) == 4
    assert data.to_dict("records")[-1]["cum_factor"] == 8.646881


def test_async_callback_exception_does_not_escape(wire):
    """业务层回调抛异常只记日志，不能把传输层的接收线程带崩。"""
    def boom(data, status):
        raise RuntimeError("业务层自己的问题")

    tm.QueryExFactorTable("600519", query_spi=boom)
    tm.deliver_response(tm.pending_task_ids()[0], RAW_EX_FACTOR_ROWS, None)  # 不抛即通过


# --- 判据 5：业务层的接法（把字节码当规格）--------------------------------


def test_business_layer_forward_fill_recipe_reproduces_reference_factor(wire):
    """照 `download_backward_factor` 的字节码跑一遍，得到 R1 §1.5 的因子。

    业务流程（`download_info_data.dis.txt:12179-12338`）：
    `set_index(['ex_date'])` → `sort_index` → 取 `cum_factor` 列改名成代码
    → 按日历补 NaN 尾巴 → `ffill` → `fillna(1)` → `.loc[calendar_index]`。

    端到端判据是 R1 §1.5 那个数：`1410.89 × (8.446992/8.646881)`
    逐位等于参照 CLI 的 `1378.274611`。本测试只走到因子那一步——
    复权乘法在 `cli.py` 那边，不在本文件的范围内。
    """
    import pandas as pd

    result, _ = _run_sync(tm.QueryExFactorTable, "600519", rows=RAW_EX_FACTOR_ROWS)

    factor = result.set_index(["ex_date"], inplace=False).sort_index()
    factor_code = pd.DataFrame(factor["cum_factor"])
    factor_code.columns = ["600519.SH"]

    # 日历只取到网关回包覆盖的区间，够验证前向填充的阶梯语义
    calendar_index = [20010827, 20020725, 20030714, 20030801, 20260626, 20260629]
    padded = pd.concat([
        factor_code,
        pd.DataFrame(float("nan"), columns=factor_code.columns,
                     index=sorted(set(calendar_index) - set(factor_code.index))),
    ])
    padded = padded.sort_index().ffill().fillna(1).loc[calendar_index]

    # 阶梯语义：`cum_factor` 只在除权日跳变，其间的交易日沿用上一档。
    # 20030801 落在 2003-07-14 那一档（1.239853），不是末值。
    assert padded["600519.SH"].tolist() == [
        1.0, 1.11828, 1.239853, 1.239853, 8.646881, 8.646881]
    assert padded["600519.SH"].iloc[-1] == FACTOR_LAST

    # R1 §1.5 的当日因子落在 2003-07-14 之后、2026-06-26 之前的阶梯区间里，
    # 这 4 条样例里没有那一天，单独验乘法关系（末值归一）
    multiplier = FACTOR_ON_20260420 / padded["600519.SH"].iloc[-1]
    assert round(1410.89 * multiplier, 6) == 1378.274611


def test_business_layer_calendar_lookup_finds_every_calendar_day(wire):
    """`.loc[calendar_index]` 取得到值：int 的 `ex_date` 与 int 的日历同域。

    取不到就是 NaN → `fillna(1)` → 因子恒为 1.0 → `--adjust` 静默不生效。
    """
    import pandas as pd

    result, _ = _run_sync(tm.QueryExFactorTable, "600519", rows=RAW_EX_FACTOR_ROWS)
    factor = result.set_index(["ex_date"]).sort_index()
    calendar_index = [20010827, 20020725, 20030714, 20260626]

    assert factor["cum_factor"].loc[calendar_index].notna().all()
    assert factor["cum_factor"].loc[calendar_index].tolist() == \
        list(REFERENCE_CUM_FACTOR.values())


# --- 判据 6：三条失败路径 × 两种形态 --------------------------------------


@pytest.mark.parametrize("break_session, expect", [
    (lambda: setattr(tm._SESSION, "logged_in", False), "未登录"),
    (lambda: tm._SESSION.set_transport(None), "传输层"),
    (lambda: None, "发送失败"),
])
def test_async_failure_paths_call_back_with_error(wire, break_session, expect):
    """未登录 / 无 transport / 发送异常：都回调 `data=None` + 字符串 status。

    必须回调——业务层 `get_backward_factor_data` 丢弃返回值，紧接着
    `EnvBackwardFactor.wait_event.wait()`（`download_info_data.dis.txt`
    :12470-12474），不回调就是永久挂死。字符串 status 是
    `BackwardFactorSpi.OnResponse` 的 `isinstance(status, str)` 分支
    （`info_spi.dis.txt:741-754`），`error_list` 如实收到。
    """
    if expect == "发送失败":
        wire.raise_on_market = OSError("连接被对端重置")
    else:
        break_session()

    seen: list = []
    ok, err = tm.QueryExFactorTable("600519", query_spi=lambda d, s: seen.append((d, s)))

    assert len(seen) == 1
    data, status = seen[0]
    assert data is None
    assert isinstance(status, str) and expect in status
    assert (ok, err) == (False, status)


@pytest.mark.parametrize("break_session, expect", [
    (lambda: setattr(tm._SESSION, "logged_in", False), "未登录"),
    (lambda: tm._SESSION.set_transport(None), "传输层"),
    (lambda: None, "发送失败"),
])
def test_sync_failure_paths_return_none_and_error(wire, break_session, expect):
    """同步形态按官方契约回 `(None, 错误文案)`，不抛异常。

    `tgw/interface.py:576`：「result 表示为查询结果集……如果结果集为 None，
    可以通过 err_code 查看相关错误信息」。返回空 DataFrame 更糟——
    那正是「以为查到了、拿到一张空表」。
    """
    if expect == "发送失败":
        wire.raise_on_market = OSError("连接被对端重置")
    else:
        break_session()

    result, err = tm.QueryExFactorTable("600519")   # 不抛即通过

    assert result is None
    assert isinstance(err, str) and expect in err


def test_sync_send_exception_does_not_propagate(wire):
    """发送异常绝不能冒到业务层——那会把它停在 `wait_event.wait()` 上。"""
    wire.raise_on_market = RuntimeError("传输层内部炸了")
    result, err = tm.QueryExFactorTable("600519")

    assert result is None
    assert "传输层内部炸了" in err


def test_failed_query_does_not_leak_task(wire):
    """失败的查询也要出任务表，否则 `req_list_len` 永远减不到 0。"""
    tm._SESSION.logged_in = False
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)

    assert tm.pending_task_ids() == ()


def test_transform_failure_becomes_visible_failure(wire):
    """响应整形失败也是可见失败，不是一张残表。"""
    tm.QueryExFactorTable("600519", query_spi=lambda d, s: None)
    task_id = tm.pending_task_ids()[0]
    # 线上 CSV 段数是 5，这里塞一段 `ex_date` 不可解析的脏数据
    tm.deliver_response(task_id, ["600519.SH,600519,NOTADATE,1.0,1.0"], None)

    assert tm.pending_task_ids() == ()
