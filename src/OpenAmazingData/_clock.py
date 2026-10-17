"""交易日历的「今天」。

只给交易日历用：`BaseData.get_calendar` 没传 `date` 时的结束日，以及内部取日历
的地方（行情、复权因子）。这对应门禁里 .pyc 一侧 `tgwpy.patches.patch_calendar`
的 `end_today`，测试经 `fixed_today` 钉住。

厂商其余「缺省取当天」的方法（研报摘要、新闻的缺省 `end_date` 等）没有被补丁
改动，取的是系统时间，自研包也直接用 `time.strftime`，不要改成调这里——
否则门禁里两条链路的请求日期会对不上。
"""

from __future__ import annotations

import time

fixed_today: str | None = None


def today() -> str:
    return fixed_today or time.strftime("%Y%m%d")
