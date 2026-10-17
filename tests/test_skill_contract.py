"""能力契约测试：用 skill 文档当尺子量逆向实现。

`docs/skill-capabilities.json` 是 SSOT，对应
`docs/skill-capabilities.md` 的表格——那是用户实际会用的接口面。这组测试把三样东西对在一起：

    SKILL.md 承诺的能力  ×  cli.py 的注册表  ×  compat 里的真实实现程度

关键是**状态必须可证伪**：文档里写 `verified` 的，底层调用就不能是 stub；
写 `impl_stub` 的，就必须真的调 `_unimplemented`。这样「文档说通了」
和「代码真的通了」不会各说各话。
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tgwpy.cli import CATEGORIES  # noqa: E402
from tgwpy.compat import tgw_module as compat  # noqa: E402

SPEC_PATH = ROOT / "docs" / "skill-capabilities.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
CAPS = SPEC["capabilities"]

#: 文档里允许出现的状态。新增状态要同时改这里的白名单和 _status_is_true。
STATUSES = {"verified", "unverified", "param_bizerr", "impl_stub", "untested"}

#: skill 文档声明的 6 个分类与各自方法数
SKILL_SHAPE = {
    "base": 8, "market": 2, "finance": 5,
    "shareholder": 7, "index": 6, "other": 11,
}


def _ids() -> list[tuple[str, str, dict]]:
    return [(c["category"], c["method"], c) for c in CAPS]


def _compat_is_stub(name: str) -> bool:
    fn = getattr(compat, name, None)
    if fn is None:
        return True
    return "_unimplemented" in inspect.getsource(fn)


# --------------------------------------------------------------------------
# 一、skill 承诺的和文档记录的必须是同一批
# --------------------------------------------------------------------------

def test_共39个能力():
    assert len(CAPS) == 39


def test_分类与skill一致():
    counts = Counter(c["category"] for c in CAPS)
    assert dict(counts) == SKILL_SHAPE


def test_没有重复的能力():
    seen = [(c["category"], c["method"]) for c in CAPS]
    assert len(set(seen)) == len(seen)


def test_注册表和文档一一对应():
    """skill 里有的必须都注册了，注册表里多出来的也要被文档记录。"""
    assert set(CATEGORIES) == {c["category"] for c in CAPS}
    for cat, method, _ in _ids():
        assert method in CATEGORIES[cat], f"{cat}.{method} 在 CATEGORIES 里没有"


# --------------------------------------------------------------------------
# 二、参数面：skill 文档写了什么，我们就得收什么
# --------------------------------------------------------------------------

@pytest.mark.parametrize("category,method,cap", _ids())
def test_参数与skill一致(category, method, cap):
    """CLI 收的 option 必须覆盖 skill 文档写明的那几个。

    只查「skill 要的我们都有」，不查「我们没多收」——多收不违背承诺，
    少收才是。
    """
    spec = CATEGORIES[category][method]
    missing = set(cap["options"]) - set(spec.options)
    assert not missing, f"{category}.{method} 缺参数 {sorted(missing)}"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_必填参数齐全(category, method, cap):
    """skill 说「所有方法都需要 --codes」的那些，required 里必须有 codes。"""
    spec = CATEGORIES[category][method]
    if "codes" in cap["options"] and cap["options"][0] == "codes":
        assert "codes" in spec.required, f"{category}.{method} 的 --codes 应为必填"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_函数id与skill一致(category, method, cap):
    spec = CATEGORIES[category][method]
    assert list(spec.function_ids) == cap["function_ids"], \
        f"{category}.{method} function_id 不一致"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_入口调用在依赖集内(category, method, cap):
    """注册表记的是**入口**调用，JSON 记的是完整依赖链。

    code_list 走 QueryThirdInfo 入口，但业务层内部还要调 QuerySecuritiesInfo
    补英文名——单值字段记不下这种链，所以 JSON 用列表。
    """
    spec = CATEGORIES[category][method]
    assert spec.call == cap["entry_call"], f"{category}.{method} 入口调用不一致"
    assert spec.call in cap["calls"], f"{category}.{method} 入口不在依赖集内"


# --------------------------------------------------------------------------
# 三、状态可证伪：文档声称的和代码实际的对得上
# --------------------------------------------------------------------------

def test_状态取值合法():
    bad = {(c["category"], c["method"]): c["status"]
           for c in CAPS if c["status"] not in STATUSES}
    assert not bad, f"非法状态：{bad}"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_impl_stub的底层调用必须真的是stub(category, method, cap):
    """写 `impl_stub` 就意味着底层还没写。写错了这条就红。"""
    if cap["status"] != "impl_stub":
        return
    assert any(_compat_is_stub(c) for c in cap["calls"]), \
        f"{category}.{method} 标成 impl_stub，但 {cap['calls']} 都已实现"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_unverified的底层调用必须已实现(category, method, cap):
    """`unverified` = 代码写完了但没跑过真实网关。

    所以底下不能还是 stub——那是 `impl_stub` 的定义。
    """
    if cap["status"] != "unverified":
        return
    assert not any(_compat_is_stub(c) for c in cap["calls"]), \
        f"{category}.{method} 标 unverified，但 {cap['calls']} 还有 stub"
    # unverified 的两条理由都必须在 gap 里说清：协议验过了，卡在哪
    assert cap["gap"], f"{category}.{method} 标 unverified 但没写 gap"
    assert "真实网关" in cap["gap"], \
        f"{category}.{method} 的 gap 要写明真实网关验证到哪一步"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_verified的底层调用不能是stub(category, method, cap):
    """反过来：标 `verified` 就不能还有 stub 在下面。"""
    if cap["status"] != "verified":
        return
    assert not any(_compat_is_stub(c) for c in cap["calls"]), \
        f"{category}.{method} 标成 verified，但 {cap['calls']} 还有 stub"


@pytest.mark.parametrize("category,method,cap", _ids())
def test_非impl_stub的必须登记了阻塞原因(category, method, cap):
    """有 gap 的要么写清楚为什么，要么就别声称通过。"""
    if cap["status"] == "verified":
        assert cap["gap"] is None, f"{category}.{method} 标 verified 却还挂着 gap"
    else:
        assert cap["gap"], f"{category}.{method} 状态 {cap['status']} 但没写 gap"


# --------------------------------------------------------------------------
# 四、汇总数字写进文档，防止悄悄漂移
# --------------------------------------------------------------------------

def test_汇总数字与实际一致():
    counts = Counter(c["status"] for c in CAPS)
    total = sum(counts.values())
    assert total == 39, f"状态数加起来 {total}，不等于 39"
    assert set(counts) <= STATUSES, f"有非法状态：{set(counts) - STATUSES}"
    # 下面这些数写进 docs/skill-capabilities.md 的汇总表
    assert counts["impl_stub"] == 0, "L0 全部落地，impl_stub 应归零"
    assert counts["param_bizerr"] == 0, "L1 四组参数全部在网关上确认，应归零"
    assert counts["untested"] == 0, "calendar 已补测，不该再有 untested"
    # 剩下的 5 个全是服务端侧限制：代码表 4 个 + etf_pcf，恒回 -82
    assert counts["unverified"] == 5, "只剩服务端限制那 5 个"
    assert counts["verified"] == 34


def test_市场类已通过真实网关门禁():
    """kline / snapshot 走完了协议、解码、传输三层，必须是 verified。

    这条是防回退的：这三个层次各踩过一次坑（周期码 10008 vs 10100、
    begin_time 填 0、双连接被 -98 拒），任何一层退回去都要在这里露出来。
    """
    for m in ("kline", "snapshot"):
        cap = next(c for c in CAPS
                   if c["category"] == "market" and c["method"] == m)
        assert cap["status"] == "verified", f"{m} 应为 verified"
        assert cap["gap"] is None

    # 三个坑都要留痕。**扫源码注释，不读分析文档**——协议真值已经写进
    # `src/tgwpy/frames/` 与 `compat/tgw_module.py` 的注释里，而分析文档
    # 是不随公开仓分发的本地产物。拿它当门禁判据等于把门禁建在一份
    # clone 下来就不存在的文件上。
    import glob as _glob
    src = "\n".join(
        pathlib.Path(f).read_text(encoding="utf-8")
        for f in _glob.glob(str(ROOT / "src" / "**" / "*.py"), recursive=True)
    )
    for token in ("10100", "-98", "5432"):
        assert token in src, f"源码注释里应留有 {token} 的证据"


def test_勾选完成的项必须已过真实网关门禁():
    """L0.1/L0.2 的门禁写了「用真实网关跑通」。

    没跑过就不许勾——这是防止把「代码写完」当成「能力通了」。
    """
    market = [c for c in CAPS if c["category"] == "market"]
    items = [i for l in LAYERS for i in l["items"]
             if any(u.startswith("market.") for u in i["unblocks"])]
    if any(c["status"] != "verified" for c in market):
        for it in items:
            assert not it["done"], \
                f"{it['id']} 勾了完成，但 market 能力还没到 verified"


# --------------------------------------------------------------------------
# 五、视图不能和事实源漂移
# --------------------------------------------------------------------------

def test_生成的md与事实源一致():
    """docs/skill-capabilities.md 是 JSON 的视图，手改就会红。

    `render` 放在包内的 `tgwpy.gen_skill_caps` 而不是反编译工具链目录：
    公开仓不带那个目录，而这道断言是两边共用的「视图防漂移」门禁，
    放进包里它才能在公开仓也跑起来——也正因为它跑得起来，
    公开仓的 CI 不需要再单跑一次 `--check`。
    """
    from tgwpy.gen_skill_caps import render

    want = render(SPEC)
    got = (ROOT / "docs" / "skill-capabilities.md").read_text(encoding="utf-8")
    assert got == want, "视图与事实源漂移，跑 python -m src.tgwpy.gen_skill_caps"


def test_旧对照表还在():
    """docs/skill-compat.md 讲的是映射细节，这份讲能力面，两份都要在。"""
    assert (ROOT / "docs" / "skill-compat.md").exists()


# --------------------------------------------------------------------------
# 六、执行清单：cron 照着它干活，所以它必须和事实源自洽
# --------------------------------------------------------------------------

EXEC = SPEC["execution"]
LAYERS = EXEC["layers"]
ITEMS = [(layer["id"], item) for layer in LAYERS for item in layer["items"]]
ITEM_IDS = {i["id"] for _, i in ITEMS}


def test_清单覆盖全部缺口():
    """事实源里所有没 verified 的能力，都得有清单项负责。

    这是最重要的一条：新增一个 verified=false 的能力却忘了进清单，
    cron 就永远不会去做它。
    """
    blocked = {f"{c['category']}.{c['method']}" for c in CAPS if c["status"] != "verified"}
    # hardening 是修隐患不补缺口，不算覆盖。
    # 已完成的项也还算覆盖——L0.3/L0.4 是「做了但服务端不通」，
    # 它们解过的能力仍是非 verified，但确实已经尽力做过了。
    covered = {u for _, item in ITEMS if item["type"] == "unblock"
               for u in item["unblocks"]}
    assert not (blocked - covered), f"有缺口没人管：{sorted(blocked - covered)}"
    # covered 里可以有已 verified 的（项成功收口了，如 L2.1 的 calendar），
    # 但不该有既没 verified 又不在缺口集里的僵尸条目
    by_cap = {f"{c['category']}.{c['method']}": c for c in CAPS}
    zombie = {u for u in covered - blocked
              if by_cap[u]["status"] not in ("verified", "unverified")}
    assert not zombie, f"清单项解开了没人认领的能力：{sorted(zombie)}"


def test_清单项都有门禁():
    """没有门禁的项勾了也没法验收。"""
    for lid, item in ITEMS:
        assert item.get("gate"), f"{item['id']} 没写门禁"
        assert item.get("type") in ("unblock", "hardening"), f"{item['id']} 类型非法"
        if item["type"] == "unblock":
            assert item.get("unblocks"), f"{item['id']} 是 unblock 却没写解锁哪些能力"
        else:
            assert not item.get("unblocks"), f"{item['id']} 是 hardening，不该挂 unblocks"


def test_依赖只能指向更早的层():
    """下层没关上层不许做，依赖必须指向前面的层。"""
    order = {layer["id"]: n for n, layer in enumerate(LAYERS)}
    for lid, item in ITEMS:
        for dep in item.get("depends_on", []):
            assert dep in ITEM_IDS, f"{item['id']} 依赖不存在的 {dep}"
            assert order[dep[:2]] < order[lid], \
                f"{item['id']} 依赖了同层或更后面的 {dep}"


def test_完成的项不能有未完成的下层():
    """层门禁的镜像校验：勾了 L1 却留着 L0 未完成，说明状态脏了。"""
    done = {item["id"] for _, item in ITEMS if item["done"]}
    for lid, item in ITEMS:
        if item["id"] in done:
            for dep in item.get("depends_on", []):
                assert dep in done, \
                    f"{item['id']} 已完成但依赖 {dep} 未完成"


def test_完成的项其解锁能力必须已验证():
    """勾完一项，对应能力的 status 就该是 verified，否则文档自相矛盾。"""
    by_cap = {f"{c['category']}.{c['method']}": c for c in CAPS}
    for lid, item in ITEMS:
        if not item["done"] or item.get("server_limited"):
            continue          # server_limited 的项见下面那条断言
        for u in item["unblocks"]:
            cap = by_cap[u]
            assert cap["status"] == "verified", \
                f"{item['id']} 标完成，但 {u} 的 status 还是 {cap['status']}"


@pytest.mark.parametrize("lid,item", ITEMS)
def test_server_limited的项必须把能力留在unverified(lid, item):
    """因服务端限制收口的项，解锁的能力必须是 unverified 且写明原因。

    标成 verified 就是在伪造通过——真实网关一个数据都没返回。
    """
    if not item.get("server_limited"):
        return
    assert item["done"], f"{item['id']} 标了 server_limited 就该是 done"
    by_cap = {f"{c['category']}.{c['method']}": c for c in CAPS}
    for u in item["unblocks"]:
        cap = by_cap[u]
        assert cap["status"] == "unverified", \
            f"{item['id']} 受服务端限制，{u} 应为 unverified，实为 {cap['status']}"
        assert cap["gap"] and "服务端" in cap["gap"], \
            f"{u} 的 gap 要写明是服务端侧限制"
    assert item.get("server_limited_why"), f"{item['id']} 缺 server_limited_why"


# --------------------------------------------------------------------------
# 七、README 里的关键数字不许和事实源脱节
# --------------------------------------------------------------------------
#
# 上一份交接文档 HANDOVER.md 就是因为长期没人对账，攒出一堆过时数字和一条
# **主动误导**的结论（说日线该填 period_type=10008，实际是 10100 档），
# 最后整份删除、内容并入 README。README 是现在的入口文档，加这层断言防同样的腐烂。

README = ROOT / "README.md"
# 私有仓才有 AGENTS.md。公开仓的 README 是使用者文档，不带这些对账句。
_PRIVATE = (ROOT / "AGENTS.md").is_file()
# 公开模板在私有仓的路径拆开写。公开仓守卫拒绝测试源码里的目录式工具链引用。
_PUBLIC_TEMPLATE = ROOT / "tools" / "public" / "README.public.md"
PUBLIC_README = _PUBLIC_TEMPLATE if _PUBLIC_TEMPLATE.is_file() else README


def _readme_counts() -> tuple[int, int, int]:
    import re
    text = README.read_text(encoding="utf-8")
    n_test = re.search(r"tests/\s+(\d+) 个测试", text)
    n_ver = re.search(r"(\d+)\s*/\s*39 已验证", text)
    assert n_test, "README 里找不到测试数"
    assert n_ver, "README 里找不到「N / 39 已验证」"
    return int(n_test.group(1)), int(n_ver.group(1)), 39


def test_readme的测试数与实际一致():
    """私有仓入口文档写多少个测试，跑一遍就得多少个。

    公开 README 不写测试条数，这条只约束私有仓。

    **要数的是测试条目，不是输出里所有含 `::` 的地方。** 直接数全文
    会被 pytest 自己的报错行、warning 回溯带偏——那些也是 `::`，
    但不是测试。子进程万一没跑起来（returncode 非 0）更会数出 0，
    然后报一句看不懂的「实际收集到 0 个」。这两种都先挡住再说。
    """
    if not _PRIVATE:
        pytest.skip("公开入口文档不写测试条数")
    n_test, _, _ = _readme_counts()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, check=False)
    assert proc.returncode == 0, \
        f"收集测试的子进程没跑成功（exit {proc.returncode}）：{proc.stdout[-400:]}"

    entries = [ln for ln in proc.stdout.splitlines()
               if "::" in ln and not ln.startswith(("ERROR", "FAILED"))]
    n_actual = len(entries)
    assert n_actual, "收集结果里一条测试都没有，判定方式不对"
    assert n_test == n_actual, \
        f"README 写 {n_test} 个测试，实际收集到 {n_actual} 个"


def test_readme的已验证数与能力清单一致():
    if not _PRIVATE:
        pytest.skip("公开入口文档不写已验证条数")
    _, n_readme, total = _readme_counts()   # (测试数, 已验证数, 总数)
    n_verified = sum(1 for c in CAPS if c["status"] == "verified")
    assert n_readme == n_verified, \
        f"README 写 {n_readme} 个已验证，能力清单里是 {n_verified} 个"
    assert total == len(CAPS) == 39


def test_readme引用的文档都存在():
    import re
    for m in re.finditer(r"\(([\w./-]+\.md)\)", README.read_text(encoding="utf-8")):
        assert (ROOT / m.group(1)).exists(), f"README 引用了不存在的 {m.group(1)}"


def test_readme不写已被推翻的结论():
    """日线周期码是 10100 档，不是 10008。

    10008 是二进制帧 cyc_type 的取值，JSON 线上协议不认。
    这条写错过一次，代价是行情族白查了一整天。
    约束留在私有仓入口文档。公开 README 是使用者文档，不带协议细节。
    """
    if not _PRIVATE:
        pytest.skip("周期码约束留在私有仓入口文档")
    text = README.read_text(encoding="utf-8")
    assert "10100" in text, "README 应写明日线周期码是 10100"
    # 提到 10008 的地方，前后 200 字内必须说清它不是日线该用的值
    for m in re.finditer("10008", text):
        win = text[max(0, m.start() - 200): m.end() + 200]
        assert ("二进制帧" in win or "cyc_type" in win), \
            f"README 提到 10008 但没说清它只是二进制帧的取值：{win[:120]}"


def test_公开入口是使用者文档():
    """公开 README 只留怎么用，不把私有仓的协议笔记再抄一遍。

公开仓的目的是在能力上替代厂商包：README 以自研包为入口，不要求
使用者准备厂商 wheel 或业务层源码。

    周期码、测试条数、「N / 39 已验证」仍由上面几条约束私有 README。
    私有仓读的是模板；公开仓没有这份模板，读的就是生成后的 README。
    """
    text = PUBLIC_README.read_text(encoding="utf-8")
    for needle in (
        "TGW_HOST", "TGW_PORT", "TGW_USER", "TGW_PASSWORD",
        "status=-82",
        "base.code_list", "base.code_info", "base.future_codes",
        "base.option_codes", "other.etf_pcf",
        "src.tgwpy.batch_run", "guard.py",
        "TGW_LOCK_ROOT", "TGW_MIN_INTERVAL", "TGW_BACKOFF_BASE",
        "TGW_BACKOFF_MAX", "TGW_MAX_FAILURES",
        "MIT", "TGW_CA_CERT", "dgw-ca.crt",
        "import OpenAmazingData as ad", "docs/vendor-differences.md",
        "TGW_LOCAL_DATA_ROOT", "TGW_CALENDAR_FIRST_DAY",
    ):
        assert needle in text, f"公开 README 缺少 {needle}"
    for code in ("0", "2", "3", "4", "5"):
        assert code in text
    assert "arm" in text and "macOS" in text
    for banned in (
        "协议速记", "别再犯的错", "最值钱的一句话",
        "bad magic", "180 个值", "8738",
        # 公开仓是厂商包的替代品，不该要求使用者去弄厂商发行物或业务层代码。
        "recovered/", "build/", "AMAZINGDATA_PKG",
    ):
        assert banned not in text, f"公开 README 不该再写 {banned}"


def test_没有文档再引用已删除的HANDOVER():
    """HANDOVER.md 已删除，README 接管了它的职责。

    留着指向它的链接会让人去翻一份不存在、且内容已过时的文档。
    """
    import glob
    stale = []
    for f in glob.glob(str(ROOT / "*.md")) + glob.glob(str(ROOT / "analysis" / "*.md")) \
            + glob.glob(str(ROOT / "docs" / "*.md")):
        if "HANDOVER.md" in pathlib.Path(f).read_text(encoding="utf-8"):
            stale.append(pathlib.Path(f).relative_to(ROOT).as_posix())
    assert not stale, f"这些文档还在引用已删除的 HANDOVER.md：{stale}"
