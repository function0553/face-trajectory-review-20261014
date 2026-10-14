"""【预览框跟随】只在本地摄像头预览窗口里，让检测框在两次推理之间平滑跟随画面。

重要边界：
    本模块只负责"把框画在哪"，不参与任何身份判定。
    它移动的是最近一次真实推理得到的框，框上的 MATCH/REVIEW/UNKNOWN 标签
    始终来自 FaceRecognitionEngine，本模块不会、也绝不能改变身份结论。

为什么需要它：
    摄像头 30fps，但推理一帧可能要几十毫秒。如果只在推理完成时刷新框，
    框会一卡一卡地跳。这里用光流估计两帧之间画面的位移，
    把框平移到当前帧的对应位置，看起来就跟手了。

为什么是"短时"的（max_age 默认 0.8 秒）：
    位移估计是靠相邻帧的小运动累积出来的，时间越长误差越大。
    超过 max_age 就主动放弃、把框藏起来，
    宁可短暂不显示，也不要画一个明显错位、可能误导人的框。
"""

from __future__ import annotations

import cv2
import numpy as np


# 状态标签的中文案。注意这是给本机演示界面看的，
# 与算法输出的 status 字符串一一对应，不改变语义。
STATUS_LABELS = {
    "MATCH": "匹配",
    "REVIEW": "待复核",
    "UNKNOWN": "未知",
    "ENROLLING": "已登记",
}


class CameraBoxOverlay:
    """在最多 max_age 秒的时间窗内，跟随最近一次推理得到的人脸框。"""

    def __init__(self, max_age: float = 0.8):
        """参数：
            max_age: 框的有效期（秒）。超过这个时间没有新的推理结果就把框清掉。
        """
        self.max_age = max_age
        self.clear()

    def clear(self) -> None:
        """清空全部跟随状态。"""
        self._gray: np.ndarray | None = None  # 上一帧灰度图，作为光流的参考帧
        self._items: list[dict] = []  # 当前跟随的框及其状态
        self._points: list[np.ndarray | None] = []  # 每个框内用于跟踪的角点
        self._detected_at = 0.0  # 最近一次真实推理的时刻

    def reset(self, frame: np.ndarray, results: list[dict], detected_at: float) -> None:
        """用一次真实推理的结果重新建立跟随锚点。

        关键：框和用来跟踪的像素必须来自同一帧。
        如果框来自 A 帧而角点取自 B 帧，位移估计就会错位。

        参数：
            frame: 该次推理所用的原始帧。
            results: 该次推理得到的人脸结果列表。
            detected_at: 推理时刻（perf_counter 秒）。
        """
        self.clear()
        if not results:
            return
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        for result in results:
            x1, y1, x2, y2 = (int(round(v)) for v in result["bbox"])
            # 把框夹到画面范围内，避免越界切片。
            x1, x2 = max(0, x1), min(width, x2)
            y1, y2 = max(0, y1), min(height, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            # 在框内取至多 40 个角点作为跟踪特征。
            # 角点是纹理明显的位置，光流跟踪最稳；纯色区域取不到点。
            points = cv2.goodFeaturesToTrack(
                gray[y1:y2, x1:x2], maxCorners=40, qualityLevel=0.01,
                minDistance=4, blockSize=3,
            )
            if points is not None:
                # 角点是在框内局部坐标里取的，这里平移回整帧坐标系。
                points[:, :, 0] += x1
                points[:, :, 1] += y1
            item = dict(result)
            item["bbox"] = [float(x1), float(y1), float(x2), float(y2)]
            self._items.append(item)
            self._points.append(points)
        self._gray = gray
        self._detected_at = detected_at

    def advance(self, frame: np.ndarray, now: float) -> list[dict]:
        """按一帧新画面推进框的位置（BGR 版本，内部转灰度）。"""
        return self.advance_gray(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), now)

    def advance_gray(self, gray: np.ndarray, now: float) -> list[dict]:
        """按一帧灰度画面推进框的位置，返回当前应显示的框。

        参数：
            gray: 当前帧灰度图。
            now: 当前时刻（perf_counter 秒）。
        返回：
            当前仍可信的框列表；不可信的框会被丢弃（不返回）。

        算法思路：
        1. 用金字塔 LK 光流把角点跟踪到当前帧，得到每个点的位移。
        2. 反向再跟踪一次做"前后一致性校验"：能回到原点的点才算跟踪成功。
           这一步能过滤掉大部分错误匹配（例如跟踪到了另一个人脸上）。
        3. 取所有成功点位移的中位数作为框的整体位移（中位数抗个别离群点）。
        4. 与中位数偏差小于 8 像素的点才算"意见一致"，再取一次中位数精化。
        5. 位移超过 120 像素（画面突变/物体丢失）或有效点不足 4 个 → 放弃该框。
        """
        # 没有锚点，或已超过有效期 → 直接清空，不再显示。
        if self._gray is None or now - self._detected_at > self.max_age:
            self.clear()
            return []
        height, width = gray.shape
        visible: list[dict] = []
        updated_points: list[np.ndarray | None] = []

        for item, points in zip(self._items, self._points):
            # 角点太少（少于 4 个）无法可靠估计位移，跳过这个框。
            if points is None or len(points) < 4:
                continue
            # 正向跟踪：上一帧 → 当前帧
            moved, found, _ = cv2.calcOpticalFlowPyrLK(
                self._gray, gray, points, None,
                winSize=(31, 31), maxLevel=3,
            )
            if moved is None or found is None:
                continue
            # 反向跟踪：当前帧 → 上一帧，用于校验
            back, back_found, _ = cv2.calcOpticalFlowPyrLK(
                gray, self._gray, moved, None,
                winSize=(31, 31), maxLevel=3,
            )
            if back is None or back_found is None:
                continue
            # 双向都成功，且回到原点附近（误差 < 2.5 像素）的点才可信。
            valid = (found[:, 0] == 1) & (back_found[:, 0] == 1)
            valid &= np.linalg.norm(back[:, 0] - points[:, 0], axis=1) < 2.5
            if np.count_nonzero(valid) < 4:
                continue

            offsets = moved[valid, 0] - points[valid, 0]
            shift = np.median(offsets, axis=0)  # 粗估计：位移中位数
            # 与中位数偏差 < 8 像素的点视为一致，用它们再取一次中位数精化。
            agreeing = np.linalg.norm(offsets - shift, axis=1) < 8.0
            # 一致点太少，或整体位移过大（画面突变）→ 不可信，丢弃。
            if np.count_nonzero(agreeing) < 4 or np.linalg.norm(shift) > 120:
                continue
            shift = np.median(offsets[agreeing], axis=0)

            # 把框按估计出的位移整体平移。
            x1, y1, x2, y2 = item["bbox"]
            moved_box = [x1 + float(shift[0]), y1 + float(shift[1]),
                         x2 + float(shift[0]), y2 + float(shift[1])]
            # 框被推出画面 → 放弃。
            if (moved_box[0] < 0 or moved_box[1] < 0 or
                    moved_box[2] > width or moved_box[3] > height):
                continue
            moved_item = dict(item)
            moved_item["bbox"] = moved_box
            visible.append(moved_item)
            # 只保留可信角点继续下一帧跟踪，防止误差累积。
            updated_points.append(moved[valid][agreeing].reshape(-1, 1, 2))

        # 用当前帧作为下一次的参考帧。
        self._gray = gray
        self._items = visible
        self._points = updated_points
        return visible
