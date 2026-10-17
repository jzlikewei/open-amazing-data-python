"""本地缓存：一张表一个 `.h5` 文件，HDF5 key 就是文件名去掉扩展名。

落盘格式与厂商 `utils/save_get_data.pyc` 相同，两边的缓存可以互读：
`DataFrame.to_hdf(<目录><名字>.h5, key=<名字>, mode='w', complevel=0)`，
读用 `pd.read_hdf(<目录><名字>.h5)`，文件不在抛 `FileNotFoundError`。

目录由调用方拼：本地数据根直接接相对目录再接 `/`，不插分隔符——
厂商就是这么拼的，数据根必须自带结尾斜杠。
"""

from __future__ import annotations

import os

import pandas as pd


def table_dir(local_path: str, *parts: str) -> str:
    """`<数据根><part1>/<part2>/`。"""
    return local_path + "/".join(parts) + "/"


def read_table(directory: str, name: str) -> pd.DataFrame:
    return pd.read_hdf(directory + name + ".h5")


def try_read_table(directory: str, name: str) -> pd.DataFrame | None:
    try:
        return read_table(directory, name)
    except FileNotFoundError:
        return None


def save_table(directory: str, name: str, frame: pd.DataFrame) -> None:
    os.makedirs(directory, exist_ok=True)
    if frame.columns.duplicated().any():
        dup = frame.columns[frame.columns.duplicated()].tolist()
        frame = frame.loc[:, ~frame.columns.duplicated()]
        print(f"[WARN] {name} 存在重复列，已保留首次出现: {dup}")
    frame.to_hdf(directory + name + ".h5", key=name, mode="w", append=False, complevel=0)
