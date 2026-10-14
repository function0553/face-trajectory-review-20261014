"""示例脚本：对视频源（MP4 / USB 摄像头 / RTSP）抽帧识别，输出 JSON Lines。

用法：
    python examples/recognize_video.py clip.mp4 subjects.npz
    python examples/recognize_video.py 0 subjects.npz --camera
    python examples/recognize_video.py 'rtsp://host/stream' subjects.npz

职责说明：解码和摄像头管理只在这个 Demo 里。
算法核心的 recognize_frames 只接收已解码的帧，
不连接摄像头、不处理断线重连——那些属于未来的视频接入层。

按配置的 detection_interval 和 recognition_interval 共同采样，
未采样帧不产出结果。--max-frames 可限制读取帧数。
"""

import argparse
import json

import cv2

from face_engine.config import load_config
from face_engine.detector.scrfd import SCRFDDetector
from face_engine.index.faiss_index import create_index
from face_engine.pipeline.image_pipeline import ImagePipeline
from face_engine.pipeline.video_pipeline import Frame, recognize_frames
from face_engine.recognizer.arcface import ArcFaceRecognizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="MP4 path, RTSP URL, or USB camera number")
    parser.add_argument("index", help=".npz subject embedding index")
    parser.add_argument("--camera", action="store_true", help="interpret source as integer USB camera number")
    parser.add_argument("--source-id", help="optional opaque source label included in each face result")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    d, r, i, v = (config[key] for key in ("detector", "recognizer", "face_index", "video"))

    detector = SCRFDDetector(d["model_path"], d["input_size"], d["confidence_threshold"],
                             d["nms_threshold"], config["device"])
    recognizer = ArcFaceRecognizer(r["model_path"], r["batch_size"], config["device"])
    index = create_index(i["backend"], i["use_gpu"])
    index.load(args.index)
    print(f"Detector Backend: {detector.backend}")
    print(f"Recognizer Backend: {recognizer.backend}")
    pipeline = ImagePipeline(detector, recognizer, index, config["matching"]["low_threshold"],
                             config["matching"]["high_threshold"], i["top_k"])

    # USB 摄像头编号要转成整数；RTSP URL 和文件路径保持字符串。
    source = int(args.source) if args.camera else args.source
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError("Cannot open video source")
    try:
        def decoded_frames():
            """不断读帧，读不到（视频结束或断流）就停止。"""
            while True:
                ok, image = capture.read()
                if not ok:
                    return
                yield Frame(image, capture.get(cv2.CAP_PROP_POS_MSEC) / 1000, args.source_id)

        for result in recognize_frames(decoded_frames(), pipeline, detection_interval=v["detection_interval"],
                                       recognition_interval=v["recognition_interval"],
                                       cache_enabled=v["cache_enabled"], cache_ttl_frames=v["cache_ttl_frames"],
                                       max_frames=args.max_frames):
            print(json.dumps(result, ensure_ascii=False))
    finally:
        capture.release()  # 无论正常结束还是异常，都要释放摄像头


if __name__ == "__main__":
    main()
