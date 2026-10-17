"""本地数据根的缺省值。

厂商签名里的缺省值是 Windows 路径 `D://AmazingData_local_data//`，在 macOS
上会原样落成当前目录下的 `./D:/...`。这里换成 `TGW_LOCAL_DATA_ROOT`，没设就用
`~/.amazingdata_local/`，与 `tgwpy.patches.patch_local_data_root` 给厂商业务层
打的补丁取同一个值。

缺省值在 import 时定下来（和厂商一样是函数签名里的常量），之后改环境变量
不影响已导入的包。结尾必须带斜杠：缓存目录是直接拼在后面的。
"""

from __future__ import annotations

import os

DEFAULT_ROOT = "~/.amazingdata_local"


def local_root() -> str:
    root = os.environ.get("TGW_LOCAL_DATA_ROOT") or DEFAULT_ROOT
    return os.path.abspath(os.path.expanduser(root)).rstrip("/" + os.sep) + os.sep


DEFAULT_LOCAL_PATH = local_root()
