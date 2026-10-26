"""【SCRFD 人脸检测器】用 ONNX Runtime 跑 SCRFD 模型，支持带五点关键点的标准模型。

SCRFD 是一种 anchor-based 的单阶段人脸检测器，一次前向就能给出：
- 是不是人脸（分类分数）
- 人脸框在哪（框回归量）
- 五官在哪（5 个关键点）

本实现支持两种常见的输出结构：
- 9 个输出 → 3 个特征层级（stride 8/16/32）
- 15 个输出 → 5 个特征层级（stride 8/16/32/64/128）
输出顺序约定为：[所有层的分数] + [所有层的框] + [所有层的关键点]。
不符合这个结构（例如不带关键点的纯检测模型）会直接报错，而不是猜着解。
"""

from pathlib import Path

import cv2
import numpy as np

from face_engine.detector.base import FaceDetection, FaceDetector
from face_engine.onnx_backend import create_session


def prepare_scrfd_input(image: np.ndarray, input_size: tuple[int, int]) -> tuple[np.ndarray, float]:
    """把任意尺寸的 BGR 帧预处理成 SCRFD 需要的输入张量。

    做两件事：
    1. letterbox 等比缩放——保持宽高比不被拉扁（人脸变形会严重影响检测效果），
       短边之外的空白处补黑。
    2. 转 RGB + NCHW 排布 + 归一化。

    参数：
        image: uint8 BGR 原图。
        input_size: 模型输入尺寸 (宽, 高)，例如 (640, 640)。
    返回：
        (blob, scale)：
        - blob: 形状 [1, 3, H, W] 的 float32 张量
        - scale: 实际缩放比例，后续用它把框坐标还原回原图尺度
    """
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError("image must be uint8 BGR [H, W, 3]")
    height, width = image.shape[:2]
    if min(height, width) == 0:
        raise ValueError("image dimensions must be positive")
    input_w, input_h = input_size

    # 等比缩放：取宽高两个方向上"缩得更狠"的比例，保证整张图能完整放进去。
    scale = min(input_w / width, input_h / height)
    resized_w, resized_h = max(1, round(width * scale)), max(1, round(height * scale))

    # 先铺一张全黑画布，再把缩放后的图贴到左上角——这就是 letterbox。
    canvas = np.zeros((input_h, input_w, 3), dtype=np.uint8)
    canvas[:resized_h, :resized_w] = cv2.resize(image, (resized_w, resized_h))

    blob = np.empty((1, 3, input_h, input_w), dtype=np.float32)
    # Fill the NCHW channels directly; avoids an extra interleaved float32 copy.
    # 直接按通道填充 NCHW，省掉一次 HWC→CHW 的转置拷贝；
    # 同时 OpenCV 是 BGR、模型要 RGB，倒序填充顺便完成了 BGR→RGB 转换。
    blob[0, 0] = canvas[:, :, 2]
    blob[0, 1] = canvas[:, :, 1]
    blob[0, 2] = canvas[:, :, 0]

    # 归一化到约 [-1, 1]：SCRFD 训练时的标准预处理。
    blob -= 127.5
    blob *= 1 / 128
    return blob, scale


class SCRFDDetector(FaceDetector):
    """SCRFD ONNX 检测器实现。

    构造参数：
        model_path: ONNX 权重路径。
        input_size: 模型输入尺寸，必须是 32 的倍数（下采样要求），默认 640×640。
        confidence_threshold: 分数低于此值的候选直接丢弃。
        nms_threshold: 非极大值抑制的 IoU 阈值，越大保留越多重叠框。
        device: "cuda" 优先 GPU，不可用时自动降级 CPU（见 onnx_backend）。
        session: 允许直接注入已建好的 session，主要用于单元测试注入假模型。
    """

    def __init__(self, model_path: str | Path, input_size=(640, 640), confidence_threshold=0.5,
                 nms_threshold=0.4, device="cuda", session=None, cpu_threads: int | None = None):
        # 没注入 session 时才真正加载模型；注入时后端标记为 "TEST"（测试桩）。
        self.session, self.backend = create_session(model_path, device, cpu_threads) if session is None else (session, "TEST")
        inputs = self.session.get_inputs()
        if len(inputs) != 1:
            raise ValueError("SCRFD model must have one float32 NCHW input")
        model_input = inputs[0]
        self.input_name = model_input.name
        self.input_size = tuple(input_size)
        if len(self.input_size) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, np.integer)) or
                                          x <= 0 or x % 32 for x in self.input_size):
            raise ValueError("input_size must be positive [width, height] divisible by 32")
        shape = model_input.shape
        if len(shape) != 4 or model_input.type != "tensor(float)":
            raise ValueError("SCRFD model must have a float32 NCHW input")
        expected_input = (1, 3, self.input_size[1], self.input_size[0])
        if any(isinstance(actual, int) and actual != expected
               for actual, expected in zip(shape, expected_input)):
            raise ValueError("SCRFD model input shape conflicts with batch 1, RGB channels or configured size")
        self.confidence_threshold = confidence_threshold
        self.nms_threshold = nms_threshold
        # 缓存 anchor 中心点：同一 (stride, anchor 数) 组合只算一次，反复调用可复用。
        self._anchor_centers: dict[tuple[int, int], np.ndarray] = {}

        # 用输出个数推断模型有几个特征层级。
        outputs = self.session.get_outputs()
        if len(outputs) == 9:
            self.strides = (8, 16, 32)
            self.num_anchors = 2
        elif len(outputs) == 15:
            self.strides = (8, 16, 32, 64, 128)
            self.num_anchors = 1
        else:
            # 不支持的结构宁可明确报错，也不要猜——猜错会得到一堆错误的框。
            raise ValueError("SCRFD model must have 9 or 15 outputs including five keypoints")
        if any(size % self.strides[-1] for size in self.input_size):
            raise ValueError("SCRFD input_size must be divisible by the largest model stride")

    def detect_faces(self, image: np.ndarray) -> list[FaceDetection]:
        """检测一帧里的所有人脸。

        流程：预处理 → 模型前向 → 逐层解码 anchor → 过滤低分 → NMS → 按分数降序返回。
        """
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError("image must be uint8 BGR [H, W, 3]")
        height, width = image.shape[:2]
        if min(height, width) == 0:
            return []  # 空帧视为 0 张脸，不报错。
        input_w, input_h = self.input_size

        blob, scale = prepare_scrfd_input(image, self.input_size)
        outputs = self.session.run(None, {self.input_name: blob})
        count = len(self.strides)
        if len(outputs) != count * 3:
            raise ValueError("SCRFD runtime output count conflicts with model heads")
        boxes, raw_boxes, points, scores = [], [], [], []

        # 逐特征层解码。输出按 [分数×count, 框×count, 关键点×count] 排布，
        # 所以第 level 层的三类数据下标分别是 level、level+count、level+2*count。
        for level, stride in enumerate(self.strides):
            grid_h, grid_w = input_h // stride, input_w // stride
            cell_count = grid_h * grid_w
            anchors = self.num_anchors
            expected_rows = cell_count * anchors
            heads = []
            # Validate actual runtime tensors, not possibly stale ONNX output
            # metadata. Never reinterpret a mismatched grid as more anchors.
            for offset, columns in ((0, 1), (count, 4), (2 * count, 10)):
                head = np.asarray(outputs[level + offset])
                if head.ndim == 3 and head.shape[0] == 1:
                    head = head[0]
                if head.shape != (expected_rows, columns) or not np.isfinite(head).all():
                    raise ValueError(f"Unexpected SCRFD output shape or values at stride {stride}; "
                                     f"expected [{expected_rows}, {columns}]")
                heads.append(head)
            score, box, kps = heads[0][:, 0], heads[1], heads[2].reshape(-1, 5, 2)

            # 先按分数筛掉绝大多数候选，后续解码只在幸存者上做，省时。
            selected = np.flatnonzero(score >= self.confidence_threshold)
            if not len(selected):
                continue

            # 生成（或复用）该层所有 anchor 的中心点坐标（输入尺度下的像素坐标）。
            key = (stride, anchors)
            centers = self._anchor_centers.get(key)
            if centers is None:
                grid = np.stack(np.mgrid[:grid_h, :grid_w][::-1], axis=-1).reshape(-1, 2)
                centers = np.repeat(grid, anchors, axis=0).astype(np.float32) * stride
                self._anchor_centers[key] = centers
            centers = centers[selected]

            # 框解码：模型输出的是"到左/上/右/下边的距离"，乘 stride 回到输入尺度，
            # 再整体除以 scale 还原到原图尺度。
            d = box[selected] * stride
            decoded = np.concatenate((centers - d[:, :2], centers + d[:, 2:]), axis=1) / scale
            raw_boxes.append(decoded.copy())

            # 裁剪到图像边界内，避免给出画外的框。
            decoded[:, [0, 2]] = np.clip(decoded[:, [0, 2]], 0, width)
            decoded[:, [1, 3]] = np.clip(decoded[:, [1, 3]], 0, height)

            # 关键点解码：相对 anchor 中心的偏移，加回中心后再还原尺度。
            landmarks = (kps[selected] * stride + centers[:, None, :]) / scale
            boxes.append(decoded)
            points.append(landmarks)
            scores.append(score[selected])

        if not boxes:
            return []
        boxes = np.concatenate(boxes)
        raw_boxes = np.concatenate(raw_boxes)
        points = np.concatenate(points)
        scores = np.concatenate(scores)

        # 丢弃宽或高非正的非法框（数值异常时可能出现）。
        valid = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
        boxes, raw_boxes, points, scores = boxes[valid], raw_boxes[valid], points[valid], scores[valid]
        if not len(boxes):
            return []

        # OpenCV NMSBoxes expects x, y, width, height; final order is score descending.
        # NMS 去重：同一张脸常被多个 anchor 重复框到，只保留重叠框中最可信的那个。
        # 注意 OpenCV 的 NMSBoxes 要求 (x, y, w, h) 格式，所以这里做一次转换。
        xywh = np.column_stack((boxes[:, :2], boxes[:, 2:] - boxes[:, :2]))
        kept = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), self.confidence_threshold,
                                self.nms_threshold)
        kept = np.asarray(kept).reshape(-1)
        # NMS 返回顺序不保证按分数排，这里显式降序，
        # 让调用方拿到的第一张脸总是分数最高的。
        kept = kept[np.argsort(-scores[kept])]
        return [{"bbox": boxes[i].tolist(), "raw_bbox": raw_boxes[i].tolist(),
                 "landmarks": points[i].tolist(), "score": float(scores[i])}
                for i in kept]


class SizeAwareSCRFDDetector(FaceDetector):
    """按输入图大小自动选择检测器：小图用小输入尺寸模型，大图用标准模型。

    为什么需要它：
        SCRFD 的输入尺寸（如 640×640）决定了计算量。如果输入本身就是一张
        已经裁剪好的小人脸图（例如抓拍特写），再放大到 640 去做检测纯属浪费；
        而全帧画面（如 1920×1080）则需要大输入尺寸才能检出远处的小脸。

    行为：
        - 图像长边 <= small_image_max_side → 用 small 检测器（小输入尺寸，更快）
        - 否则 → 用 standard 检测器（标准输入尺寸，更准）

    注意：两个检测器仍应是"同一套权重"，只是输入尺寸不同；
    换输入尺寸会改变检测行为，因此阈值需按实际使用的分支分别验证。
    """

    def __init__(self, standard: SCRFDDetector, small: SCRFDDetector, small_image_max_side: int):
        if small_image_max_side <= 0:
            raise ValueError("small_image_max_side must be positive")
        self.standard = standard
        self.small = small
        self.small_image_max_side = small_image_max_side
        # 对外暴露的后端以标准检测器为准，便于上层打印统一的后端信息。
        self.backend = standard.backend

    def detect_faces(self, image: np.ndarray) -> list[FaceDetection]:
        # 用长边（max of H/W）判断图的规模，避免宽高比影响判断。
        if image.ndim >= 2 and max(image.shape[:2]) <= self.small_image_max_side:
            return self.small.detect_faces(image)
        return self.standard.detect_faces(image)

    def detect_close_up_faces(self, image: np.ndarray) -> list[FaceDetection]:
        """大图特写的第二次检测：标准输入没检出人脸时，由调用方按需调用。

        高清照片只裁出人脸、脸几乎占满画面时，缩到 640 输入后人脸超出标准输入
        能检出的尺寸范围，结果为 0 张。同一套权重的小输入会把这张脸缩到可检出的
        大小。小图本来就走小输入，这里直接返回空列表。坐标仍是原图坐标。
        """
        if image.ndim >= 2 and max(image.shape[:2]) > self.small_image_max_side:
            return self.small.detect_faces(image)
        return []
