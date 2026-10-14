"""【NumPy 暴力检索实现 + 特征库持久化】精确余弦搜索的兜底实现。

这是人脸特征库的基础实现，用 NumPy 矩阵乘法做"精确"搜索：
把所有库存向量和查询向量做点积，取最大的前 K 个。
因为是精确搜索（不是近似搜索），所以 100% 不会漏掉真正最相似的人。

定位：
    NumPy 实现主要供开发、测试和没有 FAISS 的环境使用；
    几万人的库建议用 FAISS（见 faiss_index.py），后者继承自本类，
    复用了本类的 ID 管理、校验和持久化逻辑，只重写搜索部分。

重要：本文件也是特征库 .npz 文件格式的唯一定义处。
换模型、换对齐或换预处理后，旧库必须作废重建，不能混用。
"""

import os
from pathlib import Path
from uuid import uuid4

import numpy as np

from face_engine.index.base import FaceIndex, SearchHit
from face_engine.recognizer.base import EMBEDDING_DIM


def _normalized_matrix(values: np.ndarray, *, allow_empty: bool = True) -> np.ndarray:
    """校验并把一批向量 L2 归一化，作为入库/查询前的统一入口。

    这里集中做四件事，避免每个调用点各写一遍导致校验口径不一致：
    1. 形状必须是 [N, 512]
    2. 必须全是有限数（不能有 NaN/Inf）
    3. 不能有零向量（零向量无法归一化，相似度也无意义）
    4. 归一化成模长为 1，并返回 C 连续数组（矩阵乘法更快）

    参数：
        allow_empty: False 时不允许空数组（入库场景要求至少一条）。
    """
    matrix = np.asarray(values, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != EMBEDDING_DIM:
        raise ValueError(f"embeddings must have shape [N, {EMBEDDING_DIM}]")
    if not allow_empty and len(matrix) == 0:
        raise ValueError("at least one embedding is required")
    if not np.isfinite(matrix).all():
        raise ValueError("embeddings must be finite")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if (norms <= 0).any():
        raise ValueError("zero embeddings are invalid")
    return np.ascontiguousarray(matrix / norms)


class NumPyIndex(FaceIndex):
    """基于 NumPy 的精确余弦检索特征库。

    内部状态只有两样：
        _ids: 人员标识列表，下标即"库内行号"
        _vectors: [N, 512] 的归一化特征矩阵，第 i 行对应 _ids[i]
    两者必须始终等长。
    """

    backend = "NumPy CPU"

    def __init__(self):
        self._ids: list[str] = []
        self._vectors = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        print(f"Face Index Backend: {self.backend}")

    def _rebuild(self) -> None:
        """子类钩子：数据发生变化后由子类重建自己的加速结构。

        本类无需重建任何东西；FAISSIndex 会重写它来重建 FAISS 索引。
        """

    def add(self, subject_id: str, embedding: np.ndarray) -> None:
        """加一个人：把单条向量包装成批量再调 add_batch。"""
        self.add_batch([subject_id], np.asarray(embedding).reshape(1, -1))

    def add_batch(self, subject_ids, embeddings: np.ndarray) -> None:
        """批量加人。建几万人的库时用这个方法，比逐条 add 快很多。

        校验规则（任一不满足就整体失败，绝不部分写入）：
        - ID 数量与向量数量一致
        - 每个 ID 都是非空字符串
        - 本批内部不能有重复 ID
        - 本批 ID 不能与库中已有 ID 冲突（一个人只保留一条特征）
        """
        ids = list(subject_ids)
        vectors = _normalized_matrix(embeddings)
        if len(ids) != len(vectors):
            raise ValueError("subject_ids and embeddings length must match")
        if any(not isinstance(sid, str) or not sid for sid in ids):
            raise ValueError("subject_id must be a nonempty string")
        if len(set(ids)) != len(ids) or set(ids).intersection(self._ids):
            raise ValueError("duplicate subject_id")
        if not ids:
            return
        self._ids.extend(ids)
        self._vectors = np.concatenate((self._vectors, vectors), axis=0)
        self._rebuild()  # 通知子类同步重建加速结构

    def remove(self, subject_id: str) -> None:
        """按人员标识删除一条记录；ID 不存在时抛 KeyError。"""
        try:
            position = self._ids.index(subject_id)
        except ValueError as exc:
            raise KeyError(subject_id) from exc
        del self._ids[position]
        self._vectors = np.delete(self._vectors, position, axis=0)
        self._rebuild()

    def search_batch(self, embeddings: np.ndarray, top_k: int = 5) -> list[list[SearchHit]]:
        """批量检索：为每条查询向量返回最相似的 top_k 个人。

        实现要点（性能考虑）：
        - 一次矩阵乘法 [Q, 512] @ [512, N] 算出全部 Q×N 相似度，
          因为向量都已归一化，点积结果直接就是余弦相似度。
        - 用 argpartition 而不是 argsort：只关心前 K 名，
          做一次 O(N) 的部分选择即可，不必对几万个数全排序。
        - 只在选出的 K 个候选里做完整排序，保证返回顺序按相似度降序。
        """
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        queries = _normalized_matrix(embeddings)
        if not len(queries):
            return []
        # 库为空时返回空结果而非报错——"有脸但没库"应降级为 UNKNOWN。
        if not self.size():
            return [[] for _ in range(len(queries))]

        scores = queries @ self._vectors.T  # [Q, N] 余弦相似度矩阵
        k = min(top_k, self.size())  # 库里人不够时，最多只能返回 size 个

        # 先部分选择出 top-k 的下标（未排序），再对这 k 个排序。
        candidates = np.argpartition(-scores, k - 1, axis=1)[:, :k]
        order = np.argsort(-np.take_along_axis(scores, candidates, axis=1), axis=1)
        indices = np.take_along_axis(candidates, order, axis=1)
        return [[SearchHit(self._ids[int(j)], float(scores[i, j])) for j in row]
                for i, row in enumerate(indices)]

    def clear(self) -> None:
        """清空整个库。"""
        self._ids = []
        self._vectors = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        self._rebuild()

    def size(self) -> int:
        """返回库中人数。"""
        return len(self._ids)

    def save(self, path: str | Path) -> None:
        """把库原子性地写入 .npz 文件。

        为什么写临时文件再 os.replace：
            几万人的库文件很大，直接写可能写到一半崩溃/断电，留下一个损坏的库。
            先写临时文件 + fsync 落盘，再用 os.replace 原子替换，
            保证目标文件要么是完整的旧版本，要么是完整的新版本，
            绝不会是"写了一半"的中间状态。

        保存内容（schema 版本 1）：
            schema: 版本号，加载时校验，版本不符直接拒绝
            subject_ids: 人员标识数组
            embeddings: 特征矩阵
        注意：schema 1 不记录模型/预处理版本，
        因此换模型后必须靠流程约束重建库，不能靠文件本身判断。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                np.savez(stream, schema=np.array(1), subject_ids=np.asarray(self._ids, dtype=np.str_),
                         embeddings=self._vectors)
                stream.flush()
                os.fsync(stream.fileno())  # 强制刷到磁盘，防止只在页缓存里
            os.replace(temporary, target)  # 原子替换
        finally:
            temporary.unlink(missing_ok=True)  # 无论成功失败都清理临时文件

    def load(self, path: str | Path) -> None:
        """从 .npz 文件加载库。

        加载时会重新校验并归一化，避免读入被手工篡改或不完整的脏数据。
        allow_pickle=False 是安全考虑：不允许加载时执行任意 Python 对象。
        """
        with np.load(path, allow_pickle=False) as archive:
            if int(archive["schema"]) != 1:
                raise ValueError("unsupported index schema")
            ids = archive["subject_ids"].tolist()
            vectors = _normalized_matrix(archive["embeddings"])
        if (len(ids) != len(vectors) or len(set(ids)) != len(ids)
                or any(not isinstance(sid, str) or not sid for sid in ids)):
            raise ValueError("invalid saved index")
        self._ids = ids
        self._vectors = vectors
        self._rebuild()
