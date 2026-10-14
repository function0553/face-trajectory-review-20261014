"""示例脚本：识别单张图片里的所有人脸，输出完整 JSON 结果。

用法：
    python examples/recognize_image.py camera.jpg subjects.npz

输出每张脸的：来源标签、检测框、五点关键点、检测分数、
候选 subject_id、相似度、状态（MATCH/REVIEW/UNKNOWN）和完整 TopK。

边界行为：
- 空画面 → []
- 有脸但索引为空 → UNKNOWN、similarity 为 null、top_k 为 []
"""

import argparse
import json

import cv2

from face_engine.config import load_config
from face_engine.detector.scrfd import SCRFDDetector
from face_engine.index.faiss_index import create_index
from face_engine.pipeline.image_pipeline import ImagePipeline
from face_engine.recognizer.arcface import ArcFaceRecognizer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("index", help=".npz index created by FaceIndex.save")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    d, r, i = config["detector"], config["recognizer"], config["face_index"]

    detector = SCRFDDetector(d["model_path"], d["input_size"], d["confidence_threshold"],
                             d["nms_threshold"], config["device"])
    recognizer = ArcFaceRecognizer(r["model_path"], r["batch_size"], config["device"])
    index = create_index(i["backend"], i["use_gpu"])
    index.load(args.index)
    print(f"Detector Backend: {detector.backend}")
    print(f"Recognizer Backend: {recognizer.backend}")

    # 组装引擎：阈值和 top_k 都来自配置。
    pipeline = ImagePipeline(detector, recognizer, index, config["matching"]["low_threshold"],
                             config["matching"]["high_threshold"], i["top_k"])
    image = cv2.imread(args.image)
    if image is None:
        raise ValueError(f"Cannot read image: {args.image}")
    print(json.dumps(pipeline.recognize_image(image), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
