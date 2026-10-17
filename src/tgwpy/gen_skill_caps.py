#!/usr/bin/env python3
"""从 docs/skill-capabilities.json 生成人读版 docs/skill-capabilities.md。

JSON 是 SSOT（抄自 SKILL.md），Markdown 只是它的视图。视图手改就会和
事实源漂移，所以 `tests/test_skill_contract.py` 会重新生成一遍比对字节。

用法：
    python -m src.tgwpy.gen_skill_caps          # 写文件
    python -m src.tgwpy.gen_skill_caps --check  # 只校验，不写
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

#: 本文件在包内（`src/tgwpy/`），所以 `parent.parent` 是 `src/`，
#: 仓库根要再往上退一级。跟 batch_run.py 同一个处境。
ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "docs" / "skill-capabilities.json"
DST = ROOT / "docs" / "skill-capabilities.md"

STATUS_LABEL = {
    "verified": "✅ 已验证",
    "unverified": "🟡 待网检验证",
    "param_bizerr": "⚠️ 参数待调",
    "impl_stub": "🚧 代码待写",
    "untested": "❓ 未测过",
}
CAT_TITLE = {
    "base": "基础数据 base",
    "market": "行情数据 market",
    "finance": "财务数据 finance",
    "shareholder": "股东股本 shareholder",
    "index": "指数与行业 index",
    "other": "其他 other",
}


def _cell(text: str | None) -> str:
    """表格单元里的竖线要转义，换行要压平。"""
    if not text:
        return "—"
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_execution(exe: dict | None) -> list[str]:
    """渲染执行清单。cron 的 Director 就是照这个决定下一批做什么的。"""
    if not exe:
        return []
    layers = exe["layers"]
    done = sum(1 for l in layers for i in l["items"] if i["done"])
    total = sum(len(l["items"]) for l in layers)

    out = ["## 执行清单", ""]
    out.append(f"**{done} / {total} 完成。** 这是 cron 的唯一需求来源，"
               "Director 每次 tick 只推进最细的未完成层。")
    out.append("")
    out.append("| 层 | 项 | 状态 | 解锁 |")
    out.append("| --- | --- | --- | --- |")
    for layer in layers:
        for item in layer["items"]:
            box = "✅" if item["done"] else "⬜"
            unlocks = "、".join(f"`{u}`" for u in item["unblocks"])
            out.append(f"| `{layer['id']}` | `{item['id']}` {_cell(item['title'])} "
                       f"| {box} | {unlocks} |")
    out.append("")

    for layer in layers:
        out.append(f"### {layer['id']} · {layer['name']}")
        out.append("")
        out.append(f"{layer['why']}")
        out.append("")
        for item in layer["items"]:
            box = "x" if item["done"] else " "
            out.append(f"- [{box}] **{item['id']}** {_cell(item['title'])}")
            if item.get("depends_on"):
                out.append(f"  - 依赖：{'、'.join(f'`{x}`' for x in item['depends_on'])}")
            for g in item["gate"]:
                out.append(f"  - 门禁：{_cell(g)}")
        out.append("")

    out.append("### 通用门禁")
    out.append("")
    for rule in exe.get("gate_rules", []):
        out.append(f"- {rule}")
    out.append("")
    return out


def render(spec: dict) -> str:
    caps = spec["capabilities"]
    counts = Counter(c["status"] for c in caps)

    out: list[str] = []
    out.append("<!-- 本文件由 src/tgwpy/gen_skill_caps.py 从 skill-capabilities.json 生成，")
    out.append("     不要手改。改事实源后重跑那个脚本。 -->")
    out.append("")
    out.append("# /amazingdata skill 能力清单与实现缺口")
    out.append("")
    out.append("SSOT 是 `skill-capabilities.json`，对外方法契约的表格——")
    out.append("那是用户实际会调的接口面。这张表回答一个问题：")
    out.append("**这 39 个能力里，我们现在能真的跑通几个，剩下几个卡在哪。**")
    out.append("")
    out.append("## 状态口径")
    out.append("")
    out.append("| 状态 | 数量 | 含义 |")
    out.append("| --- | --- | --- |")
    out.append(f"| ✅ 已验证 | {counts['verified']} | 真实网关上该 function_id 回了真实数据，"
               "字段与参照实现逐项对得上 |")
    out.append(f"| 🟡 待网检验证 | {counts['unverified']} | 代码写完并用抓包原文+真业务层验过，"
               "但没跑过真实网关 |")
    out.append(f"| ⚠️ 参数待调 | {counts['param_bizerr']} | 协议通、服务端回业务错误，样例参数不对 |")
    out.append(f"| 🚧 代码待写 | {counts['impl_stub']} | 底层 compat 调用还是 stub |")
    out.append(f"| ❓ 未测过 | {counts['untested']} | 从未测过 |")
    out.append("")
    proven = [c for c in caps if c.get("evidence")]
    if proven:
        out.append(f"「已验证」里只有 **{len(proven)} 个**跑过**完整 CLI 链**并与生产库对账，"
                   "其余是协议层实测。各方法下带出处的就是这批：")
        out.append("")
    out.append("## 汇总")
    out.append("")
    out.append(f"**{counts['verified']} / {len(caps)} 已验证。**")
    out.append("")
    out.append("按阻塞原因分：")
    out.append("")
    out.append("| 阻塞面 | 方法数 | 方法 |")
    out.append("| --- | --- | --- |")
    for status in ("unverified", "impl_stub", "param_bizerr", "untested"):
        names = [f"{c['category']}.{c['method']}" for c in caps
                 if c["status"] == status]
        if names:
            out.append(f"| {STATUS_LABEL[status]} | {len(names)} | "
                       f"{'、'.join(f'`{n}`' for n in names)} |")
    out.append("")

    out.extend(_render_execution(spec.get("execution")))

    for cat in CAT_TITLE:
        rows = [c for c in caps if c["category"] == cat]
        if not rows:
            continue
        out.append(f"## {CAT_TITLE[cat]}（{len(rows)}）")
        out.append("")
        out.append("| method | 能力 | 参数 | 底层调用 | function_id | 状态 | 缺口 |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for c in rows:
            ids = " / ".join(f"`{i}`" for i in c["function_ids"]) or "—"
            calls = " + ".join(f"`{x}`" for x in c["calls"])
            opts = " ".join(f"`--{o}`" for o in c["options"]) or "无"
            out.append(
                f"| `{c['method']}` | {_cell(c['title'])} | {opts} | {calls} | "
                f"{ids} | {STATUS_LABEL[c['status']]} | {_cell(c['gap'])} |")
        out.append("")

        # 「已验证」不带出处等于自我声明。把真跑过真实网关的单列出来，
        # 谁想复核可以照着原样再跑一遍。
        proven = [c for c in rows if c.get("evidence")]
        if proven:
            out.append("### 本类里跑过真实网关的")
            out.append("")
            for c in proven:
                out.append(f"- `{c['method']}` — {c['evidence']}")
            out.append("")

    out.append("## 输出契约")
    out.append("")
    for key, shape in spec["output_contracts"].items():
        label = "默认" if key == "default" else f"`{key}`"
        out.append(f"- {label} → `{shape}`")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不写")
    args = ap.parse_args()

    spec = json.loads(SRC.read_text(encoding="utf-8"))
    text = render(spec)

    if args.check:
        if not DST.exists():
            print(f"缺 {DST.relative_to(ROOT)}，跑 python -m src.tgwpy.gen_skill_caps 生成")
            return 1
        if DST.read_text(encoding="utf-8") != text:
            print("视图与事实源不一致，跑 python -m src.tgwpy.gen_skill_caps 重新生成")
            return 1
        print("视图与事实源一致")
        return 0

    DST.write_text(text, encoding="utf-8")
    print(f"已生成 {DST.relative_to(ROOT)}（{len(text)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
