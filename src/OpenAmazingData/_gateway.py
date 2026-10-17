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
                frames: dict[Hashable, pd.DataFrame]) -> list[Hashable]:
    """发一轮，等齐回包。回包写进 `frames`，返回失败的键（按回包到达顺序）。"""
    if not keys:
        return []
    done = threading.Event()
    lock = threading.Lock()
    pending = [len(keys)]
    failed: list[Hashable] = []

    def on_response(key: Hashable) -> Callable[[Any, Any], None]:
        def callback(data: Any, status: Any) -> None:
            with lock:
                if data is not None:
                    frames[key] = data
                if is_failed(status):
                    failed.append(key)
                pending[0] -= 1
                if pending[0] == 0:
                    done.set()
        return callback

    for key in keys:
        task_id = tgw.GetTaskID()
        for name, value in params_of(key):
            tgw.SetThirdInfoParam(task_id, name, value)
        tgw.QueryThirdInfo(task_id, query_spi=on_response(key))
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
        failed = _send_round(batch, params_of, frames)
        for i in range(retry_times):
            if not failed:
                break
            print("retry ", str(i))
            failed = _send_round(list(failed), params_of, frames)
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
    return tgw.QueryThirdInfo(task_id)[0]
