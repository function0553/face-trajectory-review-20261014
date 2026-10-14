"""【FAISS 检索实现】用 IndexFlatIP 做精确内积检索，优先使用 GPU。

为什么用 FAISS：
    NumPy 实现要对几万条向量做全量矩阵乘法，规模再大就慢了。
    FAISS 是专门做向量检索的库，同样的精确搜索更快，而且可以把索引放到 GPU 上。

为什么用 IndexFlatIP（Flat = 暴力、IP = 内积）：
    因为入库和查询的向量都已经 L2 归一化，"内积"就等于"余弦相似度"，
    所以直接用内积索引即可，无需额外的归一化步骤。
    Flat 表示不压缩、不做近似，保证结果是精确的 TopK，不会漏人。

继承关系：
    FAISSIndex 继承 NumPyIndex，复用它的 ID 管理、输入校验和 .npz 持久化，
    只重写搜索部分。这样两套实现的对外行为完全一致。
"""

import numpy as np

from face_engine.index.base import SearchHit
from face_engine.index.numpy_index import NumPyIndex, _normalized_matrix
from face_engine.recognizer.base import EMBEDDING_DIM


class FAISSIndex(NumPyIndex):
    """FAISS 精确内积检索索引，GPU 不可用时自动降级到 FAISS CPU。"""

    def __init__(self, use_gpu: bool = True):
        try:
            import faiss
        except ImportError as exc:
            # 没装 FAISS 时抛出明确错误，由 create_index 决定如何兜底。
            raise RuntimeError("FAISS is unavailable; use create_index for NumPy fallback") from exc
        self._faiss = faiss
        self._use_gpu = False
        self._gpu_resources = None

        # 尝试初始化 GPU 资源。两个 hasattr 判断是为了区分
        # "装的是 faiss-cpu（没有 GPU 接口）" 和 "装了 GPU 版但初始化失败" 两种情况。
        if use_gpu and hasattr(faiss, "StandardGpuResources") and hasattr(faiss, "index_cpu_to_gpu"):
            try:
                self._gpu_resources = faiss.StandardGpuResources()
                self._use_gpu = True
            except Exception as exc:
                print(f"FAISS GPU unavailable ({exc}); falling back to FAISS CPU")
        elif use_gpu:
            print("FAISS GPU unavailable; falling back to FAISS CPU")

        self.backend = "FAISS GPU" if self._use_gpu else "FAISS CPU"
        super().__init__()  # 初始化父类的人脸库状态（空 ID 列表 + 空矩阵）
        self._rebuild()

    def _rebuild(self) -> None:
        """重建 FAISS 索引：数据每次增删改后都要重建一次。

        FAISS 的 Flat 索引不支持原地删除，所以父类 remove/add 之后
        统一走"整体重建"这条路，逻辑简单且不会出错。
        """
        index = self._faiss.IndexFlatIP(EMBEDDING_DIM)
        if self._use_gpu:
            try:
                index = self._faiss.index_cpu_to_gpu(self._gpu_resources, 0, index)
            except Exception as exc:
                # GPU 索引构建失败（显存不足等）→ 退回 CPU 索引，继续可用。
                print(f"FAISS GPU unavailable ({exc}); falling back to FAISS CPU")
                self._use_gpu = False
                self.backend = "FAISS CPU"
        if self.size():
            index.add(self._vectors)  # 把父类维护的全部向量灌进 FAISS
        self._index = index

    def search_batch(self, embeddings: np.ndarray, top_k: int = 5) -> list[list[SearchHit]]:
        """批量检索 TopK。

        与 NumPy 版行为一致：库为空返回空列表；top_k 超过库大小时返回全部。
        注意 FAISS 返回的可能是 -1 下标（理论上 Flat 索引不会），
        这里不做额外过滤以保持与父类一致的行为。
        """
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        queries = _normalized_matrix(embeddings)
        if not len(queries):
            return []
        if not self.size():
            return [[] for _ in range(len(queries))]

        # FAISS 要求 k 不超过库中向量总数，否则会报错。
        scores, indices = self._index.search(queries, min(top_k, self.size()))
        return [[SearchHit(self._ids[int(j)], float(score)) for j, score in zip(row, row_scores)]
                for row, row_scores in zip(indices, scores)]


def create_index(backend: str = "faiss", use_gpu: bool = True):
    """特征库工厂函数：按名字创建索引，并在不可用时自动降级。

    降级链条：FAISS GPU → FAISS CPU → NumPy CPU。
    每一次降级都会打印提示，绝不静默切换。

    参数：
        backend: "faiss" 或 "numpy"。
        use_gpu: 是否尝试把 FAISS 索引放到 GPU 上。
    返回：
        一个满足 FaceIndex 接口的实例。
    """
    if backend == "numpy":
        return NumPyIndex()
    if backend != "faiss":
        raise ValueError("backend must be 'faiss' or 'numpy'")
    try:
        return FAISSIndex(use_gpu=use_gpu)
    except RuntimeError as exc:
        # 通常是没装 faiss 包，退回 NumPy 保证功能可用。
        print(f"{exc}; falling back to NumPy CPU")
        return NumPyIndex()
