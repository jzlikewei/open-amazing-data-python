"""纯 Python 的 tgw 替身层，让 AmazingData 业务层在 arm macOS 上原生跑起来。

分层：
    cli.py        参数解析、分发、JSON 序列化（本层）
    compat/       装进 sys.modules 的 tgw 替身，撑起 AmazingData 的 import 链
    frames/       帧编解码，对接 wss:// 网关
    probe.py      协议探测
    frame.py      候选帧构造

`cli.py` 用 `python src/tgwpy/cli.py <category> <method> [options]` 直接跑，
所以这里不做任何 import 期副作用。
"""

from __future__ import annotations
