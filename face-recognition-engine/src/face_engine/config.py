"""【运行时配置加载】读取并校验 configs/*.yaml。

配置是唯一的可调参数来源（模型路径、阈值、批量大小、后端选择等）。
这里只做"结构校验"，不填默认值——缺字段就报错，
避免配置写错时程序带着错误的参数悄悄跑起来。

注意：配置里的匹配阈值只是开发占位值，
正式场馆阈值必须用真实同人/异人样本标定后才能上线。
"""

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    """加载 YAML 配置并做基本校验。

    校验项：
    1. 顶层必须是字典（YAML 映射），而不是列表或标量
    2. 六个必需 section 必须齐全：device / detector / recognizer / face_index / matching / video
    3. 匹配阈值必须满足 -1 <= low < high <= 1（余弦相似度的合法值域）

    参数：
        path: YAML 配置文件路径。
    返回：
        配置字典。
    """
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a YAML mapping")

    required = ("device", "detector", "recognizer", "face_index", "matching", "video")
    if any(key not in config for key in required):
        raise ValueError(f"Configuration requires: {', '.join(required)}")

    device = config["device"]
    valid_cuda_id = isinstance(device, str) and device.startswith("cuda:") and device[5:].isdigit()
    if device not in ("cpu", "cuda") and not valid_cuda_id:
        raise ValueError("device must be 'cpu', 'cuda', or 'cuda:<id>'")

    low = config["matching"]["low_threshold"]
    high = config["matching"]["high_threshold"]
    if not (-1 <= low < high <= 1):
        raise ValueError("Require -1 <= low_threshold < high_threshold <= 1")
    return config
