"""批量任务打网关时的三道保险：单会话、节流退避、断点续跑。

跑挂的原因写在这里，都是实测来的：

## 1. 同一账号同时只允许一条连接

第二次登录必被拒，`status=-98`，`ForceLogout` 取 0 或 1 都一样。
批处理经常「起一个总控进程再 fork 几个 worker」，那必然撞上。
`SessionLock` 用 `flock` 把账号级互斥做在进程外——被 kill 的进程
内核会替它释放锁，不会留下死锁。

## 2. 请求打太快会被 nginx 拒

一次批量实测（三轮全量 + 组合试探，累计 150+ 请求）把网关打到
`HTTP 404` 约两小时。**404 是后端服务不可用，不是 IP 被封**——限流
会回 429。它是「已经进到 nginx、但后端没人接」。

`RequestGate` 做两件事：出站最小间隔（节流），以及连续失败后指数退避
并在超过阈值时**直接抛异常停手**。停手比继续打强——继续打只会把两小时
拖成三小时。

## 3. 断点续跑

5205 只股票按 50 只一批是 105 批，跑第 60 批断电的话从头再来就是
纯浪费，而且再来一遍又会踩第 2 条。`BatchCheckpoint` 把「哪几批已经
拿到数据」落盘，重跑时跳过。

指纹（fingerprint）防的是**拿旧状态跑新任务**：改了代码表或日期区间
却复用上次的 checkpoint，会静默跳过没下过的批次。看到指纹对不上直接
拒绝，让人自己决定是清状态还是换任务。
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import random
import threading
import time
from typing import Any

#: 出站请求的最小间隔（秒）。节流的主要旋钮。
DEFAULT_MIN_INTERVAL = 0.4

#: 连续失败后的第一档退避（秒），之后翻倍。
DEFAULT_BACKOFF_BASE = 2.0

#: 退避上限（秒）。再长也没意义——直接停手更诚实。
DEFAULT_BACKOFF_MAX = 60.0

#: 连续失败多少次就停手。实测触发的 404 会持续约两小时，
#: 5 次足够确认「不是抖一下」，又不至于把偶发失败误杀。
DEFAULT_MAX_FAILURES = 5

#: 单会话锁文件的根目录。按账号分文件（`user@host`），所以不同账号
#: 互不干扰——账号级约束就该按账号互斥。
DEFAULT_LOCK_ROOT = "~/.tgwpy"


class SessionBusy(RuntimeError):
    """同账号已经有另一个进程在打网关。

    单独立一个类型：批处理要能 catch 它并**等一会儿再试**，
    而不是当成运行失败退出——持锁的进程可能几分钟后自己就跑完了。
    """


class ServerUnavailable(RuntimeError):
    """连续失败超过阈值，判定网关不可用，主动停手。"""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class SessionLock:
    """账号级单会话锁。

    用 `flock` 而不是进程内 `threading.Lock`：约束来自**网关**
    （第二个人登录就是 -98），两个进程各有一个线程锁等于没锁。

    锁粒度是 `user@host`——同一账号同一网关才互斥，换账号照跑。
    """

    def __init__(self, user: str, host: str, root: str | None = None):
        self.user = user
        self.host = host
        self.root = os.path.abspath(
            os.path.expanduser(root or os.environ.get("TGW_LOCK_ROOT")
                               or DEFAULT_LOCK_ROOT)
        )
        self.path = os.path.join(self.root, self._key() + ".lock")
        self._fd: int | None = None

    def _key(self) -> str:
        """账号标识。**不落明文口令，也不落用户名**——锁文件名会被
        `ls` 看见，用户名是半条凭据。只留哈希。"""
        raw = f"{self.user}@{self.host}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def acquire(self, blocking: bool = False, timeout: float = 0.0) -> bool:
        """拿锁。拿不到返回 False（`blocking=False`）或阻塞到超时。

        `blocking=True` 时按 `timeout` 轮询重试。网关会话要几分钟才
        释放，等一下比直接失败合算。
        """
        os.makedirs(self.root, exist_ok=True)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if not blocking or time.monotonic() >= deadline:
                    return False
                # 轮询间隔给大点：等的是网关放会话，最快也是秒级。
                time.sleep(min(2.0, max(0.2, deadline - time.monotonic())))
                continue
            self._fd = fd
            os.truncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
            return True

    def require(self, timeout: float = 0.0) -> "SessionLock":
        """拿不到锁就抛 `SessionBusy`，把等锁这件事交给调用方决定。"""
        if not self.acquire(blocking=timeout > 0, timeout=timeout):
            raise SessionBusy(
                f"账号 {self.user[:2]}*** 在 {self.host} 上已有会话在跑"
                f"（锁 {self.path}）。同一账号同时只允许一条连接，"
                f"第二次登录会被网关拒（status=-98）。"
            )
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)   # 关 fd 也会释放 flock，析构路径不会漏

    def __enter__(self) -> "SessionLock":
        # 已经自己拿着锁就别再 acquire 一次——`flock` 挂在 open file
        # description 上，同进程另开一个 fd 再 flock，即使同一把锁也会
        # 判成冲突。`with lock.require():` 是个很自然的写法，不该炸。
        if self._fd is None:
            self.require()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class RequestGate:
    """出站请求的节流 + 失败退避。

    装在传输层的**唯一发送路径**上（`WssTransport._dispatch`），
    资讯族和行情族都过它——两个分开放闸门等于有一个形同虚设。

    失败计数来自响应侧（`status != 0` 或断链），是个**启发式**而非
    精确记账：响应是异步回来的，与发送不同序，同一次发送可能先收到
    后一发的成功应答。用它做「连续不对劲就停手」的判断够用，用它做
    精确统计不够。
    """

    def __init__(self, min_interval: float | None = None,
                 backoff_base: float | None = None,
                 backoff_max: float | None = None,
                 max_failures: int | None = None):
        self.min_interval = (min_interval if min_interval is not None
                             else _env_float("TGW_MIN_INTERVAL", DEFAULT_MIN_INTERVAL))
        self.backoff_base = (backoff_base if backoff_base is not None
                             else _env_float("TGW_BACKOFF_BASE", DEFAULT_BACKOFF_BASE))
        self.backoff_max = (backoff_max if backoff_max is not None
                            else _env_float("TGW_BACKOFF_MAX", DEFAULT_BACKOFF_MAX))
        self.max_failures = (max_failures if max_failures is not None
                             else int(_env_float("TGW_MAX_FAILURES", DEFAULT_MAX_FAILURES)))
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._failures = 0
        self.sent = 0
        self.last_reason = ""

    # -- 闸门 ---------------------------------------------------------------

    def wait(self) -> float:
        """发送前调用。返回本次实际睡的秒数（0 表示没睡）。

        睡两段：距上次发送的间隔（节流），以及当前失败档位的退避。
        失败超阈值在这里抛，不等到发出去——网关已经不对劲了，
        再发一帧只是让它多拒一次。
        """
        with self._lock:
            if self._failures >= self.max_failures:
                raise ServerUnavailable(
                    f"连续 {self._failures} 次失败（最近一条：{self.last_reason}），"
                    f"判定网关不可用，停手。实测这种 404 会持续约两小时，"
                    f"继续打只会拖长。等一会儿再重跑，已完成的批次在 checkpoint 里。"
                )
            now = time.monotonic()
            gap = self.min_interval - (now - self._last_send)
            backoff = self.backoff_base * (2 ** (self._failures - 1)) if self._failures else 0.0
            backoff = min(backoff, self.backoff_max)
            # 抖动：多个进程/线程的退避不会撞在同一秒上一起醒。
            if backoff:
                backoff *= 0.8 + 0.4 * random.random()
            delay = max(0.0, gap) + max(0.0, backoff)
            self._last_send = now + delay   # 把睡掉的时间算进去
            self.sent += 1
        if delay > 0:
            time.sleep(delay)
        return delay

    # -- 记账 ---------------------------------------------------------------

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self, reason: str = "") -> None:
        with self._lock:
            self._failures += 1
            self.last_reason = reason

    @property
    def failures(self) -> int:
        return self._failures

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"sent": self.sent, "failures": self._failures,
                    "min_interval": self.min_interval,
                    "last_reason": self.last_reason}


class BatchCheckpoint:
    """分批任务的断点续跑状态。

    落盘而不是放内存：断电、OOM、被网关打到 2 小时 404 之后，
    进程内存里的进度全没了，而那正是最需要续跑的时刻。

    每次 `mark` 都 `fsync`——不 fsync 的话「写进 page cache 了」
    和「崩了还在」是两回事，而我们要的正是后者。
    """

    def __init__(self, path: str, fingerprint: str):
        self.path = path
        self.fingerprint = fingerprint
        self._done: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            try:
                payload = json.load(fh)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"checkpoint {self.path} 读不出来（{exc}）。"
                    f"删掉它重跑，或者换一个 --state 路径——"
                    f"拿坏状态续跑比从头来危险。"
                ) from exc
        stored = payload.get("fingerprint")
        if stored != self.fingerprint:
            raise RuntimeError(
                f"checkpoint {self.path} 是别的任务留下的"
                f"（指纹 {stored} != 本次 {self.fingerprint}）。"
                f"直接续跑会跳过没下过的批次。换 --state 路径，"
                f"或者确认要重来就删掉 {self.path}。"
            )
        self._done = dict(payload.get("done") or {})

    def done(self, key: str) -> bool:
        return key in self._done

    def get(self, key: str) -> Any:
        return self._done.get(key)

    def mark(self, key: str, meta: Any = None) -> None:
        with self._lock:
            self._done[key] = meta
            self._flush()

    def mark_many(self, keys: list[str], meta: Any = None) -> None:
        with self._lock:
            for key in keys:
                self._done[key] = meta
            self._flush()

    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".",
                    exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": self.fingerprint, "done": self._done},
                      fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)   # 原子替换：不会读到写了一半的状态

    def reset(self) -> None:
        with self._lock:
            self._done.clear()
            if os.path.exists(self.path):
                os.remove(self.path)

    def keys(self) -> list[str]:
        return sorted(self._done)

    def __len__(self) -> int:
        return len(self._done)
