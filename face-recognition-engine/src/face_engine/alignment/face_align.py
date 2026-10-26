"""【五点对齐】把检测到的任意姿态人脸，变换成 ArcFace 要求的标准 112×112 姿态。

为什么要对齐：
    人脸特征提取模型对姿态非常敏感。同一个人，正脸和歪头 15 度拍出来的特征
    可能差得很远。对齐的作用是"摆正"人脸——把眼睛、鼻子、嘴角这五个点
    通过相似变换（缩放 + 旋转 + 平移）贴到标准位置上，再去提特征，
    这样不同照片之间才可比。

为什么只用相似变换（不做透视/仿射）：
    仿射变换会引入剪切形变，把脸"拉歪"；相似变换保持形状不变，
    更符合人脸识别模型的训练预期。

重要警告：
    对齐算法一旦改动，之前用旧版本生成的特征库就全部失效，
    必须从原始登记照片重新提特征重建库，绝不能新旧混用。
"""

import cv2
import numpy as np


# ArcFace 官方的五点标准模板，坐标基于 112×112 画布。
# 顺序固定为：[左眼, 右眼, 鼻尖, 左嘴角, 右嘴角]。
# 这五个数值来自 ArcFace 训练时的约定，不能随意改动，
# 改动就等于换了一种预处理，特征库和阈值都要重做。
_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32
)


def _estimate_similarity_transform(points: np.ndarray, target: np.ndarray) -> np.ndarray:
    """根据五组对应点，解出一个二维相似变换矩阵（2×3）。

    这里刻意用全部五个点做最小二乘拟合，而不是用 RANSAC 之类的鲁棒估计。
    原因：人说话、微笑时嘴角会动。鲁棒估计可能把"动了的嘴角"当成离群点丢掉，
    只用剩下的眼睛和鼻子去拟合，结果整张脸的裁剪位置会偏。
    ArcFace 的参考实现也是拟合完整五点，这里保持一致。

    参数：
        points: 原图上的五个关键点，形状 (5, 2)。
        target: 目标标准位置，形状 (5, 2)，即 _TEMPLATE。
    返回：
        2×3 的仿射变换矩阵，可直接喂给 cv2.warpAffine。
    异常：
        关键点退化（重合、共线、含 NaN）时抛 ValueError——
        这种情况下算不出有意义的变换，宁可报错也不要产出一张废图。
    """
    source = np.asarray(points, dtype=np.float64)
    destination = np.asarray(target, dtype=np.float64)
    if (source.shape != (5, 2) or destination.shape != (5, 2) or
            not np.isfinite(source).all() or not np.isfinite(destination).all()):
        raise ValueError("landmarks must contain five finite [x, y] pairs")

    # 第一步：各自去中心化。把平移和旋转/缩放解耦，可以分开求解。
    source_mean = source.mean(axis=0)
    target_mean = destination.mean(axis=0)
    source_centered = source - source_mean
    target_centered = destination - target_mean

    # 第二步：算源点集的二阶矩，用来判断点集是否"退化"。
    xx = float(np.sum(source_centered[:, 0] ** 2))
    yy = float(np.sum(source_centered[:, 1] ** 2))
    xy = float(np.sum(source_centered[:, 0] * source_centered[:, 1]))
    variance = xx + yy
    # 退化判据：点全重合（方差≈0），或五点几乎共线（行列式≈0）。
    if variance <= 1e-8 or xx * yy - xy * xy <= variance * variance * 1e-8:
        raise ValueError("cannot align degenerate face landmarks")

    # 第三步：闭式最小二乘解 2-D 相似变换，旋转缩放部分形如 [[a, -b], [b, a]]。
    # 相比通用做法（两次 SVD），这里直接套公式，更快也更省。
    a = float(np.sum(source_centered * target_centered) / variance)
    b = float(np.sum(source_centered[:, 0] * target_centered[:, 1] -
                     source_centered[:, 1] * target_centered[:, 0]) / variance)
    rotation_scale = np.array([[a, -b], [b, a]])

    # 第四步：由中心点反推平移量，使得源中心恰好变换到目标中心。
    translation = target_mean - rotation_scale @ source_mean
    transform = np.column_stack((rotation_scale, translation))

    # 兜底检查：矩阵不能含 NaN，缩放也不能退化成 0（否则会得到一张空白图）。
    if not np.isfinite(transform).all() or a * a + b * b <= 1e-12:
        raise ValueError("cannot align degenerate face landmarks")
    return transform


def alignment_scale(landmarks: np.ndarray | list) -> float:
    """align_face 对这组五点使用的缩放倍数（112 模板像素 / 原图像素）。

    小于 1 表示识别模型拿到的是缩小后的人脸。质量判定用它在识别模型实际
    看到的尺度上复测清晰度；不改变对齐本身。关键点退化时抛 ValueError。
    """
    transform = _estimate_similarity_transform(np.asarray(landmarks, dtype=np.float32), _TEMPLATE)
    return float(np.hypot(transform[0, 0], transform[1, 0]))


def align_face(image: np.ndarray, landmarks: np.ndarray | list, output_size: int = 112) -> np.ndarray:
    """把原图中的一张脸裁剪并摆正成 112×112，供 ArcFace 提特征。

    参数：
        image: 原图，uint8 BGR，[H, W, 3]。注意传原图而不是裁剪后的小图，
               因为人脸可能靠近边缘，裁剪可能超出边界。
        landmarks: 该张脸的五个关键点，形状 (5, 2)，原图坐标系。
        output_size: 输出边长，目前只支持 112（ArcFace 的固定输入尺寸）。
    返回：
        112×112×3 的 uint8 BGR 对齐人脸图。
    """
    points = np.asarray(landmarks, dtype=np.float32)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError("image must be uint8 BGR [H, W, 3]")
    if points.shape != (5, 2) or not np.isfinite(points).all():
        raise ValueError("landmarks must contain five finite [x, y] pairs")
    if output_size != 112:
        raise ValueError("only 112x112 ArcFace alignment is supported")

    transform = _estimate_similarity_transform(points, _TEMPLATE)
    # 双线性插值做仿射变换；超出原图的部分自动填黑。
    return cv2.warpAffine(image, transform, (112, 112), flags=cv2.INTER_LINEAR)
