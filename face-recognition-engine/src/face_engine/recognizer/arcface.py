"""【ArcFace 特征提取器】用 ONNX Runtime 批量提取 512 维人脸特征。

ArcFace 是整个识别链路的核心模型：输入 112×112 的对齐人脸，
输出一个 512 维向量。同一个人的向量彼此接近，不同人的向量彼此远离。

关键设计是"批量"：GPU 上一次算 32 张脸和算 1 张脸的耗时差别不大，
所以本类对外虽然可以只传 1 张，内部也会按 batch_size 分块批量推理。
"""

from pathlib import Path

import numpy as np

from face_engine.onnx_backend import create_session
from face_engine.recognizer.base import EMBEDDING_DIM, FaceRecognizer


def prepare_arcface_input(faces) -> np.ndarray:
    """把一批对齐后的 BGR 人脸图，转成 ArcFace 需要的 RGB NCHW 张量。

    做三件事：
    1. BGR → RGB（OpenCV 读进来是 BGR，模型训练用的是 RGB）
    2. HWC → NCHW（模型要求的排布）
    3. 归一化：减 127.5 再除 127.5，把 0~255 映射到约 [-1, 1]

    实现上直接按通道赋值，避免先构造一份 float32 的 HWC 副本再转置，
    在大批量时能省一次可观的内存拷贝。

    参数：
        faces: 一批 uint8 BGR 人脸图，形状 [N, 112, 112, 3]。
    返回：
        形状 [N, 3, 112, 112] 的 float32 张量。
    """
    batch = np.asarray(faces)
    if batch.ndim != 4 or batch.shape[1:] != (112, 112, 3) or batch.dtype != np.uint8:
        raise ValueError("aligned faces must be uint8 BGR [N, 112, 112, 3]")
    input_data = np.empty((len(batch), 3, 112, 112), dtype=np.float32)
    # 倒序取通道，一次性完成 BGR→RGB 与 HWC→CHW。
    input_data[:, 0] = batch[:, :, :, 2]
    input_data[:, 1] = batch[:, :, :, 1]
    input_data[:, 2] = batch[:, :, :, 0]
    input_data -= 127.5
    input_data /= 127.5
    return input_data


class ArcFaceRecognizer(FaceRecognizer):
    """ArcFace ONNX 特征提取器实现。

    构造参数：
        model_path: ONNX 权重路径。
        batch_size: 单次推理最多送几张脸，默认 32。
        device: "cuda" 优先 GPU，不可用时自动降级 CPU。
        session: 允许注入已建好的 session，供单元测试使用假模型。
    """

    def __init__(self, model_path: str | Path, batch_size=32, device="cuda", session=None,
                 cpu_threads: int | None = None):
        # 未注入 session 时才真正加载模型；注入时后端标记为 "TEST"。
        self.session, self.backend = create_session(model_path, device, cpu_threads) if session is None else (session, "TEST")
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        if len(shape) != 4 or shape[1:] != [3, 112, 112]:
            raise ValueError("ArcFace model input must be [batch, 3, 112, 112]")
        # 模型第一维必须是不定长（字符串或 None）。
        # 若第一维被写死成 1，就无法批量推理，会严重拖慢多人场景。
        if isinstance(shape[0], int):
            raise ValueError("ArcFace ONNX model needs a dynamic batch dimension")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size

    def extract_embeddings(self, faces) -> np.ndarray:
        """提取一批人脸的 512 维特征。

        参数：
            faces: 已对齐的 uint8 BGR 人脸图序列，每张 112×112×3。
        返回：
            形状 [N, 512] 的 float32 数组，每行已 L2 归一化（模长为 1）。
            归一化后"点积 == 余弦相似度"，检索环节可以直接用矩阵乘法。
        """
        if len(faces) == 0:
            # 空输入返回一个形状正确的空数组，而不是报错——
            # 这样"检测到 0 张脸"时能自然走通流水线。
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)

        chunks = []
        # 按 batch_size 切块推理。50 张脸 + batch_size 32 → 切成 [32, 18] 两批。
        for start in range(0, len(faces), self.batch_size):
            cuda_input = getattr(self, 'cuda_input', None)
            if cuda_input is None:
                input_data = prepare_arcface_input(faces[start:start + self.batch_size])
                outputs = self.session.run(None, {self.input_name: input_data})
            else:
                input_data = cuda_input.arcface(faces[start:start + self.batch_size])
                outputs = cuda_input.run(self.input_name, input_data)
            embeddings = np.asarray(outputs[0], dtype=np.float32)
            if embeddings.shape != (len(input_data), EMBEDDING_DIM):
                raise ValueError(f"ArcFace output must be [{len(input_data)}, 512]")
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            # 兜底：模型若输出 NaN/Inf 或全零向量，说明推理异常，
            # 必须报错而不是让脏向量污染特征库和检索结果。
            if not np.isfinite(embeddings).all() or (norms <= 0).any():
                raise ValueError("ArcFace returned non-finite or zero embeddings")
            chunks.append(embeddings / norms)  # L2 归一化
        return np.concatenate(chunks, axis=0)
