"""批处理的续跑语义。用假 dispatch 跑，验证的是**账目**不是协议。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tgwpy.batch import (                             # noqa: E402
    BatchCheckpoint, BatchPlan, read_codes, run_batches,
)
from tgwpy.cli import CATEGORIES                      # noqa: E402
from tgwpy.guard import ServerUnavailable              # noqa: E402

SPEC = CATEGORIES["market"]["kline"]


def plan_for(codes, **kw) -> BatchPlan:
    return BatchPlan(
        category="market", method="kline",
        options={"begin": 20260930, "end": 20260930,
                 "period": "day", "adjust": "none"},
        codes=codes, **kw)


def ok(codes):
    """每只都有一行数据的假响应。"""
    return {c: [{"close": 1.0}] for c in codes}


def load(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# -- 切批 ----------------------------------------------------------------

def test_batches_split_by_size():
    assert plan_for([f"{i}.SH" for i in range(5)], batch_size=2).batches() == [
        ["0.SH", "1.SH"], ["2.SH", "3.SH"], ["4.SH"]]


def test_last_batch_may_be_short():
    assert plan_for(["a", "b", "c"], batch_size=2).batches() == [["a", "b"], ["c"]]


def test_fingerprint_is_stable_for_same_plan():
    assert plan_for(["a", "b"]).fingerprint() == plan_for(["a", "b"]).fingerprint()


@pytest.mark.parametrize("other", [
    {"codes": ["a", "c"]},                       # 代码表变了
    {"options": {"begin": 20260929, "end": 20260929, "period": "day", "adjust": "none"}},
    {"batch_size": 7},
    {"method": "snapshot"},
])
def test_fingerprint_changes_with_the_job(other):
    """换了宇宙/日期/批大小却复用旧 checkpoint，会静默跳过没下过的批。"""
    kw = {"category": "market", "method": "kline",
          "options": {"begin": 20260930, "end": 20260930,
                      "period": "day", "adjust": "none"},
          "codes": ["a", "b"], "batch_size": 2}
    kw.update(other)
    assert BatchPlan(**kw).fingerprint() != plan_for(["a", "b"], batch_size=2).fingerprint()


# -- 代码表 --------------------------------------------------------------

def test_read_codes_keeps_order_and_dedupes():
    got = read_codes(None, "600519.SH,000858.SZ,600519.SH")
    assert got == ["600519.SH", "000858.SZ"]


def test_read_codes_from_file_with_comments(tmp_path):
    path = tmp_path / "codes.txt"
    path.write_text("# 沪深\n600519.SH\n000858.SZ,600036.SH  # 同一行两个\n\n",
                    encoding="utf-8")
    assert read_codes(str(path), None) == ["600519.SH", "000858.SZ", "600036.SH"]


def test_read_codes_merges_both_sources(tmp_path):
    path = tmp_path / "codes.txt"
    path.write_text("000001.SZ\n", encoding="utf-8")
    assert read_codes(str(path), "600519.SH") == ["600519.SH", "000001.SZ"]


def test_read_codes_rejects_empty():
    with pytest.raises(ValueError):
        read_codes(None, None)


# -- 续跑 ----------------------------------------------------------------

def test_runs_every_batch_first_time(tmp_path):
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    cp = BatchCheckpoint(state, plan_for(["a", "b", "c"]).fingerprint())
    seen = []

    def dispatch(ad, spec, opts):
        seen.append(list(opts.codes))
        return ok(opts.codes)

    res = run_batches(None, plan_for(["a", "b", "c"], batch_size=1),
                      SPEC, cp, out, dispatch=dispatch)
    assert seen == [["a"], ["b"], ["c"]]
    assert res.done == 3 and res.failed == 0
    assert len(load(out)) == 3
    assert res.codes_ok == 3


def test_resume_skips_completed_batches(tmp_path):
    """断点续跑的核心：已完成的批不能再打一次网关。"""
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b", "c", "d"], batch_size=1)
    cp = BatchCheckpoint(state, plan.fingerprint())
    cp.mark("00000")
    cp.mark("00001")

    seen = []

    def dispatch(ad, spec, opts):
        seen.append(opts.codes[0])
        return ok(opts.codes)

    res = run_batches(None, plan, SPEC, cp, out, dispatch=dispatch)
    assert seen == ["c", "d"]
    assert res.skipped == 2 and res.done == 2
    # 输出文件是追加的，老的两行还在
    assert len(load(out)) == 2


def test_data_is_written_before_checkpoint(tmp_path):
    """顺序反了的话，崩在中间会记成「已完成」而数据永远丢了。"""
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a"], batch_size=1)
    cp = BatchCheckpoint(state, plan.fingerprint())
    real_mark = cp.mark

    def mark(key, meta=None):
        assert Path(out).exists(), "checkpoint 落笔时数据还没进文件"
        real_mark(key, meta)

    cp.mark = mark
    run_batches(None, plan, SPEC, cp, out, dispatch=lambda a, s, o: ok(o.codes))
    assert Path(out).exists()


def test_empty_batch_counted_not_failed(tmp_path):
    """整批都没数据（停牌/库里没这一天）和查询失败是两回事。"""
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b"], batch_size=2)
    cp = BatchCheckpoint(state, plan.fingerprint())
    res = run_batches(None, plan, SPEC, cp, out,
                      dispatch=lambda a, s, o: {})
    assert res.done == 1 and res.empty == 1 and res.failed == 0
    assert res.codes_ok == 0 and res.codes_empty == 2


def test_partial_payload_counts_only_codes_with_data(tmp_path):
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b"], batch_size=2)
    cp = BatchCheckpoint(state, plan.fingerprint())
    res = run_batches(None, plan, SPEC, cp, out,
                      dispatch=lambda a, s, o: {"a": [{"close": 1.0}], "b": []})
    assert res.codes_ok == 1 and res.codes_empty == 1


def test_one_bad_batch_does_not_kill_the_run(tmp_path):
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b", "c"], batch_size=1)
    cp = BatchCheckpoint(state, plan.fingerprint())
    calls = []

    def dispatch(ad, spec, opts):
        code = opts.codes[0]
        calls.append(code)
        if code == "b":
            raise ValueError("炸了")
        return ok(opts.codes)

    res = run_batches(None, plan, SPEC, cp, out, dispatch=dispatch)
    assert calls == ["a", "b", "c"]           # 炸了也继续往下走
    assert res.done == 2 and res.failed == 1
    assert "炸了" in res.failures[0]["error"]
    # 失败的那批也记进 checkpoint：重跑时它已经被标记过
    assert len(cp) == 3


def test_serialization_failure_is_recorded(tmp_path):
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a"], batch_size=1)
    cp = BatchCheckpoint(state, plan.fingerprint())

    def boom(spec, raw):
        raise TypeError("转不了")

    res = run_batches(None, plan, SPEC, cp, out,
                      dispatch=lambda a, s, o: ok(o.codes), normalize=boom)
    assert res.failed == 1 and "序列化失败" in res.failures[0]["error"]


def test_server_unavailable_stops_immediately(tmp_path):
    """网关不可用时停手。已完成的批在 checkpoint 和输出里，续跑安全。"""
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b", "c", "d"], batch_size=1)
    cp = BatchCheckpoint(state, plan.fingerprint())
    calls = []

    def dispatch(ad, spec, opts):
        calls.append(opts.codes[0])
        if len(calls) >= 2:
            raise ServerUnavailable("连续 5 次失败")
        return ok(opts.codes)

    res = run_batches(None, plan, SPEC, cp, out, dispatch=dispatch)
    assert calls == ["a", "b"]                # 没有继续打 c / d
    assert res.done == 1 and res.stopped
    assert len(load(out)) == 1
    # 续跑时从 b 开始
    again = run_batches(None, plan, SPEC, cp, out,
                        dispatch=lambda a, s, o: ok(o.codes))
    assert again.skipped == 1 and again.done == 3


def test_limit_caps_this_run_only(tmp_path):
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for([f"{i}" for i in range(10)], batch_size=1, limit=3)
    cp = BatchCheckpoint(state, plan.fingerprint())
    res = run_batches(None, plan, SPEC, cp, out,
                      dispatch=lambda a, s, o: ok(o.codes))
    assert res.done == 3
    assert len(cp) == 3
    # 不带 limit 再跑，补齐剩下的
    rest = run_batches(None, plan_for([f"{i}" for i in range(10)], batch_size=1),
                       SPEC, cp, out, dispatch=lambda a, s, o: ok(o.codes))
    assert rest.done == 7 and rest.skipped == 3


def test_namespace_carries_the_batch_codes(tmp_path):
    """批里的代码必须真的进了 --codes 位，否则等于白切。"""
    seen = []

    def dispatch(ad, spec, opts):
        seen.append(dict(vars(opts)))
        return ok(opts.codes)

    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b", "c"], batch_size=2)
    run_batches(None, plan, SPEC, BatchCheckpoint(state, plan.fingerprint()),
                out, dispatch=dispatch)
    assert [s["codes"] for s in seen] == [["a", "b"], ["c"]]
    for s in seen:
        assert s["period"] == "day" and s["adjust"] == "none"
        assert s["begin"] == 20260930 and s["end"] == 20260930
    # options 里不该混进批处理的旋钮——它们不是方法参数
    assert "batch_size" not in seen[0]


def test_report_shape(tmp_path):
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b"], batch_size=2)
    res = run_batches(None, plan, SPEC, BatchCheckpoint(state, plan.fingerprint()),
                      out, dispatch=lambda a, s, o: ok(o.codes))
    d = res.as_dict()
    assert set(d) == {"total", "done", "skipped", "failed", "empty",
                      "codes_ok", "codes_empty", "stopped", "seconds", "failures"}


def test_output_file_is_one_json_object_per_line(tmp_path):
    """JSONL：一行一个批次，续跑时读到最后一行就够，不用整个重解析。"""
    out, state = str(tmp_path / "o.jsonl"), str(tmp_path / "s.json")
    plan = plan_for(["a", "b", "c"], batch_size=1)
    run_batches(None, plan, SPEC, BatchCheckpoint(state, plan.fingerprint()),
                out, dispatch=lambda a, s, o: ok(o.codes))
    text = Path(out).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.strip().splitlines()) == 3
    for line in text.strip().splitlines():
        json.loads(line)
