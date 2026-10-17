"""基础数据（代码表 / 证券信息 / 复权因子）与行情数据（K 线 / 快照）的请求构造与响应解析。

## 走哪条路

**先分清两条路，这是这块最容易踩的坑。**

`amd::modules::query::QueryReqSender` 那一族（`QueryKline` / `QueryCodeTableReq` /
`QueryStockInfoReq` / `QueryUnionReq` / `QueryExFactorTableReq`）构造的是**定长二进制帧**，
属于 `amd::rqa` 命名空间下的 **TCP 专线 / 部署模式**。协议.md 已经确认
`QueryReqSender::Logon` 的 118 字节帧打互联网网关 72 组参数全部无回包，
原因就在这里。

**互联网（AMA / wss）模式走的是另一族**：`amd::mdga::QuerySendRequest::Send*Request`，
用 rapidjson 的 Writer 逐字段 `AddMember`，最后 `SerializeToString` 再
`Session::SendMsg(std::string)` 发出去 —— **JSON over WebSocket**，
与 `frames/query.py` 的 `ReqGetThirdInfo` 同构（同一个 writer 家族、同样的
`headers.userName` / `headers.token` 凭据位）。

本模块**两个都给**：JSON 请求构造器是对接真实网关的那条；
二进制帧构造器一并保留，是 TCP 部署模式的完整逆向成果，
完整偏移表见帧格式逆向结论。

## 互联网模式的四个方法

| 业务能力 | method | 参数键 |
| --- | --- | --- |
| `market kline` | `ReqGetKline` | security_code / market_type / cq_flag / auto_complete / period_type / begin_date / end_date / begin_time / end_time |
| `market snapshot` | `ReqGetSnapshot` | security_code / market_type / date / begin_time / end_time / data_type |
| 复权因子 | `ReqGetExFactor` | security_code |
| 代码表 | `ReqGetReduceCodeTable` | 无业务参数，配 `QueryBandWidth` |

字符串常量地址见每个常量旁的注释（.rodata vaddr）。

已确证：方法名、参数键名与顺序、JSON 信封结构、四个请求的 record 布局。
未确证的部分集中在文末「未验证」段，不要当事实用。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# --- JSON 请求常量 ---------------------------------------------------------

#: 请求方法名。.rodata 中的字符串常量地址见下。
METHOD_KLINE = "ReqGetKline"              # @ 0x62162f
METHOD_SNAPSHOT = "ReqGetSnapshot"        # @ 0x3ff410 内
METHOD_EX_FACTOR = "ReqGetExFactor"       # @ 0x401e70 内
METHOD_REDUCED_CODE_TABLE = "ReqGetReduceCodeTable"   # @ 0x402460 内
METHOD_QUERY_BANDWIDTH = "QueryBandWidth" # @ 0x402460 内

#: 信封键名。三族共用（与 frames/query.py 同一批 .rodata 字符串）。
KEY_ID = "id"                 # @ 0x641927
KEY_METHOD = "method"         # @ 0x6192be
KEY_PARAMS = "params"         # @ 0x6192c5
KEY_HEADERS = "headers"       # @ 0x607f36
KEY_USERNAME = "userName"     # @ 0x61928a
KEY_TOKEN = "token"           # @ 0x619284

#: 带宽协商键。SDK 每个行情请求都带，实测可省。
KEY_QUERY_BANDWIDTH = "QueryBandWidth"

#: K 线周期 → `period_type`。
#:
#: **编码分两段，别把它们混为一谈。**
#:
#: - 分钟线沿用 `MDDatatype` 的 10000 档：`min1`=10000 … `min120`=10007
#: - 日线及以上另起 10100 档：`day`=10100 `week`=10101 `month`=10102
#:   `season`=10103 `year`=10104
#:
#: 交接文档把日线填成 10008（`MDDatatype.kDayKline`），那是**二进制帧**
#: `cyc_type` 的取值，JSON 线上协议不认这一档，服务端恒定无回包。
#: 全部数值来自 MITM 抓参照 SDK（tgw 1.0.8.7）的真实报文，逐个 `--period` 实测。
PERIOD_TYPE: dict[str, int] = {
    "min1": 10000, "min3": 10001, "min5": 10002, "min10": 10003,
    "min15": 10004, "min30": 10005, "min60": 10006, "min120": 10007,
    "day": 10100, "week": 10101, "month": 10102,
    "season": 10103, "year": 10104,
}

PERIOD_DAY = PERIOD_TYPE["day"]

#: K 线参数键，按 Writer 里 AddMember 的先后。
#: 值取自 MITM 抓到的参照 SDK 真实报文，不是猜的。
KLINE_PARAM_KEYS: tuple[tuple[str, object], ...] = (
    ("security_code", "600519"),   # 字符串：带 .SH 后缀会报 20002
    ("market_type", 101),          # 整数：SSE=101 / SZSE=102
    ("cq_flag", 0),                # 复权标志
    ("auto_complete", 1),
    ("period_type", PERIOD_DAY),   # 10100，不是 MDDatatype 的 10008
    ("begin_date", 20260420),      # 整数
    ("end_date", 20260424),
    ("begin_time", 900),           # 分钟数；日线也传 900/1700，不是 0
    ("end_time", 1700),
)

#: 快照参数键。日线级别的时间单位与 K 线不同：这里放大了一万倍。
SNAPSHOT_PARAM_KEYS: tuple[str, ...] = (
    "security_code", "market_type", "date", "begin_time", "end_time", "data_type",
)


def build_request(method: str, params: Mapping[str, Any], task_id: int, *,
                  username: str, token: str) -> bytes:
    """构造互联网模式的 JSON 请求帧。

    信封结构与 `frames/query.py` 的 `ReqGetThirdInfo` 一致：

        {"method": method, "params": {...},
         "headers": {"id": task_id, "userName": ..., "token": ...}}

    **`id` 在 `headers` 里，顶层不放。** 这一点原先写反了：静态扫 .rodata
    看到 `id` 字符串就以为它在顶层。MITM 抓参照 SDK 的真实报文确认它与
    `ReqGetThirdInfo` 一样写在 `headers` 下。
    """
    payload = {
        KEY_METHOD: method,
        KEY_PARAMS: dict(params),
        KEY_HEADERS: {KEY_ID: task_id, KEY_USERNAME: username, KEY_TOKEN: token},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# --- K 线 -------------------------------------------------------------------

@dataclass(slots=True)
class KlineRequest:
    """K 线查询参数。

    取值全部来自 MITM 抓到的参照 SDK 真实报文（见 `KLINE_PARAM_KEYS`）。
    两个反直觉的点：

    - `period_type` 走 **10100 档**，不是 `MDDatatype` 的 10000 档。
      10008 = `kDayKline` 是二进制帧 `cyc_type` 的取值，JSON 协议不认。
    - `begin_time` / `end_time` 日线也传 **900 / 1700**，不是 0。
      填 0 服务端直接不回包（实测）。
    """

    security_code: str
    market_type: int
    period_type: int
    begin_date: int
    end_date: int
    begin_time: int = 900
    end_time: int = 1700
    cq_flag: int = 0
    auto_complete: int = 1

    def to_params(self) -> dict[str, Any]:
        """按 Writer 的 AddMember 顺序展开成 JSON params。

        键名与顺序取自 `SendKlineRequest` @ `0x3feb60`；**值的类型与取值**
        以抓包为准——静态反编译只能定键名，定不了值。
        """
        return {
            "security_code": self.security_code,
            "market_type": self.market_type,
            "cq_flag": self.cq_flag,
            "auto_complete": self.auto_complete,
            "period_type": self.period_type,
            "begin_date": self.begin_date,
            "end_date": self.end_date,
            "begin_time": self.begin_time,
            "end_time": self.end_time,
            KEY_QUERY_BANDWIDTH: 0.0,
        }


def build_kline_request(req: KlineRequest, task_id: int, *, username: str,
                        token: str) -> bytes:
    """`market kline` 的 JSON 请求帧。"""
    return build_request(METHOD_KLINE, req.to_params(), task_id,
                         username=username, token=token)


# --- 快照 -------------------------------------------------------------------

@dataclass(slots=True)
class SnapshotRequest:
    """历史快照（Level-1）查询参数。

    抓包实测：`begin_time` / `end_time` 是 **90000000 / 170000000**，
    比 K 线同名字段大十万倍。别想当然沿用 K 线的 900 / 1700。
    """

    security_code: str
    market_type: int
    date: int
    begin_time: int = 90000000
    end_time: int = 170000000
    data_type: int = 0

    def to_params(self) -> dict[str, Any]:
        return {
            "security_code": self.security_code,
            "market_type": self.market_type,
            "date": self.date,
            "begin_time": self.begin_time,
            "end_time": self.end_time,
            "data_type": self.data_type,
            KEY_QUERY_BANDWIDTH: 0.0,
        }


def build_snapshot_request(req: SnapshotRequest, task_id: int, *, username: str,
                           token: str) -> bytes:
    """`market snapshot` 的 JSON 请求帧。"""
    return build_request(METHOD_SNAPSHOT, req.to_params(), task_id,
                         username=username, token=token)


# --- 复权因子（--adjust 的数据来源）-----------------------------------------

def build_ex_factor_request(security_code: str, task_id: int, *, username: str,
                            token: str) -> bytes:
    """复权因子表请求帧。

    `SendExFactorRequest` @ `0x401e70` 只带一个参数 `security_code`
    （.rodata @0x401e70 段内），**不带 market_type**。

    ## 这个 method 没有 function_id

    四个互联网 method 里只有它是例外。`ReqGetKline` / `ReqGetSnapshot` /
    `ReqGetReduceCodeTable` 都自成一路，而 `ReqGetExFactor` 也不在
    `ReqGetThirdInfo` 那 54 个 function_id 的体系里——`params` 里既没有
    `item` 也没有 `function_id`，`SetThirdInfoParam` 在这条路上没有意义。

    抓包实证（探针实发的报文，66 上）：

        {"headers":{"id":1791694922813,"userName":"…","token":"…"},
         "method":"ReqGetExFactor","params":{"security_code":"600519"}}

    要走 `frames/query.py::encode_third_info_request` 只会得到一个服务端
    不认的 method——**那条路是错的**，别往里套。

    ## 入参是裸代码

    `security_code` 取 `600519` 而不是 `600519.SH`。同一次连接内只改
    这一处：带后缀回 `status=-100` / `tag=DataEmpty`，`data` 是空串。
    业务层 `get_backward_factor_data` 用
    `market_type, security_code = get_tgw_type_code(code)` 拆开，传进来
    的本来就是裸码。
    """
    return build_request(METHOD_EX_FACTOR, {"security_code": security_code},
                         task_id, username=username, token=token)


def build_code_table_request(task_id: int, *, username: str, token: str,
                             security_type: str = "") -> bytes:
    """代码表请求帧（`ReqGetReduceCodeTable`）。

    `SendReducedCodeTableRequest` @ `0x402460` 的 params 里除
    `QueryBandWidth` 外没有业务字段；`security_type` 之类的过滤在
    响应侧做。留空字符串表示不带。
    """
    params: dict[str, Any] = {}
    if security_type:
        params["security_type"] = security_type
    return build_request(METHOD_REDUCED_CODE_TABLE, params, task_id,
                         username=username, token=token)


#: 行情响应的**记录格式是 CSV 字符串数组**，不是 JSON 记录数组。
#: 抓包实测 5 根日线：
#:     "600519,101,20260420,1403000000,1413940000,1400000000,1410890000,364513900,512714604145000"
#: 价格固定 6 位小数、成交量 2 位、成交额 5 位，所以要按位还原。
#: 这一层原先压根没写——请求就算发对了也解不出记录。
KLINE_CSV_FIELDS: tuple[str, ...] = (
    "security_code", "market_type", "kline_time",
    "open", "high", "low", "close", "volume", "amount",
)
#: 各列的还原除数。0 表示该列原样（字符串/整数）。
KLINE_CSV_DIVISOR: tuple[float, ...] = (
    0, 0, 0,          # security_code / market_type / kline_time
    1e6, 1e6, 1e6, 1e6,   # open high low close
    1e2, 1e5,         # volume amount
)


def _parse_kline_time(raw: str) -> str:
    """K 线时间字段按位长还原。

    **位长随周期变**，实测（抓包实证 + 本机复验）：

    | 周期 | 原始 | 位数 |
    | --- | --- | --- |
    | day / week / month | `20260420` | 8 |
    | min1 / min5 … min120 | `202609300930` | **12**（YYYYMMDDHHMM） |
    | 快照的 `orig_time` | `20260424162955000` | 17（YYYYMMDDHHMMSSmmm） |

    之前一律截成 `YYYY-MM-DD`，分钟线的时间部分被丢掉——ETF 分钟线任务
    要的就是分钟精度，截掉之后一天 242 根全变成同一个时间戳。
    """
    raw = raw.strip()
    if len(raw) <= 8:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) <= 12:
        return (f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} "
                f"{raw[8:10]}:{raw[10:12]}")
    return (f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} "
            f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}.{raw[14:17]}")


def decode_kline_rows(rows: list) -> list[dict]:
    """把行情响应里的 CSV 字符串数组还原成记录字典。

    `date` 列的位数随周期变，见 `_parse_kline_time`。
    """
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, str):
            continue
        parts = raw.split(",")
        if len(parts) != len(KLINE_CSV_FIELDS):
            continue                      # 字段数对不上就跳过，别硬凑
        rec: dict[str, Any] = {}
        for name, val, div in zip(KLINE_CSV_FIELDS, parts, KLINE_CSV_DIVISOR):
            if name == "security_code":
                rec[name] = val
            elif name == "market_type":
                rec[name] = int(val)
            elif name == "kline_time":
                rec[name] = _parse_kline_time(val)
            else:
                rec[name] = int(val) / div
        out.append(rec)
    return out


#: 快照响应的 CSV 列定义。共 36 段（末段为空，是行尾逗号造成的）。
#:
#: 逐列对账方法：抓参照 SDK `query_snapshot` 的 35 列输出，
#: 用同一时刻的**首行和末行**核对——首行全是 0 看不出名堂，
#: 末行有真实买卖档，只有它能把列名钉死。行数也对上（5432 = 5432）。
#:
#: 值 = (字段名, 除数)。除数 0 表示原样（字符串/整数）。
#: 空字符串表示该列在参照输出里没有对应字段，**未命名**，不做推测。
SNAPSHOT_CSV_FIELDS: tuple[tuple[str, float], ...] = (
    ("security_code", 0),           # 0  600519
    ("market_type", 0),             # 1  101
    ("trade_time", 0),              # 2  20260424162955000
    ("trading_phase_code", 0),      # 3  E110
    ("pre_close", 1e6),             # 4  1419000000 -> 1419.0
    ("open", 1e6),                  # 5
    ("high", 1e6),                  # 6
    ("low", 1e6),                   # 7
    ("close", 1e6),                 # 8
    ("last", 1e6),                  # 9
    ("bid_prices", 1e6),            # 10 竖线分隔 10 档
    ("bid_volumes", 1e2),           # 11
    ("ask_prices", 1e6),            # 12
    ("ask_volumes", 1e2),           # 13
    ("num_trades", 0),              # 14  整数，不缩放
    ("volume", 1e2),                # 15  554553100 -> 5545531
    ("amount", 1e5),                # 16  800341099907000 -> 8003410999.07
    ("iopv", 1e6),                  # 17
    ("high_limited", 1e6),          # 18  1560.9
    ("low_limited", 1e6),           # 19  1277.1
    ("_f20", 1e6),                  # 20  未命名（参照输出无此列）
    ("_f21", 1e6),                  # 21
    ("_f22", 1e6),                  # 22
    ("_f23", 1e6),                  # 23
    ("_f24", 0),                    # 24-28 恒为空
    ("_f25", 0),
    ("_f26", 0),
    ("_f27", 0),
    ("_f28", 0),
    ("_f29", 0),                    # 29-34 恒为 0
    ("_f30", 0),
    ("_f31", 0),
    ("_f32", 0),
    ("_f33", 0),
    ("_f34", 0),
    ("_f35", 0),                    # 35  行尾逗号产生的空段
)
#: 线上给 10 档，参照实现的 DataFrame 只暴露前 5 档。
SNAPSHOT_LEVELS = 10
SNAPSHOT_LEVELS_EXPOSED = 5
#: 必须按字符串保留的列。除数 0 不代表就是整数——`600519` 转成 int 就废了。
SNAPSHOT_STR_FIELDS = frozenset({"security_code", "trading_phase_code", "trade_time"})


def _scaled(val: str, div: float, as_str: bool = False) -> Any:
    if val == "":
        return None
    if as_str:
        return val
    if div == 0:
        try:
            return int(val)
        except ValueError:
            return val
    return int(val) / div


def _decode_time(raw: str) -> str:
    """``20260424162955000`` → ``2026-04-24 16:29:55``（末三位是毫秒，丢弃）。"""
    if len(raw) < 14:
        return raw
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"


def decode_snapshot_rows(rows: list) -> list[dict]:
    """把快照响应的 CSV 字符串数组还原成记录字典。

    买卖档从 `bid_prices` 之类的一维字段再摊平成 `bid_price1..5` / `bid_volume1..5`，
    列名与参照实现的 DataFrame 对齐。
    """
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, str):
            continue
        parts = raw.split(",")
        if len(parts) != len(SNAPSHOT_CSV_FIELDS):
            continue
        rec: dict[str, Any] = {}
        for (name, div), val in zip(SNAPSHOT_CSV_FIELDS, parts):
            as_str = name in SNAPSHOT_STR_FIELDS
            if "|" in val:
                rec[name] = [_scaled(v, div, as_str) for v in val.split("|")]
            else:
                rec[name] = _scaled(val, div, as_str)
        if isinstance(rec.get("trade_time"), str):
            rec["trade_time"] = _decode_time(rec["trade_time"])

        for side in ("bid", "ask"):
            prices = rec.get(f"{side}_prices") or []
            vols = rec.get(f"{side}_volumes") or []
            for i in range(SNAPSHOT_LEVELS_EXPOSED):
                if i < len(prices):
                    rec[f"{side}_price{i + 1}"] = prices[i]
                if i < len(vols):
                    rec[f"{side}_volume{i + 1}"] = vols[i]
        out.append(rec)
    return out


#: 复权因子响应的 CSV 段定义。列名与顺序 = `EX_FACTOR_FIELDS`
#: （`galaxy::tgw::Tools::Serialize(MDExFactorTable)` @ `0x222f60` 的 5 项），
#: 与探针实测的 data 原文逐段对账：
#:
#:     600519.SH,600519,20010827,1.000000000000000000,1.000000000000000000
#:     inner_code security_code ex_date  ex_factor  cum_factor
#:
#: 值 = (字段名, 取值方式)。**这一族没有除数**——K 线那套 1e6 / 1e2 /
#: 1e5 的定点缩放在这里一个都不适用，因子是 18 位小数的十进制串，套上
#: 缩放会把它缩小 6 个数量级。
EX_FACTOR_CSV_KINDS: tuple[tuple[str, str], ...] = (
    ("inner_code", "str"),
    ("security_code", "str"),
    ("ex_date", "int"),
    ("ex_factor", "float"),
    ("cum_factor", "float"),
)


def decode_ex_factor_rows(rows: list) -> list[dict]:
    """把复权因子响应的 CSV 字符串数组还原成记录字典。

    ## 精度

    `ex_factor` / `cum_factor` 线上是 18 位小数的十进制串。这里**按
    float 直接解析**：不套定点缩放，也不四舍五入到任何固定位数——把
    `1.023663926756412223` 截成 6 位小数会让复权价在第 6 位上出错。

    float64 留 15~17 位有效数字。线上这 18 位里绝大多数是尾随零
    （`8.646881000000000000` → `8.646881` 一个有效位不丢），真的长尾
    （`1.108714275494509425`）才掉到 17 位，差 1~2 个末位。

    **原串另存 `ex_factor_text` / `cum_factor_text`**：十进制字面量只在
    CSV 里存在一次，转成 float 就再也回不去了。要精确十进制的调用方从
    这两列重新 `Decimal()` 解析。这两列业务层用不上（它只取因子列），
    不会流进 HDF5。

    ## `ex_date` 必须是 int

    业务层拿这一列当索引：`set_index(['ex_date'])` → `sort_index()` →
    `.loc[calendar_index]`。`calendar_index` 是
    `list(TRADE_DAYS.astype(int))`（`base_data.dis.txt` `get_calendar`
    第 50/53 行），整数 YYYYMMDD。两边不同类型域，`.loc` 取不到值，
    整张因子表塌成 NaN 再被 `fillna(1)` 填成 1.0——**复权静默失效**。
    """
    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, str):
            continue
        parts = raw.split(",")
        if len(parts) != len(EX_FACTOR_CSV_KINDS):
            continue                      # 段数对不上就跳过，别硬凑
        rec: dict[str, Any] = {}
        text: dict[str, str] = {}
        for (name, kind), val in zip(EX_FACTOR_CSV_KINDS, parts):
            if kind == "str":
                rec[name] = val
            elif kind == "int":
                rec[name] = int(val)
            else:
                rec[name] = float(val)
                text[f"{name}_text"] = val
        # 文本列**追加在末尾**，不插在各自因子列后面：键序即列序，
        # 业务层 `return_df_format=False` 拿到的记录列表也照这个序。
        rec.update(text)
        out.append(rec)
    return out


# --- 响应字段名 -------------------------------------------------------------

#: K 线响应字段名顺序。`galaxy::tgw::Tools::Serialize(MDKLine)` @ `0x21ad10`，
#: 11 项，按 lea 出的先后即序列化顺序。
KLINE_FIELDS: tuple[str, ...] = (
    "market_type", "security_code", "orig_time", "kline_time",
    "open_price", "high_price", "low_price", "close_price",
    "volume_trade", "value_trade", "variety_category",
)

#: 快照响应字段名顺序。`galaxy::tgw::Tools::Serialize(MDSnapshotL1)` @ `0x21a720`，
#: 21 项。bid_price / bid_volume / offer_price / offer_volume 是档位数组。
SNAPSHOT_L1_FIELDS: tuple[str, ...] = (
    "market_type", "security_code", "variety_category", "orig_time",
    "trading_phase_code", "pre_close_price", "open_price", "high_price",
    "low_price", "last_price", "close_price", "bid_price", "bid_volume",
    "offer_price", "offer_volume", "num_trades", "total_volume_trade",
    "total_value_trade", "IOPV", "high_limited", "low_limited",
)

#: 代码表记录字段名顺序。`galaxy::tgw::Tools::Serialize(MDCodeTableRecord)`
#: @ `0x21cc00`，43 项。
CODE_TABLE_FIELDS: tuple[str, ...] = (
    "security_code", "market_type", "symbol", "english_name", "security_type",
    "currency", "variety_category", "pre_close_price", "underlying_security_id",
    "contract_type", "exercise_price", "expire_date", "high_limited",
    "low_limited", "security_status", "price_tick", "buy_qty_unit",
    "sell_qty_unit", "market_buy_qty_unit", "market_sell_qty_unit",
    "buy_qty_lower_limit", "buy_qty_upper_limit", "sell_qty_lower_limit",
    "sell_qty_upper_limit", "market_buy_qty_lower_limit",
    "market_buy_qty_upper_limit", "market_sell_qty_lower_limit",
    "market_sell_qty_upper_limit", "list_day", "par_value", "outstanding_share",
    "public_float_share_quantity", "contract_multiplier", "regular_share",
    "interest", "coupon_rate", "product_code", "delivery_year",
    "delivery_month", "create_date", "start_deliv_date", "end_deliv_date",
    "position_type",
)

#: 证券信息响应字段名顺序。`galaxy::tgw::Tools::Serialize(MDStockInfo)`
#: @ `0x21c440`，21 项。末两项与前面重复，是原生序列化里的重复输出。
STOCK_INFO_FIELDS: tuple[str, ...] = (
    "security_code", "symbol", "market_type", "security_type", "currency",
    "security_status", "pre_close_price", "total_shares", "flow_shares",
    "noprofit", "weighted_voting_rights", "registration_flag", "eps",
    "eps_cell", "net_profit_ttm", "net_profit", "net_asset",
    "net_profit_recent_annual", "net_profit_first_quarter",
    "registration_flag", "weighted_voting_rights",
)

#: 复权因子记录字段名顺序。
#: `galaxy::tgw::Tools::Serialize(MDExFactorTable)` @ `0x222f60`，5 项。
EX_FACTOR_FIELDS: tuple[str, ...] = (
    "inner_code", "security_code", "ex_date", "ex_factor", "cum_factor",
)


def field_names(kind: str) -> tuple[str, ...]:
    """按响应类型取字段名顺序。"""
    table = {
        "kline": KLINE_FIELDS,
        "snapshot": SNAPSHOT_L1_FIELDS,
        "code_table": CODE_TABLE_FIELDS,
        "stock_info": STOCK_INFO_FIELDS,
        "ex_factor": EX_FACTOR_FIELDS,
    }
    try:
        return table[kind]
    except KeyError:
        raise ValueError(f"未知响应类型 {kind!r}，可选 {sorted(table)}") from None


# --- 结构体布局（TCP 部署模式，逆向成果留档）--------------------------------

#: `galaxy::tgw::ReqKline` 的字段偏移。
#: 证据：`galaxy::tgw::InternetConvert::KlineRequestConvert` @ `0x2728e0`
#: 逐条 `mov ..., [rbp + X]` 从业务层结构体搬到 amd 结构体。
#: 注意这是**业务层**结构体，不是 `QueryReqSender::QueryKline` 读的那个
#: （后者字段顺序完全不同，见下方 REQ_KLINE_WIRE）。
REQ_KLINE_FIELDS: tuple[tuple[int, str, int], ...] = (
    (0x00, "security_code", 0x20),   # std::string，SSO 缓冲 32 字节
    (0x20, "cq_flag", 1),
    (0x21, "auto_complete", 1),
    (0x22, "cyc_type", 1),
    (0x23, "cyc_def", 2),
    (0x25, "begin_date", 4),
    (0x29, "end_date", 4),
    (0x2D, "begin_time", 4),
    (0x31, "end_time", 4),
)

#: `QueryReqSender::QueryKline` 实际读的那个 `ReqKline` 的字段偏移。
#: 证据：`QueryReqSender::QueryKline` @ `0x4f7860` 的 `movzx/mov ..., [rbp + X]`
#: 序列（0x4f7933 起），配合 SWIG 属性顺序交叉验证。
#: `cyc_type` / `cq_flag` / `auto_complete` 都是 **1 字节**。
REQ_KLINE_WIRE: tuple[tuple[int, str, int], ...] = (
    (0x11, "cq_flag", 1),
    (0x12, "begin_date", 4),
    (0x16, "end_date", 4),
    (0x1A, "cyc_type", 1),
    (0x1B, "cyc_def", 4),
    (0x1F, "auto_complete", 1),
    (0x20, "begin_time", 4),
    (0x24, "end_time", 4),
    (0x28, "time_base", 4),
    (0x2C, "time_base2", 4),
)

# --- TCP 部署模式的二进制帧（留档，不用于互联网网关）------------------------

#: security_id 的市场前缀表。
#: 证据：`QueryUtils::SecurityID(uchar, string)` @ `0x4cd080` 的 cmp 链，
#: 每分支 `operator+(前缀字面量, code)`（0x170a50 = _ZStplIc...）。
MARKET_PREFIX: dict[int, str] = {
    2: ".neeq",      # kNEEQ
    3: ".shfe",      # kSHFE
    4: ".cffex",     # kCFFEX
    5: ".dce",       # kDCE
    6: ".czce",      # kCZCE
    7: ".ine",       # kINE
    101: ".sh",      # kSSE
    102: ".sz",      # kSZSE
    103: ".hk",      # kHKEx
    201: ".bkzs",    # kBK
}
PREFIX_ILLEGAL = ".illegal"


def security_id(market_type: int, code: str) -> str:
    """把 (market_type, security_code) 拼成二进制帧里的 security_id。

    顺序是**前缀在前**：`SecurityID` 走 `operator+(前缀, code)`
    （@ `0x170a50` = `_ZStplIcSt11char_traitsIcESaIcEESbIT_T0_T1_`），
    所以 `kSSE` + `600519` 得到 `.sh600519`，不是 `600519.sh`。

    未知市场返回 `.illegal` + code，与原生 `SecurityID` 兜底分支一致
    （.rodata @ `0x4cd118`）。
    """
    return MARKET_PREFIX.get(market_type, PREFIX_ILLEGAL) + code


def _pack_header(protocol_byte: int, data_type: int, task_id: int,
                 msg_type: int) -> bytearray:
    """二进制帧头 15 字节（0x00..0x0e）。"""
    buf = bytearray(0x0F)
    buf[0x00] = protocol_byte & 0xFF
    struct.pack_into("<H", buf, 0x01, data_type & 0xFFFF)
    struct.pack_into("<Q", buf, 0x03, task_id & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<I", buf, 0x0B, msg_type & 0xFFFFFFFF)
    return buf


#: 消息类型（写在偏移 0x0b 的 u32）。
MSG_KLINE = 0x45        # 69  @ 0x4f789b
MSG_STOCK_INFO = 0x26   # 38  @ 0x4f72f7
MSG_CODE_TABLE = 0x00   # 0   @ 0x4f5954
MSG_EX_FACTOR = 0x26    # 38  @ 0x4f6d57
MSG_UNION_REQ = 0x32    # 50  @ 0x4f7ee4

SECURITY_ID_LEN = 0x26  # 38
KLINE_FRAME_LEN = 0x54  # 84  @ NewMemoryNonblock(pool, 0x54)
CODE_TABLE_FRAME_LEN = 0x0F  # 15
EX_FACTOR_FRAME_LEN = 0x35   # 53
STOCK_INFO_FRAME_LEN = 0x35  # 53
UNION_FRAME_LEN = 0x41       # 65

#: 时间字段的魔数除数。`mov esi, 0xa7c5ac5` + `mul esi` + `shr 7`
#: 是编译器对 `/100000` 的展开。
TIME_DIVISOR = 100000
TIME_DIGIT_THRESHOLD = 5  # QueryUtils::QueryIntegerNum @ 0x4cb0a0，`cmp al, 5 / jbe`


def scale_time(value: int) -> int:
    """时间字段的协议侧换算：十进制位数 > 5 时除以 100000。

    900 -> 900（3 位，原样）；20260420 -> 202（8 位，除）。
    """
    if value == 0:
        return 0
    return value // TIME_DIVISOR if len(str(abs(value))) > TIME_DIGIT_THRESHOLD else value


def build_kline_frame(*, protocol_byte: int, data_type: int, task_id: int,
                      security_code: str, market_type: int, cyc_type: int,
                      begin_date: int, end_date: int, begin_time: int,
                      end_time: int, cq_flag: int = 0, auto_complete: int = 1,
                      cyc_def: int = 0) -> bytes:
    """TCP 部署模式的 K 线请求帧（84 字节）。

    偏移（capstone 逐条核对，0x0f..0x34 与 0x35..0x53 首尾相接无空洞）：

        0x00 u8       协议字节 = this->f_0
        0x01 u16      data_type
        0x03 u64      task_id
        0x0b u32      0x45
        0x0f char[38] security_id（前缀在前，如 .sh600519）
        0x35 u8       cq_flag
        0x36 u32      begin_date
        0x3a u32      end_date
        0x3e u8       cyc_type      ← 1 字节，10008 会截成 24
        0x3f u32      cyc_def
        0x43 u8       auto_complete
        0x44 u32      begin_time（位数 > 5 时 /100000）
        0x48 u32      end_time（同上）
        0x4c u32      time_base     （原生写的是 0x28 字段，未命名）
        0x50 u32      time_base2    （原生写的是 0x2c 字段，未命名）
    """
    buf = _pack_header(protocol_byte, data_type, task_id, MSG_KLINE)
    buf += security_id(market_type, security_code).encode()[:SECURITY_ID_LEN]
    buf += b"\x00" * (KLINE_FRAME_LEN - len(buf))
    struct.pack_into("<B", buf, 0x35, cq_flag & 0xFF)
    struct.pack_into("<I", buf, 0x36, begin_date & 0xFFFFFFFF)
    struct.pack_into("<I", buf, 0x3A, end_date & 0xFFFFFFFF)
    struct.pack_into("<B", buf, 0x3E, cyc_type & 0xFF)
    struct.pack_into("<I", buf, 0x3F, cyc_def & 0xFFFFFFFF)
    struct.pack_into("<B", buf, 0x43, auto_complete & 0xFF)
    struct.pack_into("<I", buf, 0x44, scale_time(begin_time))
    struct.pack_into("<I", buf, 0x48, scale_time(end_time))
    return bytes(buf)


def build_code_table_frame(*, protocol_byte: int, data_type: int,
                           task_id: int) -> bytes:
    """TCP 部署模式的代码表请求帧（15 字节，只有帧头）。"""
    return bytes(_pack_header(protocol_byte, data_type, task_id, MSG_CODE_TABLE))


# --- 记录步长（TCP 模式的二进制解码用）--------------------------------------

#: K 线记录步长。`InternetConvert::KlineConvert` @ `0x270de0` 每轮
#: `add rdi, 0x64` = 100 字节。
KLINE_RECORD_LEN = 0x64  # 100

#: 复权因子记录步长。`InternetConvert::ExFactorConvert` @ `0x272770`
#: 每轮 `add rdi, 0x44` = 68 字节。
EX_FACTOR_RECORD_LEN = 0x44  # 68

#: 代码表记录步长。`amd::rqa::Tools::GetDataByIndex(MDCodeTable*)` @ `0x2c9a20`
#: lea(×3) -> shl 6(×64) -> sub 1 = 191 个元素，×4 = 764 字节。
CODE_TABLE_RECORD_LEN = 0x2FC  # 764

#: 快照记录步长 —— 未定死，见文末「未验证」第 3 条。
SNAPSHOT_RECORD_LEN: int | None = None


# --- 未验证 -----------------------------------------------------------------

# 1. **JSON 请求的业务参数值语义未实测**。方法名、参数键名、写入顺序都是从
#    `SendKlineRequest` @ `0x3feb60` / `SendSnapshotRequest` @ `0x3ff410` 的
#    .rodata 引用顺序直接读出来的，键名确证；但每个键该填什么值
#    （尤其 `period_type` 到底吃 10000 档的 MDDatatype 还是别的编码、
#    `data_type` 快照档位怎么选）需要登录打通后拿真实回包验证。
#
# 2. **`ReqGetReduceCodeTable` 的分页机制未逆完**。
#    `SendReducedCodeTableRequest` @ `0x402460` 里除 `QueryBandWidth` 外
#    没有业务字段，代码表是分批下发 + `QueryBandWidth` 协商带宽，
#    完整协议要等实测。
#
# 3. **响应侧 `amd::mdga` 的二进制解码没有逆完**。字段名已知、顺序已知，
#    但互联网模式下响应也是 JSON 还是二进制流、记录内各字段的精确字节偏移，
#    都需要真实回包。快照尤其不确定：`bid_list` / `ask_list` 是变长档位
#    数组（`MDSnapshotL1` 的字段名表里是 `bid_list:` / `ask_list:`），
#    记录本身很可能不定长，`SNAPSHOT_RECORD_LEN` 因此留空。
#
# 4. **复权（--adjust qfq/hfq）不在 K 线请求里，协议层无复权字段。**
#    业务层走独立的 `download_backward_factor` / `download_adj_factor`
#    （`BackwardFactorSpi` / `AdjFactorSpi`），拿到 ex_factor / cum_factor
#    后在客户端换算。所以复权是**客户端后处理**，不是请求字段。
#    换算公式在业务层 `.pyc` 里，不在本模块范围。
#    佐证：K 线请求的 9 个参数里没有任何与复权相关的键。
#
# 5. `KlineTypeConvert`（@ `0x2727d0`）是运行期构建的哈希表，查不到时返回
#    默认值 0x10（16）。tgw 的 Period 到 mdga 周期的实际映射值**未确证**，
#    表在 .bss（0x916000 / 0x916040），静态不可读。
#
# 6. 二进制帧（TCP 模式）偏移 0x00 的协议字节取自 `QueryReqSender` 的
#    `this->f_0`，由上层配置注入，**具体数值未确证**。
