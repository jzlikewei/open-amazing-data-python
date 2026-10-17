"""把单个 CLI 方法铺到整张代码表上，一次登录跑完全部批次。

为什么不让调用方「起 105 个进程各跑一次」：

* **登录开销**。每次进程启动都要重拼交易日历（12 个请求），
  105 批就是 1260 个请求，光日历就够把网关打到 404。
* **单会话**。同账号第二条连接就是 -98，串行还好，并发就是自找麻烦。
* **节流要有全局视角**。`guard.RequestGate` 的退避档位是跨请求累计的，
  拆成独立进程就退回「每批各自为政」，批量打快的教训会原样复发。

所以：一个进程、一次登录、一条连接，批次之间共用同一个闸门，
每批做完就往 checkpoint 里落一笔。中断了重跑，续上没做完的批。

**输出是 JSONL，一行一个批次。** 续跑时已经做完的行还在原处，
调用方按行读就能拿到完整结果；写成单个 JSON 数组的话中途断电
就什么都没有——那正是最需要续跑的时刻。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .guard import BatchCheckpoint, ServerUnavailable, SessionBusy

#: 一批多少只代码。默认 50 是任务书给的口径。
DEFAULT_BATCH_SIZE = 50


@dataclass
class BatchPlan:
    """一个批处理任务的完整描述，也就是 checkpoint 指纹的来源。"""

    category: str
    method: str
    options: dict[str, Any]
    codes: list[str]
    batch_size: int = DEFAULT_BATCH_SIZE
    #: 单次运行最多做几批，给调试和「先试跑几批」用。0 = 不限。
    limit: int = 0

    def batches(self) -> list[list[str]]:
        n = max(1, self.batch_size)
        return [self.codes[i:i + n] for i in range(0, len(self.codes), n)]

    def fingerprint(self) -> str:
        """任务指纹。**代码表和选项都进去**——换了宇宙或换了日期区间
        却复用旧 checkpoint，会静默跳过没下过的批。"""
        import hashlib

        payload = json.dumps(
            {
                "category": self.category,
                "method": self.method,
                "options": {k: v for k, v in sorted(self.options.items())},
                "codes": self.codes,
                "batch_size": self.batch_size,
            },
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class BatchResult:
    """一次批处理跑完（或中途停手）的账。"""

    total: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    empty: int = 0
    codes_ok: int = 0
    codes_empty: int = 0
    stopped: str = ""
    seconds: float = 0.0
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "done": self.done, "skipped": self.skipped,
            "failed": self.failed, "empty": self.empty,
            "codes_ok": self.codes_ok, "codes_empty": self.codes_empty,
            "stopped": self.stopped, "seconds": round(self.seconds, 2),
            "failures": self.failures,
        }


def _append_jsonl(path: str, record: dict) -> None:
    """追加一行并落盘。**每批都 fsync**——断电后续跑要靠它，
    只在退出时 flush 的话崩了就白跑。"""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        import os

        os.fsync(fh.fileno())


def _count_codes(payload: Any) -> int:
    """payload 是 {code: [records]} 时数出有多少只真有数据。

    键在但记录为空，算「这只今天没数据」——停牌、新上市、库里本来就没
    这一天。它和「查询失败」在产出上一样（都是没数据），但成因不同，
    所以分开计，不混成一个数。
    """
    if not isinstance(payload, dict):
        return 0
    return sum(1 for v in payload.values() if v)


def run_batches(
    ad: Any,
    plan: BatchPlan,
    spec: Any,
    checkpoint: BatchCheckpoint,
    out_path: str,
    *,
    dispatch: Callable[..., Any] | None = None,
    normalize: Callable[..., Any] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> BatchResult:
    """跑完 plan 里所有批次，结果逐批追加到 `out_path`。

    `dispatch` / `normalize` / `on_progress` 是注入点：测试用假实现跑，
    不碰网络。生产走 `cli.dispatch` / `cli.normalize`。
    """
    from . import cli as _cli

    dispatch = dispatch or _cli.dispatch
    normalize = normalize or _cli.normalize
    batches = plan.batches()
    result = BatchResult(total=len(batches))
    started = time.monotonic()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    for index, batch in enumerate(batches):
        if plan.limit and result.done >= plan.limit:
            break
        # 批号 + 首批末批的代码摘要当 key。改批大小会让批号对应的内容
        # 变化，所以批大小也在指纹里。
        key = f"{index:05d}"
        if checkpoint.done(key):
            result.skipped += 1
            continue

        opts = _batch_namespace(plan, spec, batch)
        try:
            raw = dispatch(ad, spec, opts)
        except ServerUnavailable as exc:
            # 网关主动不可用。**立刻停手**——继续打只会把两小时拖成三小时。
            # 已完成的批都在 checkpoint 和输出文件里，续跑是安全的。
            result.stopped = str(exc)
            break
        except SessionBusy:
            raise
        except Exception as exc:  # noqa: BLE001
            # 单批失败不该拖垮整轮：记下来，下一轮续跑还会碰到它。
            result.failed += 1
            entry = {"batch": index, "codes": batch, "error": f"{type(exc).__name__}: {exc}"}
            result.failures.append(entry)
            checkpoint.mark(key, entry)
            _append_jsonl(out_path, entry)
            continue

        try:
            payload = normalize(spec, raw)
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            entry = {"batch": index, "codes": batch, "error": f"序列化失败 {type(exc).__name__}: {exc}"}
            result.failures.append(entry)
            checkpoint.mark(key, entry)
            _append_jsonl(out_path, entry)
            continue

        n_codes = _count_codes(payload)
        result.done += 1
        result.codes_ok += n_codes
        result.codes_empty += len(batch) - n_codes
        if n_codes == 0:
            result.empty += 1
        record = {"batch": index, "codes": batch, "codes_with_data": n_codes,
                  "payload": payload}
        # 先落数据再记 checkpoint。反过来的话，崩在中间会记成「已完成」
        # 而数据永远丢了——那比多跑一遍糟糕得多。
        _append_jsonl(out_path, record)
        checkpoint.mark(key, {"codes_with_data": n_codes})
        if on_progress:
            on_progress(index + 1, len(batches), result.codes_ok)

    result.seconds = time.monotonic() - started
    return result


def _batch_namespace(plan: BatchPlan, spec: Any, batch: list[str]) -> argparse.Namespace:
    """造一个只带这一批代码的 opts，复用 CLI 的类型约定。"""
    fields: dict[str, Any] = {
        "category": plan.category, "method": plan.method,
    }
    for name in spec.options:
        if name == "codes":
            fields["codes"] = batch
        else:
            # 缺省值从 CLI 的 parser 拿，保证两边的默认行为一模一样。
            fields[name] = plan.options.get(name)
    return argparse.Namespace(**fields)


def read_codes(path: str | None, inline: str | None) -> list[str]:
    """代码表从文件或逗号串来。**去重但保序**——保序是为了批号在
    续跑时稳定，同一只票不会因为排序变化跑到别的批去。
    """
    raw: list[str] = []
    if inline:
        raw.extend(c.strip() for c in inline.split(","))
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    raw.extend(c.strip() for c in line.split(","))
    seen: set[str] = set()
    codes: list[str] = []
    for code in raw:
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    if not codes:
        raise ValueError("代码表是空的：--codes 和 --codes-file 至少给一个有内容的")
    return codes
