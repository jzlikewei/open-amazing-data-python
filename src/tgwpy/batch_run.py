#!/usr/bin/env python
"""分批跑一个 AmazingData 方法，一次登录覆盖整张代码表。

替代的是原来「每个批次起一个进程」的写法——那样每次启动都要重拼
交易日历，105 批就是 1260 个请求，光日历就够把网关打到 404，
而且同账号第二条连接直接被拒（-98）。

    set -a && . ./.env && set +a
    python -m src.tgwpy.batch_run --category market --method kline \
        --codes-file /path/to/hs_codes.txt \
        --begin 20260930 --end 20260930 \
        --period day --adjust none \
        --out /tmp/hs_daily.jsonl

中断了就原样再跑一遍：已完成的批次在 checkpoint 里，会被跳过。
换代码表或换日期区间会撞上指纹校验并拒绝复用——确认要重来就加
`--fresh`，或者删掉那个 state 文件。

退出码：
    0  全部批次跑完
    2  参数错误
    3  凭据/登录/网络失败
    4  本地环境缺件
    5  网关不可用，已停手（可以过会儿原样重跑）
    6  有批次失败（跑完了，但产出不全）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 本文件在包内（`src/tgwpy/`），所以 `parent.parent` 就是 `src/` 本身。
# 内部一律 `from tgwpy import ...`，需要 `src/` 在 sys.path 上；
# 被当脚本直接跑（`python src/tgwpy/batch_run.py`）时靠这一行补上。
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tgwpy import cli                      # noqa: E402
from tgwpy.batch import (                  # noqa: E402
    DEFAULT_BATCH_SIZE, BatchPlan, read_codes, run_batches,
)
from tgwpy.guard import (                   # noqa: E402
    BatchCheckpoint, ServerUnavailable, SessionBusy,
)

EXIT_PARTIAL = 6

#: 批处理产物默认落在哪。跟本地数据根同一个家目录约定。
DEFAULT_BATCH_DIR = "~/.amazingdata_local/batch"


def add_batch_args(parser: argparse.ArgumentParser) -> None:
    """批处理专属选项。

    故意**不重复定义** `--codes`：方法自己的 `--codes` 就是整张代码表，
    这里只负责把它切批。两处同名会让「我给的是全表还是一批」彻底说不清。
    """
    parser.add_argument(
        "--codes-file", metavar="PATH",
        help="代码表文件，一行一个，# 之后是注释。逗号分隔也认",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, metavar="N",
        help=f"每批多少只代码，默认 {DEFAULT_BATCH_SIZE}",
    )
    parser.add_argument(
        "--state", metavar="PATH",
        help="断点续跑状态文件。默认按方法+日期算在批处理目录下",
    )
    parser.add_argument(
        "--out", metavar="PATH",
        help="结果 JSONL，一行一个批次。默认按方法+日期算在批处理目录下",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="本次最多跑几批，0 = 全部。先试跑用",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="清掉已有的断点状态从头跑。改了代码表/日期区间时用它",
    )
    parser.add_argument(
        "--wait-lock", type=float, default=0.0, metavar="SECONDS",
        help="拿不到单会话锁时等多久。0 = 立刻失败",
    )
    parser.add_argument(
        "--report", metavar="PATH",
        help="把本轮统计写到这个 JSON 文件，默认不写",
    )


def defaults_for(category: str, method: str, opts: argparse.Namespace) -> tuple[str, str]:
    """按方法+日期算默认的 out / state 路径。

    带上日期是为了「每天一个文件」的直觉：昨天的结果不会被今天的
    续跑覆盖，checkpoint 也就自然按天分开。
    """
    base = os.path.abspath(os.path.expanduser(
        os.environ.get("TGW_BATCH_DIR") or DEFAULT_BATCH_DIR))
    stamp = str(getattr(opts, "begin", "") or getattr(opts, "end", "")
                or getattr(opts, "date", "") or "adhoc")
    stem = f"{category}_{method}_{stamp}"
    return (os.path.join(base, stem + ".jsonl"),
            os.path.join(base, stem + ".state.json"))


def _iter_method_parsers(parser: argparse.ArgumentParser):
    """三层：category → method → 带 --codes 的那个 action。"""
    for action in parser._subparsers._group_actions:          # noqa: SLF001
        for cat in action.choices.values():
            for sub in cat._subparsers._group_actions:        # noqa: SLF001
                for method in sub.choices.values():
                    yield method


def build_parser() -> argparse.ArgumentParser:
    """CLI 的解析器 + 批处理选项。

    复用 `cli.build_parser()` 而不是抄一份方法选项：39 个方法的参数
    契约在 CATEGORIES 里是唯一事实源，抄一份必然会漂。
    """
    parser = cli.build_parser()
    for method in _iter_method_parsers(parser):
        add_batch_args(method)
        # 批处理里 `--codes` 是**整张代码表**，由 `--codes-file` 提供时
        # 不该再强制。方法契约要求它必填，是为了防单次调用漏掉代码——
        # 批处理有 `--codes-file` 兜着，这条约束在这里是多余的。
        for act in method._actions:                          # noqa: SLF001
            if act.dest == "codes":
                act.required = False
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    os.environ.setdefault("TGW_LOCK_WAIT", "0")
    parser = build_parser()
    try:
        opts, extra = parser.parse_known_args(argv)
    except SystemExit as exc:
        return int(exc.code or cli.EXIT_OK)
    if extra:
        print(f"无法识别的参数：{' '.join(extra)}", file=sys.stderr)
        return cli.EXIT_USAGE
    if not opts.category or not opts.method:
        print("缺少 <category> 或 <method>", file=sys.stderr)
        return cli.EXIT_USAGE
    if opts.method not in cli.CATEGORIES.get(opts.category, {}):
        print(f"没有这个方法：{opts.category} {opts.method}", file=sys.stderr)
        return cli.EXIT_USAGE
    # **不要**用 `cli.run(argv)` 拿 spec：它走的是严格的 CLI 解析器，
    # 认不出 `--codes-file` 之类的批处理选项，会先报「--codes 必填」。
    spec = cli.CATEGORIES[opts.category][opts.method]

    try:
        codes = read_codes(opts.codes_file,
                           ",".join(opts.codes) if getattr(opts, "codes", None) else None)
    except (OSError, ValueError) as exc:
        print(f"代码表读不出来：{exc}", file=sys.stderr)
        return cli.EXIT_USAGE
    # 代码校验原先在 `cli.dispatch` 里，登录之后才拦；现在 `cli.run` 解析阶段
    # 就拦，批处理不走 `cli.run`，在登录前自己拦一次。
    try:
        if "codes" in spec.options:
            cli._check_codes(codes, spec)
        cli._check_range(opts)
    except cli.CliError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return cli.EXIT_USAGE

    out_path = opts.out
    state_path = opts.state
    if not out_path or not state_path:
        d_out, d_state = defaults_for(spec.category if hasattr(spec, "category")
                                      else opts.category, spec.method, opts)
        out_path = out_path or d_out
        state_path = state_path or d_state

    plan = BatchPlan(
        category=opts.category, method=opts.method,
        options={k: v for k, v in vars(opts).items()
                 if k not in ("codes", "codes_file", "batch_size", "state",
                              "out", "limit", "fresh", "wait_lock", "report",
                              "category", "method")},
        codes=codes, batch_size=opts.batch_size, limit=opts.limit,
    )

    try:
        if opts.fresh and os.path.exists(state_path):
            os.remove(state_path)
        checkpoint = BatchCheckpoint(state_path, plan.fingerprint())
    except RuntimeError as exc:
        print(f"断点状态不可用：{exc}", file=sys.stderr)
        return cli.EXIT_USAGE

    pending = [b for i, b in enumerate(plan.batches())
               if not checkpoint.done(f"{i:05d}")]
    print(f"{len(plan.batches())} 批，已完成 {len(plan.batches()) - len(pending)}，"
          f"本次要跑 {len(pending)} 批（{len(codes)} 只，批大小 {opts.batch_size}）",
          file=sys.stderr)

    if not pending and not opts.limit:
        print("没有待跑的批次。--fresh 可以强制重来。", file=sys.stderr)
        return cli.EXIT_OK

    try:
        os.environ["TGW_LOCK_WAIT"] = str(opts.wait_lock)
        ad = cli.load_amazingdata()
    except cli.SetupError as exc:
        print(f"环境缺件：{exc}", file=sys.stderr)
        return cli.EXIT_ENV
    except (cli.RuntimeFailure, SessionBusy) as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return cli.EXIT_RUNTIME

    def progress(done: int, total: int, codes_ok: int) -> None:
        print(f"  {done}/{total} 批，累计 {codes_ok} 只拿到数据", file=sys.stderr)

    try:
        result = run_batches(ad, plan, spec, checkpoint, out_path,
                             on_progress=progress)
    except ServerUnavailable as exc:
        print(f"网关不可用，已停手：{exc}", file=sys.stderr)
        return EXIT_PARTIAL if len(checkpoint) else cli.EXIT_SERVER
    finally:
        cli.release_session()

    report = result.as_dict()
    if opts.report:
        Path(opts.report).parent.mkdir(parents=True, exist_ok=True)
        Path(opts.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
    print(f"结果写入 {out_path}", file=sys.stderr)

    if result.stopped:
        print(f"网关不可用，已停手：{result.stopped}", file=sys.stderr)
        return cli.EXIT_SERVER
    if result.failed:
        return EXIT_PARTIAL
    return cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
