"""示例脚本：比较两张图片里的人脸是不是同一个人，输出余弦相似度。

用法：
    python examples/compare_two_faces.py first.jpg second.jpg

要求每张图恰好一张脸。输出是两个 512 维特征的点积，
因为特征已 L2 归一化，点积就是余弦相似度，范围 [-1, 1]。

注意：相似度高低本身不构成判定。
"多少算同一个人"这个阈值必须用目标场景的真实同人/异人样本标定，
配置里的阈值只是开发占位值。
"""

import argparse

import numpy as np

from face_engine.alignment.face_align import align_face
from face_engine.config import load_config
from face_engine.cli import _image, _models
from face_engine.quality import QualityPolicy, evaluate_face_quality


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("first")
    parser.add_argument("second")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    detector, recognizer = _models(config)
    quality_policy = QualityPolicy.from_config(config.get("quality"))

    aligned = []
    for path in (args.first, args.second):
        image = _image(path)
        faces = detector.detect_faces(image)
        if len(faces) != 1:
            raise ValueError(f"Expected exactly one face in {path}; found {len(faces)}")
        quality = evaluate_face_quality(image, faces[0], quality_policy)
        if not quality["accepted"]:
            raise ValueError(f"QUALITY_REJECTED in {path}: " + ", ".join(quality["reasons"]))
        aligned.append(align_face(image, faces[0]["landmarks"]))

    # 两张脸合成一个 batch 一次推理。
    embeddings = recognizer.extract_embeddings(aligned)
    # 向量已归一化，点积即余弦相似度。
    print(f"Cosine similarity: {float(np.dot(embeddings[0], embeddings[1])):.6f}")


if __name__ == "__main__":
    main()
