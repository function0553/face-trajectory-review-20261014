"""【人脸特征提取器抽象契约】定义"人脸图 → 特征向量"这一环节的统一接口。

特征（embedding）是人脸识别的核心：把一张对齐后的人脸图压缩成一个固定长度的向量，
使得"同一个人的向量距离近、不同人的向量距离远"。本引擎用 512 维。

接口刻意设计成"批量"优先（extract_embeddings 收一批、返回一批），因为 GPU 上
一次处理 50 张脸远比逐张调用 50 次快得多。单张接口只是批量的语法糖。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

import numpy as np


# 特征向量维度。ArcFace 标准输出 512 维。
# 换模型（例如 SFace 的 128 维）时必须同步改这里，否则整条链路的校验都会失败，
# 这也是防止"不同模型特征混库"的一道硬约束。
EMBEDDING_DIM = 512


class FaceRecognizer(ABC):
    """人脸特征提取器抽象基类：所有具体提取器都必须实现 extract_embeddings。"""

    @abstractmethod
    def extract_embeddings(self, faces: Sequence[np.ndarray]) -> np.ndarray:
        """把一批对齐后的人脸图转成特征向量。

        参数：
            faces: 已对齐的人脸图序列，每张 [112, 112, 3] 的 uint8 BGR 图。
        返回：
            形状 [N, 512] 的 float32 数组，且每一行都已做 L2 归一化（模长为 1）。
            归一化很关键：归一化后"向量点积"就直接等于"余弦相似度"，
            检索时无需再额外计算模长。
            输入为空时返回 [0, 512]，而不是报错。
        """

    def extract_embedding(self, face: np.ndarray) -> np.ndarray:
        """单张人脸的便捷方法：内部走批量接口，取第一行结果。"""
        return self.extract_embeddings([face])[0]
