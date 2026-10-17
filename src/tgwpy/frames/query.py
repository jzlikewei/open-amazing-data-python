"""下载协议（QueryThirdInfo）的请求构造与响应解包。

互联网模式（AMA / wss）走的是 **JSON over WebSocket**，不是二进制帧。
证据：`amd::mdga::QuerySendRequest::SendThirdInfoRequest` 用 rapidjson 的
Writer 逐个 `AddMember`，末尾 `SerializeToString` 再 `Session::SendMsg(std::string)`
（`libtgw.so` @ `0x404280`）。

请求信封（`0x404280` 内的字符串常量 + writer 调用顺序）：

    {
      "id":      <task_id>,          # AddUint64 @0x3fe9f0，完整 64 位整数
      "method":  "ReqGetThirdInfo",  # @0x4045e0
      "params": {
        "function_id": "<fid>",      # 平级节点，不进 item
        "offset": 0,                 # 同上
        "count": 1000,               # 同上，缺省 1000
        "item": [ {"key": k, "value": v}, ... ]   # @0x404c9a，其余具名参数
      },
      "headers": {
        "userName": <username>,      # @0x404514，key 长度 8
        "token":    <token>         # @0x404596，key 长度 5
      }
    }

`function_id` / `offset` / `count` 被循环显式跳过，不进 `item`
（反编译实证）。

`item` 数组就是 `SetThirdInfoParam` 暂存的那些键值对
（`amd::mdga::QueryRequestCache::SetThirdInfoParam` @ `0x3f7bc0`：
`[this+0xc0]` 是 `unordered_map<u64, map<string,string>>`，
`[this+0x28]` 是互斥量，先锁后 insert）。

响应信封（`amd::mdga::QueryImpl::OnMessage` @ `0x3f24d0` +
`amd::mdga::QueryDecoder::HandleThirdInfo` @ `0x3d8620`）：

    {
      "headers": {
        "id":            <task_id>,   # 32 位有符号读出，见下方 TRUNCATION 警告
        "pack_num":      <n>,         # 本包序号
        "all_pack_num":  <m>          # 总包数
      },
      "result": {                      # 推断，见帧格式逆向结论
        "code": <int>,
        "msg":  <string>
      },
      "data":   <...>                  # 数据节点
    }

响应没有 `method` 字段，靠 `headers.id` 认领任务。
传输层分帧：`[u32 总长][10 字节头][载荷]`，一条记录 = 总长 + 10 字节
（`amd::rqa::TcpProtocolDecode::OnMessage` @ `0x2ef400`，
反编译实证：`v15 >= *(v7) + 10`）。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

# --- 协议常量（来源见上）-------------------------------------------------

#: 请求方法名。libtgw.so @ 0x621757 "ReqGetThirdInfo"
METHOD_GET_THIRD_INFO = "ReqGetThirdInfo"

#: 信封键名。各地址为 .rodata 中的字符串常量地址。
KEY_ID = "id"                 # @ 0x641927
KEY_METHOD = "method"         # @ 0x6192be
KEY_PARAMS = "params"         # @ 0x6192c5
KEY_HEADERS = "headers"       # @ 0x607f36
KEY_ITEM = "item"             # @ 0x62177f
KEY_KEY = "key"               # @ 0x63efff
KEY_VALUE = "value"           # @ 0x63c46e
#: 这三个不进 item，是 params 下的平级节点。query_404280.c:279 的循环显式跳过。
KEY_FUNCTION_ID = "function_id"   # @ 0x6214d5
KEY_OFFSET = "offset"             # @ 0x60662d
KEY_COUNT = "count"               # @ 0x606603
KEY_USERNAME = "userName"     # @ 0x61928a
KEY_TOKEN = "token"           # @ 0x619284
KEY_PACK_NUM = "pack_num"     # @ 0x61f5eb
KEY_ALL_PACK_NUM = "all_pack_num"   # @ 0x61f5e7
#: 外层容器名。.rodata 里没有这个键名，纯推断，见 parse 里的注释。
KEY_RESULT = "result"

#: 传输层固定头长。一条记录总长 = u32 总长字段 + 本值。
#: 证据：反编译实证，两处控制流校验。
LENGTH_PREFIX = 4
FRAME_HEADER_LEN = 10

#: 单次拉取的默认条数。原生侧 `sub_7fe890(v4, "count", 5, 1000, ...)`
#: （query_404280.c:269）。
DEFAULT_COUNT = 1000

#: 客户端自报的分页上限。.rodata @ 0x60660e "50000"，与
#: `QueryRequestCache::CheckOffsetAndCountParam`（@ 0x3f6560）共用。
#: 与 DEFAULT_COUNT 的关系未验证。
MAX_COUNT = 50000


class QueryProtocolError(ValueError):
    """帧或信封结构不合法。"""


# --- 请求 -----------------------------------------------------------------


def build_third_info_request(
    task_id: int,
    params: Mapping[str, str] | Sequence[tuple[str, str]],
    *,
    username: str = "",
    token: str = "",
    method: str = METHOD_GET_THIRD_INFO,
) -> dict[str, Any]:
    """构造 QueryThirdInfo 请求信封。

    `params` 接受映射或有序键值序列——`SetThirdInfoParam` 是按调用顺序
    `params` 接受映射或有序键值序列。

    `function_id` / `offset` / `count` 三个是 `params` 下的**平级节点**，
    其余具名参数进 `item` 数组 —— 反编译实证的
    循环显式跳过这三个键不写进 `item`。`count` 缺省 1000
    （`query_404280.c:269`）。

    `token` 是登录后由服务端下发的会话令牌，登录帧由 AMA 路径负责，
    本模块不生成。空串表示尚未登录。
    """
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    if not items:
        raise QueryProtocolError("params 为空，至少要有 function_id")
    if not any(k == "function_id" for k, _ in items):
        raise QueryProtocolError("params 缺少 function_id")

    top: dict[str, Any] = {KEY_FUNCTION_ID: "", KEY_OFFSET: 0, KEY_COUNT: DEFAULT_COUNT}
    rest: list[tuple[str, str]] = []
    for k, v in items:
        if k in top:
            top[k] = v
        else:
            rest.append((k, v))

    # 实测校正（真实网关抓包确认成功响应格式）：
    #   - `id` 在 **headers** 里，不在顶层
    #     （SendThirdInfoRequest 开头 sub_7fe9f0(v2, a1, ...) 把第一个
    #       参数写进 headers 对象）
    #   - `function_id` / `offset` / `count` / `item` / `params`
    #     全在**顶层**，不在 `params` 对象里
    # 早期版本写成 {"id":..,"method":..,"params":{function_id,offset,count,item}}
    # 服务端静默丢弃，不回任何包——实测 4 种变体全哑。
    return {
        KEY_HEADERS: {
            KEY_ID: int(task_id),
            KEY_USERNAME: username,
            KEY_TOKEN: token,
        },
        KEY_METHOD: method,
        **top,
        KEY_ITEM: [{KEY_KEY: k, KEY_VALUE: v} for k, v in rest],
        KEY_PARAMS: {},
    }


def encode_third_info_request(
    task_id: int,
    params: Mapping[str, str] | Sequence[tuple[str, str]],
    *,
    username: str = "",
    token: str = "",
) -> bytes:
    """序列化成 WebSocket 文本帧的 UTF-8 字节。

    原生侧用 rapidjson `SerializeToString`（@ 0x404e2c）产出紧凑 JSON，
    键序即写入序，这里用 `sort_keys=False` + 插入序保持一致。
    """
    env = build_third_info_request(
        task_id, params, username=username, token=token
    )
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- 响应：传输层分帧 -------------------------------------------------------


def iter_frames(buffer: bytes) -> Iterator[bytes]:
    """按 `[u32 总长][10 字节头][载荷]` 切出完整记录，尾部残缺则丢弃。

    复刻 `TcpProtocolDecode::OnMessage` 的边界判断：剩余不足 10 字节、
    或剩余小于 `总长 + 10` 时，本条记录不完整。
    残段留在调用方手里等下一批字节，本函数不消费它。
    """
    pos = 0
    total_len = len(buffer)
    while total_len - pos > LENGTH_PREFIX - 1:
        (body_len,) = struct.unpack_from("<I", buffer, pos)
        need = body_len + FRAME_HEADER_LEN
        if total_len - pos < need:
            return
        yield buffer[pos:pos + need]
        pos += need


def consumed_length(buffer: bytes) -> int:
    """返回 `iter_frames` 会消费的字节数，剩下的是残段。"""
    pos = 0
    total_len = len(buffer)
    while total_len - pos > LENGTH_PREFIX - 1:
        (body_len,) = struct.unpack_from("<I", buffer, pos)
        need = body_len + FRAME_HEADER_LEN
        if total_len - pos < need:
            break
        pos += need
    return pos


# --- 响应：信封 ------------------------------------------------------------


@dataclass(frozen=True)
class ThirdInfoResponse:
    """一包下载响应。"""

    #: `headers.id`，即请求里的 task_id
    task_id: int
    #: `headers.pack_num`，本包序号
    pack_num: int = 0
    #: `headers.all_pack_num`，总包数
    all_pack_num: int = 0
    #: 状态码，挂在 `result` 下还是根下未验证
    code: int | None = None
    #: 状态描述，同上
    msg: str | None = None
    #: `data` 节点原文，未做结构假设
    data: Any = None

    @property
    def is_last(self) -> bool:
        """`pack_num` 是 0 基还是 1 基未验证，两种都覆盖。"""
        if self.all_pack_num <= 0:
            return True
        return self.pack_num >= self.all_pack_num - 1 or self.pack_num >= self.all_pack_num

    def to_json(self) -> str:
        """按服务端同构的 JSON 形态输出。

        `Tools::ThirdInfoToJson`（@ 0x231d90）只把 `ThirdInfoData` 的
        24 字节字符串元素原样搬进输出流，不改字段名——`data` 里是什么
        就原样吐什么。
        """
        out: dict[str, Any] = {KEY_ID: self.task_id}
        if self.code is not None:
            out[KEY_RESULT] = {"code": self.code, "msg": self.msg or ""}
        out["data"] = self.data
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def parse_third_info_response(payload: bytes | str) -> ThirdInfoResponse:
    """解析一包响应。`payload` 是单条记录的载荷部分（已跳过 10 字节头）。"""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise QueryProtocolError(f"响应不是合法 UTF-8: {exc}") from exc
    try:
        doc = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise QueryProtocolError(f"响应不是合法 JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise QueryProtocolError(f"响应顶层应为对象，实得 {type(doc).__name__}")

    headers = doc.get(KEY_HEADERS) or {}
    if not isinstance(headers, dict):
        raise QueryProtocolError("headers 不是对象")
    if KEY_ID not in headers:
        raise QueryProtocolError("Json has no 'headers.id' node.")

    # code/msg 挂哪儿未验证：ConvertThirdInfoJS @0x3d38c1 把它们定位在
    # 文档根，.rodata 里却没有 result 这个 JSON 键名。两处都试。
    result = doc.get(KEY_RESULT)
    if not isinstance(result, dict):
        result = {}

    return ThirdInfoResponse(
        task_id=int(headers[KEY_ID]),
        pack_num=int(headers.get(KEY_PACK_NUM, 0)),
        all_pack_num=int(headers.get(KEY_ALL_PACK_NUM, 0)),
        code=_opt_int(result.get("code") if result else doc.get("code")),
        msg=result.get("msg") if result else doc.get("msg"),
        data=doc.get("data"),
    )


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def collect_responses(
    buffer: bytes, *, compress: bool = False
) -> tuple[list[ThirdInfoResponse], bytes]:
    """从缓冲区里取出所有完整响应，返回 (响应列表, 未消费的残段)。

    `compress=True` 时对 `data` 里的字符串做 zlib 解压——原生侧有
    "Query compress" / "decompress failed <{1}>" 日志（@ 0x620949 /
    @ 0x61e07f），说明大结果会压缩后放 JSON 字段。压缩字段名与算法
    未验证，这里按 `data` 里的 base64 字符串试 zlib，失败就原样留着。
    """
    out: list[ThirdInfoResponse] = []
    for frame in iter_frames(buffer):
        payload = frame[FRAME_HEADER_LEN:]
        rsp = parse_third_info_response(payload)
        if compress:
            rsp = _maybe_decompress(rsp)
        out.append(rsp)
    return out, buffer[consumed_length(buffer):]


def _maybe_decompress(rsp: ThirdInfoResponse) -> ThirdInfoResponse:
    if not isinstance(rsp.data, str) or not rsp.data:
        return rsp
    import base64
    import zlib

    try:
        raw = base64.b64decode(rsp.data, validate=True)
        plain = zlib.decompress(raw)
    except Exception:  # noqa: BLE001 — 压缩形态未验证，解不开就保持原样
        return rsp
    try:
        return ThirdInfoResponse(
            task_id=rsp.task_id,
            pack_num=rsp.pack_num,
            all_pack_num=rsp.all_pack_num,
            code=rsp.code,
            msg=rsp.msg,
            data=json.loads(plain.decode("utf-8")),
        )
    except Exception:  # noqa: BLE001
        return rsp


# --- task_id ---------------------------------------------------------------


def make_task_id(now: float, last: int = 0) -> int:
    """1:1 复刻 `TgwImpl::GetTaskID`（@ 0x2123a0）。

    原式（本地时间）：

        v = sec*1000 + min*1e5 + hour*1e7 + mday*1e9
            + (mon+1)*1e11 + (year+1900)*1e10
        v >>= 3; v %= 10**10
        id = v * 1e6 + 1
        if id == last: id += 1

    注意 `v %= 10**10` 之后 `id` 最大约 1e16，**超出 int32**。
    响应侧却用 `movsxd r12, dword ptr [rax]`（@ 0x3d8afc）按 32 位
    有符号读回 `headers.id`。这个不对称是否会在真实链路上截断，
    未验证——原生侧大概率只在低 32 位唯一时才这么读。
    """
    import time

    tm = time.localtime(now)
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
    task_id = v * 1_000_000 + 1
    if task_id == last:
        task_id += 1
    return task_id
