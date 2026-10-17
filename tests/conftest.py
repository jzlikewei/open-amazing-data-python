"""共用的测试前置条件。

**业务层是可选的。** 需要真 import `AmazingData` 的测试，读的是
`recovered/AmazingData` 里的 .py，不是发行物里的 .pyc。
那份源码入私有记录仓，不进公开仓（厂商业务代码不发布）。

所以公开仓的 CI（以及任何没有 recovered/ 的人）跑起来是一批通过加少量跳过，
而不是一片红。跳过要说清楚为什么，默默跳过等于把门禁拆了。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

_RECOVERED = ROOT / "recovered"
_RECOVERED_INIT = _RECOVERED / "AmazingData" / "__init__.py"

# 查询链路的 .py 在时，让 `import AmazingData` 找到它，而不是去找 .pyc。
if _RECOVERED_INIT.is_file() and str(_RECOVERED) not in sys.path:
    sys.path.insert(0, str(_RECOVERED))

#: 没生成查询链路源码时的跳过理由。
NO_WHEEL = (
    "需要查询链路源码 recovered/AmazingData，但还没有。"
    "它在私有记录仓里，公开仓不带。"
)


def business_layer_available() -> bool:
    """查询链路的 .py 在不在。**只看文件在不在**，不真 import——
    真 import 会在收集阶段抛异常，那时已经来不及跳过了。"""
    return _RECOVERED_INIT.is_file()


#: 装饰器：给「必须真跑业务层」的测试用。
needs_business_layer = pytest.mark.skipif(
    not business_layer_available(), reason=NO_WHEEL
)


def pytest_collection_modifyitems(session, config, items):
    """等价门禁只跑被选中的场景。

    观测是整组缓存的（每个场景每条链路一个子进程）。不收窄的话 `-k` 只挑一个
    方法也要把 144 个场景全跑一遍。
    """
    import parity_selection

    parity_selection.ids = {
        item.callspec.params["scenario"].id
        for item in items
        if hasattr(item, "callspec") and "scenario" in item.callspec.params
    }
