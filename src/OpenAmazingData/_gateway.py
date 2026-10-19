"""资讯查询的批量收发：按代码分批发出，等齐回包，失败的代码重试。

线上协议、DataFrame 整形都在 `tgwpy.compat.tgw_module` 里，本模块只管
「一批代码怎么发、什么算失败、失败了怎么办」。厂商的同一套语义散在
53 个下载函数各自的闭包和 `EnvXxx` 类变量里（`download_info_data.pyc`），
这里收成一个函数。

判失败的规则照厂商 SPI（`info_spi.pyc` 各 `OnResponse`）：

* 回包 `data` 非空就记下，空则保留这只代码的初值（空 DataFrame）；
* `status` 是字符串，或者不在 `(kSuccess, kDataEmpty)` 里，这只代码算失败。

一批里有失败的代码，只重发失败的那些，最多 `RETRY_TIMES` 轮；仍有失败就抛
`Exception('查询失败')`。重发轮次拿到的结果覆盖到同一批里，没失败的代码
保留首轮结果。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import Any

import pandas as pd

from tgwpy.compat import tgw_module as tgw
from ._diagnostics import emit_failure

#: 厂商 `QueryPara.retry_times`。
RETRY_TIMES = 3

Params = Sequence[tuple[str, Any]]


#: 一批代码重试后仍有失败时抛的消息。厂商抛的是裸 `Exception`，异常类型属于
#: 暴露面（门禁比较类型名），所以这里不定义子类。
QUERY_FAILED = "查询失败"


def is_failed(status: Any) -> bool:
    """回包状态算不算失败。规则见模块文档。"""
    if status is None:
        return False
    if isinstance(status, str):
        return True
    return status not in (tgw.ErrorCode.kSuccess, tgw.ErrorCode.kDataEmpty)


def _send_round(keys: Sequence[Hashable], params_of: Callable[[Hashable], Params],
                frames: dict[Hashable, pd.DataFrame], *, attempt: int = 1,
                max_attempts: int = RETRY_TIMES + 1) -> list[Hashable]:
    """发一轮，等齐回包。回包写进 `frames`，返回失败的键（按回包到达顺序）。"""
    if not keys:
        return []
    done = threading.Event()
    lock = threading.Lock()
    pending = [len(keys)]
    failed: list[Hashable] = []

    def on_response(key: Hashable, task_id: int, function_id) -> Callable[[Any, Any], None]:
        def callback(data: Any, status: Any) -> None:
            with lock:
                if data is not None:
                    frames[key] = data
                if is_failed(status):
                    failed.append(key)
                    emit_failure("query_failed", method="QueryThirdInfo", function_id=function_id,
                                 code=key, task_id=task_id, attempt=attempt, max_attempts=max_attempts,
                                 status=status)
                pending[0] -= 1
                if pending[0] == 0:
                    done.set()
        return callback

    for key in keys:
        task_id = tgw.GetTaskID()
        params = list(params_of(key))
        for name, value in params:
            tgw.SetThirdInfoParam(task_id, name, value)
        tgw.QueryThirdInfo(task_id, query_spi=on_response(key, task_id, dict(params).get("function_id")))
    done.wait()
    return failed


def fetch_each(keys: Iterable[Hashable], params_of: Callable[[Hashable], Params],
               batch_len: int, *, retry_times: int = RETRY_TIMES) -> dict[Hashable, pd.DataFrame]:
    """每个键一次 `ReqGetThirdInfo`，按 `batch_len` 分批。

    返回 `{键: DataFrame}`，每个发出去的键都在里面；没数据的是空 DataFrame。
    `params_of(键)` 给出这次请求的参数，顺序就是线上 `item` 数组的顺序。
    """
    keys = list(keys)
    result: dict[Hashable, pd.DataFrame] = {}
    for start in range(0, len(keys), batch_len):
        batch = keys[start:start + batch_len]
        frames = {k: pd.DataFrame([]) for k in batch}
        failed = _send_round(batch, params_of, frames, max_attempts=retry_times + 1)
        for i in range(retry_times):
            if not failed:
                break
            print("retry ", str(i))
            failed = _send_round(list(failed), params_of, frames, attempt=i + 2, max_attempts=retry_times + 1)
        if failed:
            raise Exception(QUERY_FAILED)
        result.update(frames)
    return result


def fetch_one(params: Params) -> pd.DataFrame:
    """同步发一条，不重试。失败时厂商拿到的是 `(None, 原因)`，再对 `None`
    取列会 `TypeError`；这里照样把 `None` 交回去，调用方自己取用。"""
    task_id = tgw.GetTaskID()
    for name, value in params:
        tgw.SetThirdInfoParam(task_id, name, value)
    data, status = tgw.QueryThirdInfo(task_id)
    if is_failed(status):
        emit_failure("query_failed", method="QueryThirdInfo", task_id=task_id,
                     function_id=dict(params).get("function_id"), status=status, attempt=1, max_attempts=1)
    return data
