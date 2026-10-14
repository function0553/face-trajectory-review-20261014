"""【视频帧采样器】按间隔从帧序列里挑帧送识别，输出 JSON Lines 事件。

职责边界（很重要）：
    本模块只接收"已经解码好的帧"，不负责连接摄像头、不拉 RTSP、不管断线重连。
    解码和摄像头管理属于部署层（见 examples/ 和 cli 的 stream 命令）。
    这样算法核心才能被不同的视频接入方式复用。

为什么需要采样间隔：
    摄像头每秒 25 帧，但没必要每帧都识别——一个人不会在 40 毫秒内换张脸。
    按间隔抽帧可以在几乎不损失识别效果的前提下大幅降低算力开销。
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

from face_engine.cache.recognition_cache import RecognitionCache
from face_engine.pipeline.image_pipeline import FaceRecognitionEngine


@dataclass(frozen=True)
class Frame:
    """一帧图像及其可选元信息。

    frozen=True：帧数据不应在传递过程中被修改，避免多处共享同一数组时互相干扰。
    """

    image: np.ndarray  # 已解码的 BGR 帧
    timestamp_seconds: float | None = None  # 时间戳（秒）；摄像头实时流通常拿不到，可为 None
    source_id: str | None = None  # 来源标签，如摄像头编号


def recognize_frames(frames: Iterable[Frame | np.ndarray], pipeline: FaceRecognitionEngine, *,
                     detection_interval: int = 1, recognition_interval: int = 15,
                     cache_enabled: bool = False, cache_ttl_frames: int | None = None,
                     max_frames: int | None = None) -> Iterator[dict]:
    """按采样间隔挑选帧做识别，逐条产出结果事件（生成器）。

    采样规则：只有帧号同时被 detection_interval 和 recognition_interval 整除时才识别。
    例如 detection=1、recognition=15 → 第 0、15、30... 帧会出结果，其余帧直接跳过。

    参数：
        frames: 帧序列，元素可以是 Frame 对象，也可以是裸的 ndarray（后者按无元信息处理）。
        pipeline: 识别引擎。
        detection_interval: 检测采样间隔。
        recognition_interval: 识别采样间隔（取两者共同生效的帧）。
        cache_enabled: 是否启用完全相同的帧缓存。
        cache_ttl_frames: 缓存有效期（帧数）；不给则默认取 recognition_interval × 2。
        max_frames: 最多读多少帧后停止；None 表示读完为止。
    产出：
        每个被采样的帧产出一个字典：
        {frame_index, timestamp_seconds, results, cache_hit}
    """
    if detection_interval <= 0 or recognition_interval <= 0:
        raise ValueError("frame intervals must be positive")
    if max_frames is not None and max_frames <= 0:
        raise ValueError("max_frames must be positive")
    if cache_ttl_frames is not None and cache_ttl_frames <= 0:
        raise ValueError("cache_ttl_frames must be positive")

    cache = RecognitionCache(cache_ttl_frames or recognition_interval * 2) if cache_enabled else None

    for frame_index, item in enumerate(frames):
        if max_frames is not None and frame_index >= max_frames:
            break
        # 未被两个间隔同时整除的帧直接跳过，不产出任何事件。
        if frame_index % detection_interval or frame_index % recognition_interval:
            continue

        # 兼容裸 ndarray：自动包装成 Frame，元信息为默认值。
        frame = item if isinstance(item, Frame) else Frame(item)

        results = cache.get(frame.image, frame_index, frame.source_id) if cache else None
        cache_hit = results is not None
        if results is None:
            results = pipeline.recognize_image(frame.image, frame.source_id)
            if cache:
                cache.put(frame.image, frame_index, results, frame.source_id)

        yield {"frame_index": frame_index, "timestamp_seconds": frame.timestamp_seconds,
               "results": results, "cache_hit": cache_hit}
