"""示例脚本：本机摄像头的可见预览窗口。

用法：
    python examples/preview_camera.py --camera 0

用途：只确认摄像头能出画面、分辨率和帧率是否正常。
它不运行任何识别模型，也不保存任何图片。

重要：预览做了水平镜像（像照镜子），但识别算法接收的始终是未镜像的原始帧。
镜像只是显示效果，不能影响任何算法输入。
"""

import argparse
import sys
import time
import tkinter as tk
from pathlib import Path

import cv2
from PIL import Image, ImageTk


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--status-file", type=Path)  # 可选：把状态写给外部脚本读取
    args = parser.parse_args()

    def status_file(message: str):
        if args.status_file:
            args.status_file.write_text(message, encoding="utf-8")

    # Windows 上 DirectShow 后端更稳，其他平台用默认后端。
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    capture = cv2.VideoCapture(args.camera, backend)
    if not capture.isOpened():
        capture.release()
        status_file("ERROR: camera could not open")
        return 1

    root = tk.Tk()
    root.title(f"Face Engine Camera Preview - Camera {args.camera}")
    root.configure(bg="#111827")
    image_label = tk.Label(root, bg="#111827")
    image_label.pack(padx=12, pady=(12, 4))
    status_label = tk.Label(root, text="Opening camera...", fg="white", bg="#111827")
    status_label.pack(pady=4)
    tk.Button(root, text="Close camera (Esc)", command=lambda: close()).pack(pady=(4, 12))
    root.attributes("-topmost", True)  # 先置顶，1.2 秒后取消，确保用户看得到
    root.after(1200, lambda: root.attributes("-topmost", False))
    root.lift()
    root.focus_force()
    last_frame = time.perf_counter()
    frame_count = 0

    def close():
        capture.release()
        status_file("CLOSED")
        root.destroy()

    def update():
        nonlocal last_frame, frame_count
        ok, frame = capture.read()
        if not ok:
            status_label.configure(text="Camera read failed; close this window to release it.")
            status_file("ERROR: camera read failed")
            return  # 读取失败就不再排下一次刷新
        height, width = frame.shape[:2]
        # Mirror only the on-screen preview, like a front-facing camera.
        # The recognition pipeline still receives the untouched BGR frame.
        # 只对显示做镜像；送给识别的仍是原始 BGR 帧。
        rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
        photo = ImageTk.PhotoImage(image=Image.fromarray(rgb), master=root)
        image_label.configure(image=photo)
        image_label.image = photo  # 必须持有引用，否则图片会被回收后消失
        frame_count += 1
        now = time.perf_counter()
        if frame_count == 1:
            status_file(f"LIVE {width}x{height}")
        # 每 15 帧统计一次实际帧率。
        if frame_count % 15 == 0:
            fps = 15 / max(now - last_frame, 1e-6)
            status_label.configure(text=f"LIVE  {width}x{height}  {fps:.1f} FPS  |  Mirrored preview  |  No images saved")
            last_frame = now
        root.after(1, update)

    root.protocol("WM_DELETE_WINDOW", close)
    root.bind("<Escape>", lambda event: close())
    root.after(1, update)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
