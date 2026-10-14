"""【本机摄像头演示】单人内存登记 + 实时识别的可见窗口 Demo。

用途：
    在没有正式人脸库、没有正式模型授权时，也能用本机 USB 摄像头
    直观看到"检测—登记—匹配"的整条链路是否跑通。

严格限制（不是生产功能）：
    - 只登记 1 张正面帧（模拟正式场景每人只有一张登记照的场景）
    - 登记特征只存在进程内存里，关闭窗口即消失，不写磁盘
    - 不保存摄像头画面（除非显式开启诊断目录并手动点按钮）
    - 阈值未经校准，不能从这里测试结果推断误认率

架构原则：摄像头的所有权（打开/读取/释放）留在本文件，
算法引擎不知道摄像头的存在——它只收到一个 BGR 帧。

线程模型（关键）：
    用一个后台单线程工作器跑推理，主线程只负责读帧和刷新界面。
    这样推理耗时不会冻结预览窗口。
    同时"最多只允许一个推理任务在跑"，避免旧帧堆积积压。
"""

from __future__ import annotations

import sys
import time
import tkinter as tk
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

from face_engine.alignment.face_align import align_face
from face_engine.camera_overlay import CameraBoxOverlay, STATUS_LABELS
from face_engine.pipeline.image_pipeline import FaceRecognitionEngine
from face_engine.quality import evaluate_face_quality

STATUS_LABELS = {**STATUS_LABELS, "QUALITY_REJECTED": "图像质量不足"}


def _enrollment_sample(engine: FaceRecognitionEngine, frame: np.ndarray):
    """从一帧里采集登记样本：检测→校验→对齐→提特征，返回 (框, 特征向量)。

    注意：本函数不保存这一帧图像，只产出特征向量。
    登记照的"原始照片"不会落到磁盘，符合演示不留存画面的约束。

    校验规则：
    - 画面中必须恰好 1 张脸（多人会登记错人，无人则无从登记）
    - 与图片和轨迹入口使用同一原始像素、清晰度和关键点质量规则
    """
    faces = engine.detector.detect_faces(frame)
    if len(faces) != 1:
        raise ValueError(f"登记时画面中须恰好一张脸，当前检测到 {len(faces)} 张")
    face = faces[0]
    quality = evaluate_face_quality(frame, face, engine.quality_policy)
    if not quality["accepted"]:
        raise ValueError("登记照片质量不足：" + ", ".join(quality["reasons"]))
    aligned = align_face(frame, face["landmarks"])
    return face["bbox"], engine.recognizer.extract_embeddings([aligned])[0]


def run_camera_demo(engine: FaceRecognitionEngine, *, camera: int = 0,
                    subject_id: str = "SELF", status_file: Path | None = None,
                    diagnostic_dir: Path | None = None) -> int:
    """打开可见窗口：镜像预览 + 一个后台推理任务。

    参数：
        engine: 已组装好的识别引擎（检测器/特征器/索引都就绪）。
        camera: 摄像头编号，默认 0。
        subject_id: 登记时使用的演示人员标识，默认 "SELF"。
        status_file: 可选；把状态文本写到该文件，供自动化冒烟测试读取。
        diagnostic_dir: 可选；给定时窗口出现"保存基准帧/标记失败帧"按钮，
                        仅在手动点击时保存当前原始帧，用于复现检测失败。
    返回：
        退出码 0。
    """
    if not subject_id:
        raise ValueError("subject_id must not be empty")

    def report(message: str):
        """把一行状态写到状态文件（供外部脚本/测试观察，非必需）。"""
        if status_file:
            status_file.write_text(message, encoding="utf-8")

    # Windows 上用 DirectShow 后端打开摄像头更稳，其他平台用默认后端。
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    capture = cv2.VideoCapture(camera, backend)
    if not capture.isOpened():
        capture.release()
        report("ERROR: camera could not open")
        raise RuntimeError(f"无法打开本机摄像头 {camera}")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    # Pillow needs a CJK font; OpenCV's built-in text renderer cannot draw Chinese.
    # OpenCV 自带的绘图函数画不了中文，所以用 Pillow 加载系统中文字体来写标签。
    # 逐个尝试常见字体路径，都找不到就退回到默认字体（此时中文会显示成方块）。
    label_font = next(
        (ImageFont.truetype(str(path), 17) for path in (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ) if path.exists()),
        ImageFont.load_default(),
    )

    # ============ 界面搭建 ============
    root = tk.Tk()
    root.title("人脸算法：本机摄像头识别测试")
    root.configure(bg="#111827")
    image_label = tk.Label(root, bg="#111827")
    image_label.pack(padx=12, pady=(12, 4))
    status_label = tk.Label(root, text="请正对摄像头，点击登记本人", fg="white", bg="#111827")
    status_label.pack(pady=4)
    note = ("单张登记照测试，阈值未校准；仅点击标记按钮时保存诊断帧"
            if diagnostic_dir is not None else "单张登记照测试，阈值未校准；照片和特征不会写入磁盘")
    # 把两个模型实际跑在什么后端上显示出来（CUDA 还是 CPU），便于一眼确认环境。
    detector_backend = getattr(engine.detector, "backend", "未知")
    recognizer_backend = getattr(engine.recognizer, "backend", "未知")
    detail_label = tk.Label(root, text=f"{note}\n推理后端：检测 {detector_backend} / 特征 {recognizer_backend}",
                            fg="#9ca3af", bg="#111827")
    detail_label.pack(pady=(0, 5))
    controls = tk.Frame(root, bg="#111827")
    controls.pack(pady=(0, 12))

    # 单工作线程：一次只跑一个推理任务。
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-demo-inference")
    overlay = CameraBoxOverlay()
    pending: Future | None = None  # 正在跑的推理任务
    pending_frame: np.ndarray | None = None  # 该任务所用的帧
    # 推理期间主线程仍在读帧，把这些中间帧的灰度图缓存下来，
    # 结果回来后逐帧推进覆盖层，让框"补上"推理期间的位移。
    pending_previews: deque[tuple[float, np.ndarray]] = deque(maxlen=24)
    pending_kind = ""  # "enroll" 或 "recognize"
    enrolling = False
    enrolled = False
    last_submit_at = 0.0
    closed = False
    frame_count = 0
    latest_frame: np.ndarray | None = None
    enrollment_frame: np.ndarray | None = None
    reference_count = 0
    failure_count = 0

    def start_enrollment():
        """点击"登记本人"：进入登记模式，清空已有登记。"""
        nonlocal enrolling, enrolled, enrollment_frame
        enrolling = True
        enrolled = False
        enrollment_frame = None
        overlay.clear()
        status_label.configure(text="正在登记：正面面向摄像头，保持画面中只有你")
        report("ENROLLING")

    def close():
        """关闭窗口：释放摄像头、停掉工作线程。可重入保护。"""
        nonlocal closed
        if closed:
            return
        closed = True
        capture.release()
        worker.shutdown(wait=False, cancel_futures=True)
        report("CLOSED")
        root.destroy()

    def save_diagnostic(kind: str):
        """保存诊断帧。只在用户主动点击时调用，绝不自动保存。

        kind="reference" 存登记时的基准帧（限 1 张）；
        其余情况存"失败帧"（限 5 张），用于事后复现漏检。
        """
        nonlocal reference_count, failure_count
        if diagnostic_dir is None:
            return
        if kind == "reference":
            if enrollment_frame is None:
                status_label.configure(text="请先完成登记，再保存基准帧")
                return
            if reference_count >= 1:
                status_label.configure(text="基准帧只保存 1 张")
                return
            reference_count += 1
            name = f"reference_{reference_count:02d}.png"
        else:
            if failure_count >= 5:
                status_label.configure(text="失败帧最多保存 5 张")
                return
            failure_count += 1
            name = f"failure_{failure_count:02d}.png"
        selected_frame = enrollment_frame if kind == "reference" else latest_frame
        if selected_frame is None:
            status_label.configure(text="当前没有可保存的摄像头帧")
            return
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        target = diagnostic_dir / name
        encoded, buffer = cv2.imencode(".png", selected_frame)
        if not encoded:
            raise RuntimeError("无法编码诊断帧")
        target.write_bytes(buffer.tobytes())
        status_label.configure(text=f"已保存 {name}，仅供本机诊断")
        report(f"CAPTURED {target}")

    tk.Button(controls, text="登记本人（1 张）", command=start_enrollment).pack(side="left", padx=6)
    if diagnostic_dir is not None:
        tk.Button(controls, text="保存基准帧", command=lambda: save_diagnostic("reference")).pack(side="left", padx=6)
        tk.Button(controls, text="标记失败帧", command=lambda: save_diagnostic("failure")).pack(side="left", padx=6)
    tk.Button(controls, text="关闭摄像头 (Esc)", command=close).pack(side="left", padx=6)
    root.protocol("WM_DELETE_WINDOW", close)
    root.bind("<Escape>", lambda event: close())
    root.attributes("-topmost", True)  # 先置顶确保窗口被看到，1.2 秒后取消
    root.after(1200, lambda: root.attributes("-topmost", False))
    root.lift()
    root.focus_force()

    def update():
        """主循环回调：读一帧 → 收取推理结果 → 提交新任务 → 刷新画面。"""
        nonlocal pending, pending_frame, pending_kind, enrolling, enrolled
        nonlocal last_submit_at, frame_count, latest_frame, enrollment_frame
        if closed:
            return
        ok, frame = capture.read()
        if not ok:
            status_label.configure(text="摄像头读取失败，请关闭窗口后重试")
            report("ERROR: camera read failed")
            root.after(100, update)
            return
        frame_count += 1
        latest_frame = frame
        now = time.perf_counter()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if pending is not None:
            # Captured preview frames bridge the inference-to-display motion gap.
            # 推理进行中的每一帧都留一份灰度图，待结果回来后用于推进框的位置。
            pending_previews.append((now, gray))

        # ===== 收取已完成的推理结果 =====
        if pending is not None and pending.done():
            try:
                value = pending.result()
                if pending_kind == "enroll":
                    bbox, vector = value
                    if pending_frame is not None:
                        enrollment_frame = pending_frame.copy()
                        overlay.reset(pending_frame,
                                      [{"bbox": bbox, "status": "ENROLLING"}], last_submit_at)
                        # 用推理期间缓存的中间帧把框推进到当前画面位置。
                        for captured_at, preview in pending_previews:
                            overlay.advance_gray(preview, captured_at)
                    # 登记前清空库：本演示只维护一个人。
                    engine.index.clear()
                    engine.index.add(subject_id, vector)
                    enrolled, enrolling = True, False
                    status_label.configure(text="单张登记完成。离开画面再回来，观察识别结果")
                    report("ENROLLED")
                elif pending_kind == "recognize":
                    if pending_frame is not None:
                        overlay.reset(pending_frame, value, last_submit_at)
                        for captured_at, preview in pending_previews:
                            overlay.advance_gray(preview, captured_at)
                    if value:
                        # 展示分数最高的那张脸的详细信息。
                        best = value[0]
                        score = best["similarity"]
                        label = best["status"]
                        score_text = f"{score:.3f}" if score is not None else "--"
                        x1, y1, x2, y2 = best["bbox"]
                        size = f"{x2-x1:.0f}x{y2-y1:.0f}"
                        detection = f"{best['detection_score']:.3f}"
                        status_label.configure(text=f"{STATUS_LABELS.get(label, label)}  相似度 {score_text}  检测 {detection}  人脸 {size}")
                        report(f"RESULT {label} similarity={score_text} detection={detection} size={size} faces={len(value)}")
                    else:
                        status_label.configure(text="未检测到人脸")
                        report("RESULT NO_FACE faces=0")
            except Exception as exc:
                # 登记/识别失败（例如画面里有多人）时把原因显示给用户，而不是崩溃。
                overlay.clear()
                status_label.configure(text=str(exc))
                report(f"ERROR: {exc}")
            pending = None
            pending_frame = None
            pending_previews.clear()
            pending_kind = ""

        # ===== 提交新的推理任务 =====
        # A single pending job prevents stale-frame buildup; preview remains responsive.
        # 只有上一个任务完成后才提交新的，这样不会积压一堆过期帧。
        # 登记时限流 0.35 秒一次，避免用户还没摆好姿势就连续提交。
        interval = 0.35 if enrolling else 0.0
        if pending is None and now - last_submit_at >= interval:
            if enrolling:
                pending_frame = frame.copy()
                pending_previews.clear()
                pending = worker.submit(_enrollment_sample, engine, pending_frame)
                pending_kind = "enroll"
                last_submit_at = now
            elif enrolled:
                pending_frame = frame.copy()
                pending_previews.clear()
                # 注意：送进去的是原始帧副本，source_id 只是给结果打个来源标签。
                pending = worker.submit(engine.recognize_image, pending_frame, f"WEBCAM_{camera}")
                pending_kind = "recognize"
                last_submit_at = now
            elif frame_count % 12 == 0:
                report(f"LIVE {frame.shape[1]}x{frame.shape[0]} READY")

        # ===== 刷新画面 =====
        # Only display is mirrored; inference always sees the original camera frame.
        # 只有显示做了水平镜像（像照镜子）；算法输入始终是未镜像的原始帧。
        display = Image.fromarray(cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB))
        shown = overlay.advance_gray(gray, now)
        if shown:
            draw = ImageDraw.Draw(display)
            width = frame.shape[1]
            for item in shown:
                x1, y1, x2, y2 = item["bbox"]
                status = item["status"]
                # 按状态配色：匹配绿、待复核黄、未知红，其余（如登记中）蓝。
                color = {"MATCH": "#22c55e", "REVIEW": "#f59e0b",
                         "UNKNOWN": "#ef4444"}.get(status, "#38bdf8")
                # 画面已水平镜像，所以框的横坐标要用 width - x 换算到镜像后的位置。
                draw.rectangle((width - x2, y1, width - x1, y2), outline=color, width=3)
                label = STATUS_LABELS.get(status, status)
                bounds = draw.textbbox((0, 0), label, font=label_font)
                label_width = bounds[2] - bounds[0] + 10
                label_height = bounds[3] - bounds[1] + 6
                label_x = max(0, min(width - label_width, width - x2))
                label_y = max(0, y1 - label_height)
                draw.rectangle((label_x, label_y, label_x + label_width,
                                label_y + label_height), fill=color)
                draw.text((label_x + 5 - bounds[0], label_y + 3 - bounds[1]),
                          label, fill="white", font=label_font)
        photo = ImageTk.PhotoImage(display, master=root)
        image_label.configure(image=photo)
        image_label.image = photo  # 必须自己持有引用，否则会被垃圾回收导致图片消失
        root.after(1, update)  # 立即排下一次刷新，保持预览流畅

    report("OPENING")
    root.after(1, update)
    root.mainloop()
    return 0
