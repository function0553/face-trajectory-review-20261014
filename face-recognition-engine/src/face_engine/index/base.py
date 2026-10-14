"""【人脸特征库（向量索引）抽象契约】定义"存特征、搜 TopK"这一环节的统一接口。

人脸库只存两样东西：人员标识 subject_id 和对应的特征向量。
刻意不存姓名、票号、照片等业务字段——那些属于外部业务系统，算法层不理解也不保存。

替换实现时（NumPy 暴力搜索 → FAISS → 未来可能的其他引擎），只要满足本接口，
上层流水线无需改动。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class SearchHit:
    """一条检索命中结果。

    frozen=True 表示不可修改，避免在流水线传递过程中被意外改写。
    """

    subject_id: str  # 库中的人员标识（由外部业务系统提供，算法不解释含义）
    similarity: float  # 余弦相似度，范围 [-1, 1]，越大越像


class FaceIndex(ABC):
    """人脸特征库抽象基类。"""

    @abstractmethod
    def add(self, subject_id: str, embedding: np.ndarray) -> None:
        """往库里加一个人：人员标识 + 一条特征向量。"""

    @abstractmethod
    def add_batch(self, subject_ids: Sequence[str], embeddings: np.ndarray) -> None:
        """批量加人。建 4~5 万人的库时比逐条 add 快得多。"""

    @abstractmethod
    def remove(self, subject_id: str) -> None:
        """按人员标识删除一条记录；不存在时应抛 KeyError。"""

    @abstractmethod
    def search_batch(self, embeddings: np.ndarray, top_k: int = 5) -> list[list[SearchHit]]:
        """批量检索：为每条查询向量找出最相似的 top_k 个人。

        返回二维列表：外层对应每条查询，内层是该查询的候选（按相似度降序）。
        库为空时返回空列表（而不是报错），这样"有脸但没库"能正常降级为 UNKNOWN。
        """

    def search(self, embedding: np.ndarray, top_k: int = 5) -> list[SearchHit]:
        """单条查询的便捷方法：把 1 条向量包装成批量再取第一行。"""
        return self.search_batch(np.asarray(embedding).reshape(1, -1), top_k)[0]

    @abstractmethod
    def clear(self) -> None:
        """清空整个库。"""

    @abstractmethod
    def size(self) -> int:
        """返回库中人数。"""

    @abstractmethod
    def save(self, path: str | Path) -> None:
        """把库持久化到磁盘（.npz 文件）。"""

    @abstractmethod
    def load(self, path: str | Path) -> None:
        """从磁盘加载库。"""
