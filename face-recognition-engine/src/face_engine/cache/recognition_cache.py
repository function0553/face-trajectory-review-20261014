"""【完全相同的帧缓存】只复用字节级相同且未过期的帧结果，绝不跨帧推断身份。

设计红线（务必理解）：
    本缓存是"同一张画面别算两遍"的短路优化，不是跟踪器。
    它只在"这一帧和上一帧的像素完全相同"时才复用结果。
    它绝不根据上一帧的身份去猜这一帧是谁——那是跨帧身份推断（tracking/ReID），
    一旦做了，画面里的人换了而框没变时就会认错人，在安保场景是严重事故。

适用场景：静态画面、编码器卡住重复输出同一帧、抓拍图重复提交等。
默认关闭（见 configs 里的 cache_enabled）。
"""

import hashlib
from copy import deepcopy

import numpy as np


class RecognitionCache:
    """单条目缓存：只记住最近一次成功识别的帧指纹和结果。"""

    def __init__(self, max_age_frames: int):
        """参数：
            max_age_frames: 结果的有效期（按帧数计）。
                            即使画面完全相同，隔太久也不再复用，避免用到陈旧的库状态。
        """
        if max_age_frames <= 0:
            raise ValueError("max_age_frames must be positive")
        self.max_age_frames = max_age_frames
        self._key: bytes | None = None  # 上一帧的指纹
        self._source_id: str | None = None  # 上一帧的来源标签
        self._frame_index = -1  # 上一帧的帧号
        self._results: list[dict] = []

    @staticmethod
    def _fingerprint(frame: np.ndarray) -> bytes:
        """计算一帧的指纹：形状/dtype + 全部像素字节的 BLAKE2b 摘要。

        先写入形状和 dtype 再写像素，是为了避免"形状不同但字节数碰巧相同"的极端碰撞。
        digest_size=16（128 位）对缓存去重足够，碰撞概率可忽略。
        """
        digest = hashlib.blake2b(digest_size=16)
        digest.update(str((frame.shape, frame.dtype)).encode())
        digest.update(frame.tobytes())
        return digest.digest()

    def get(self, frame: np.ndarray, frame_index: int, source_id: str | None = None) -> list[dict] | None:
        """尝试命中缓存。

        必须同时满足三个条件才算命中：
        1. 帧指纹相同（画面字节完全一致）
        2. 来源标签相同（不同摄像头的同画面不能互相复用）
        3. 距离上次缓存未超过 max_age_frames 帧

        返回深拷贝，防止调用方修改结果时污染缓存。未命中返回 None。
        """
        key = self._fingerprint(frame)
        if key == self._key and source_id == self._source_id and frame_index - self._frame_index < self.max_age_frames:
            return deepcopy(self._results)
        return None

    def put(self, frame: np.ndarray, frame_index: int, results: list[dict], source_id: str | None = None) -> None:
        """把一次识别结果存入缓存（覆盖旧的唯一条目）。"""
        self._key = self._fingerprint(frame)
        self._source_id = source_id
        self._frame_index = frame_index
        self._results = deepcopy(results)  # 存副本，与调用方的对象解耦
