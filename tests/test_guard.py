"""`guard` 的三个原语。全部不碰网络——被测的是决策，不是协议。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tgwpy import guard                                    # noqa: E402


# -- RequestGate ---------------------------------------------------------

def test_first_send_does_not_wait():
    """第一次发请求没有「上次发送」可对齐，不该白等一个间隔。"""
    gate = guard.RequestGate(min_interval=5.0, backoff_base=0.0)
    assert gate.wait() == 0.0


def test_min_interval_spaces_sends():
    gate = guard.RequestGate(min_interval=0.15, backoff_base=0.0)
    gate.wait()
    began = time.monotonic()
    slept = gate.wait()
    elapsed = time.monotonic() - began
    assert slept > 0
    assert elapsed >= 0.10          # 留余量，别让 CI 的调度抖动吃成 0


def test_backoff_grows_with_consecutive_failures():
    gate = guard.RequestGate(min_interval=0.0, backoff_base=0.10,
                             backoff_max=10.0)
    gate.record_failure("1")
    first = gate.wait()
    gate.record_failure("2")
    second = gate.wait()
    gate.record_failure("3")
    third = gate.wait()
    assert first < second < third
    # 抖动是 0.8~1.2 倍，翻倍后档位差仍然分得开
    assert second > first * 1.4


def test_backoff_is_capped():
    gate = guard.RequestGate(min_interval=0.0, backoff_base=1.0,
                             backoff_max=0.2, max_failures=99)
    for _ in range(6):
        gate.record_failure("x")
    assert gate.wait() <= 0.2 * 1.2 + 0.01


def test_success_resets_backoff():
    """一次成功就该把退避清零——连续失败记的是「连续」。"""
    gate = guard.RequestGate(min_interval=0.0, backoff_base=1.0)
    gate.record_failure("a")
    gate.record_failure("b")
    gate.record_success()
    assert gate.failures == 0
    assert gate.wait() == 0.0


def test_trips_and_raises_at_threshold():
    """超过阈值直接停手。网关已经不对劲了，再发只是让它多拒一次。"""
    gate = guard.RequestGate(min_interval=0.0, backoff_base=0.0, max_failures=3)
    for i in range(3):
        gate.record_failure(f"boom-{i}")
    with pytest.raises(guard.ServerUnavailable) as exc:
        gate.wait()
    assert "boom-2" in str(exc.value)
    assert "约两小时" in str(exc.value)


def test_trip_does_not_send():
    gate = guard.RequestGate(min_interval=0.0, backoff_base=0.0, max_failures=1)
    gate.record_failure("dead")
    before = gate.state()["sent"]
    with pytest.raises(guard.ServerUnavailable):
        gate.wait()
    assert gate.state()["sent"] == before


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("TGW_MIN_INTERVAL", "1.5")
    monkeypatch.setenv("TGW_MAX_FAILURES", "9")
    gate = guard.RequestGate()
    assert gate.min_interval == 1.5
    assert gate.max_failures == 9


def test_bad_env_value_falls_back(monkeypatch):
    """环境变量写错不该让程序起不来——用默认值接着跑。"""
    monkeypatch.setenv("TGW_MIN_INTERVAL", "not-a-number")
    assert guard.RequestGate().min_interval == guard.DEFAULT_MIN_INTERVAL


# -- SessionLock ---------------------------------------------------------

def test_second_lock_on_same_account_is_blocked(tmp_path):
    a = guard.SessionLock("alice", "gw", root=str(tmp_path))
    b = guard.SessionLock("alice", "gw", root=str(tmp_path))
    a.require()
    with pytest.raises(guard.SessionBusy) as exc:
        b.require()
    assert "-98" in str(exc.value)      # 得把症状写出来，否则只当凭据问题
    a.release()
    b.require()
    b.release()


def test_different_accounts_do_not_collide(tmp_path):
    a = guard.SessionLock("alice", "gw", root=str(tmp_path))
    b = guard.SessionLock("bob", "gw", root=str(tmp_path))
    a.require()
    b.require()            # 同网关不同账号，网关不认它们互斥
    a.release()
    b.release()


def test_different_hosts_do_not_collide(tmp_path):
    a = guard.SessionLock("alice", "gw1", root=str(tmp_path))
    b = guard.SessionLock("alice", "gw2", root=str(tmp_path))
    a.require()
    b.require()
    a.release()
    b.release()


def test_lock_filename_hides_the_account(tmp_path):
    """锁文件名会被人 `ls` 看见，用户名是半条凭据。"""
    lock = guard.SessionLock("alice", "gw", root=str(tmp_path))
    assert "alice" not in lock.path
    assert not os.path.exists(lock.path)      # 只在 require 时才建文件
    lock.require()
    try:
        assert "alice" not in open(lock.path).read()
    finally:
        lock.release()


def test_release_is_idempotent(tmp_path):
    lock = guard.SessionLock("alice", "gw", root=str(tmp_path))
    lock.require()
    lock.release()
    lock.release()         # 收尾路径常被调用两次，不该炸


def test_context_manager_releases(tmp_path):
    lock = guard.SessionLock("alice", "gw", root=str(tmp_path))
    with lock:
        assert guard.SessionLock("alice", "gw", root=str(tmp_path)).acquire() is False
    # 出了 with 就能再拿——锁真的还回去了
    other = guard.SessionLock("alice", "gw", root=str(tmp_path))
    other.require()
    other.release()


def test_require_result_usable_as_context_manager(tmp_path):
    """`with lock.require():` 是很自然的写法，不该因为二次加锁而炸。"""
    lock = guard.SessionLock("alice", "gw", root=str(tmp_path))
    with lock.require():
        pass
    assert guard.SessionLock("alice", "gw", root=str(tmp_path)).acquire() is True


def test_blocking_acquire_times_out(tmp_path):
    a = guard.SessionLock("alice", "gw", root=str(tmp_path))
    b = guard.SessionLock("alice", "gw", root=str(tmp_path))
    a.require()
    began = time.monotonic()
    assert b.acquire(blocking=True, timeout=0.3) is False
    assert time.monotonic() - began >= 0.2
    a.release()


# -- BatchCheckpoint -----------------------------------------------------

def test_mark_then_reopen_sees_progress(tmp_path):
    path = str(tmp_path / "state.json")
    cp = guard.BatchCheckpoint(path, "fp1")
    cp.mark("00000", {"codes_with_data": 3})
    cp.mark("00001", {"codes_with_data": 5})

    again = guard.BatchCheckpoint(path, "fp1")
    assert again.done("00000") and again.done("00001")
    assert again.get("00001") == {"codes_with_data": 5}
    assert len(again) == 2
    assert again.keys() == ["00000", "00001"]


def test_fingerprint_mismatch_is_refused(tmp_path):
    """拿别的任务的 checkpoint 续跑会静默跳过没下过的批，必须挡住。"""
    path = str(tmp_path / "state.json")
    guard.BatchCheckpoint(path, "fp-A").mark("00000")
    with pytest.raises(RuntimeError) as exc:
        guard.BatchCheckpoint(path, "fp-B")
    assert "fp-A" in str(exc.value) and "fp-B" in str(exc.value)


def test_corrupt_state_is_refused(tmp_path):
    """坏状态比没状态危险——宁可不跑。"""
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        guard.BatchCheckpoint(str(path), "fp1")
    assert "读不出来" in str(exc.value)


def test_reset_clears(tmp_path):
    path = str(tmp_path / "state.json")
    cp = guard.BatchCheckpoint(path, "fp1")
    cp.mark("00000")
    cp.reset()
    assert len(cp) == 0
    assert not os.path.exists(path)


def test_mark_many_is_atomic(tmp_path):
    path = str(tmp_path / "state.json")
    cp = guard.BatchCheckpoint(path, "fp1")
    cp.mark_many([f"{i:05d}" for i in range(50)])
    assert len(guard.BatchCheckpoint(path, "fp1")) == 50
    assert not os.path.exists(path + ".tmp")      # 临时文件没留下
