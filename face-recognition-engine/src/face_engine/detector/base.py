"""【人脸检测器抽象契约】定义"检测人脸"这一环节的统一接口。

本文件只规定接口，不实现任何具体模型。这样设计的目的：
- 上层流水线（ImagePipeline）只依赖这个接口，不关心底层是 SCRFD、YuNet 还是其他检测器；
- 将来换成 TensorRT、C++ 实现或虹软 SDK 时，只要新写一个满足本接口的类即可，
  上层代码一行都不用改。

检测器的职责边界：输入一张 BGR 图，输出 0 张、1 张或多张人脸。
它不做识别、不比对、不管摄像头，也不理解业务含义（谁是谁）。
"""

from abc import ABC, abstractmethod
from typing import TypedDict

import numpy as np


class _OptionalDetectionFields(TypedDict, total=False):
    # Unclipped original-image coordinates for boundary quality measurement.
    raw_bbox: list[float]


class FaceDetection(_OptionalDetectionFields):
    """单张人脸的检测结果（一个纯数据字典，不含任何业务逻辑）。"""

    bbox: list[float]  # 人脸框 [x1, y1, x2, y2]，单位是原图像素坐标
    landmarks: list[list[float]]  # 五官关键点：5 个 [x, y] 坐标对，同样是原图像素坐标
    score: float  # 检测置信度，越高越可能是人脸（由具体模型定义，不同检测器不可横向比较）


class FaceDetector(ABC):
    """人脸检测器抽象基类：所有具体检测器都必须实现 detect_faces。"""

    @abstractmethod
    def detect_faces(self, image: np.ndarray) -> list[FaceDetection]:
        """检测一张图里的全部人脸。

        参数：
            image: OpenCV 格式的 BGR 图像，形状 [H, W, 3]，dtype 为 uint8。
        返回：
            人脸列表。空画面返回 []（不是错误）；有 N 张脸就返回 N 个元素。
            注意：本引擎要求"多人脸"，因此绝不能只返回分数最高的那一张。
        """
