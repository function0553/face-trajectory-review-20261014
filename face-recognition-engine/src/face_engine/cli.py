"""【统一命令行入口】`python -m face_engine <子命令>`。

提供的子命令：
    enroll            从 CSV 名单建特征库
    recognize         识别单张图片里的所有人脸
    stream            持续识别一路 RTSP / USB / MP4 画面，输出 JSON Lines
    camera-demo       本机摄像头可见窗口演示（单人内存登记）
    benchmark-camera  测量摄像头采集耗时，或完整各阶段耗时

设计原则：本文件只做"组装和演示"，算法逻辑一律在 face_engine 各模块里。
摄像头连接、解码也只出现在这里和 examples/，算法核心只认已解码的帧。
"""

import argparse
import csv
import json
import statistics
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import cv2
import numpy as np

from face_engine.alignment.face_align import align_face
from face_engine.config import load_config
from face_engine.detector.scrfd import SCRFDDetector, SizeAwareSCRFDDetector
from face_engine.detector.scrfd import prepare_scrfd_input
from face_engine.index.faiss_index import create_index
from face_engine.pipeline.image_pipeline import FaceRecognitionEngine
from face_engine.pipeline.video_pipeline import Frame, recognize_frames
from face_engine.recognizer.arcface import ArcFaceRecognizer


# 项目根目录：src/face_engine/cli.py 往上两级。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _PROJECT_ROOT / "configs" / "default.yaml"


def _model_path(configured: str) -> Path:
    """把配置里的模型路径解析成绝对路径。

    配置中写相对路径时，以项目根目录为基准，
    这样无论从哪个工作目录执行命令都能找到模型。
    """
    path = Path(configured)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _models(config: dict):
    """按配置加载检测器和特征提取器。

    若配置里给了 small_image_input_size，就额外建一个小输入尺寸的检测器，
    用 SizeAwareSCRFDDetector 包装：小图走小模型省算力，大图走标准模型保精度。
    """
    d, r = config["detector"], config["recognizer"]
    detector_path = _model_path(d["model_path"])
    recognizer_path = _model_path(r["model_path"])
    # 模型权重不随仓库分发，缺失时给出明确的修复指引而不是让它后面崩溃。
    missing = [str(path) for path in (detector_path, recognizer_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing ONNX model weights: " + ", ".join(missing)
            + ". Put licensed SCRFD/ArcFace models there or edit --config."
        )
    cpu_threads = config.get("cpu_threads")
    detector = SCRFDDetector(detector_path, d["input_size"], d["confidence_threshold"],
                             d["nms_threshold"], config["device"], cpu_threads=cpu_threads)
    if "small_image_input_size" in d:
        small = SCRFDDetector(detector_path, d["small_image_input_size"], d["confidence_threshold"],
                              d["nms_threshold"], config["device"], cpu_threads=cpu_threads)
        detector = SizeAwareSCRFDDetector(detector, small, d["small_image_max_side"])
    recognizer = ArcFaceRecognizer(recognizer_path, r["batch_size"], config["device"],
                                   cpu_threads=cpu_threads)
    # 打印两个模型实际跑在什么后端上（CUDA / CPU），便于确认环境是否符合预期。
    print(f"Detector Backend: {detector.backend}")
    print(f"Recognizer Backend: {recognizer.backend}")
    return detector, recognizer


def _image(path: str | Path) -> np.ndarray:
    """读取一张图片为 BGR 数组。

    # np.fromfile + imdecode also handles Chinese file names on Windows.
    用 np.fromfile + imdecode 而不是 cv2.imread，
    因为后者在 Windows 上遇到中文路径会读不出来。
    """
    encoded = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return image


def _enroll(args, config):
    """建库：读 CSV 名单，每人一张照片，逐张检测→对齐→提特征，最后存成 .npz。

    CSV 格式（UTF-8）：
        subject_id,image_path
        FACE_000001,photos/one.jpg

    严格要求每张登记照片恰好检出一张脸：
    - 检出 0 张：这张照片没用，应换图
    - 检出多张：无法确定要登记谁，也应换图
    而不是"随便挑一张"——那会静默地建出一个错误的库。

    特征按 batch_size 攒批提取，几万人时比逐张快得多。
    """
    from face_engine.quality import QualityPolicy, evaluate_face_quality
    detector, recognizer = _models(config)
    quality_policy = QualityPolicy.from_config(config.get("quality"))
    manifest = Path(args.manifest)
    ids, aligned_batch, embedding_parts = [], [], []
    with manifest.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not {"subject_id", "image_path"}.issubset(reader.fieldnames):
            raise ValueError("CSV requires subject_id,image_path columns")
        for line, row in enumerate(reader, start=2):  # start=2：表头算第 1 行
            subject_id = row["subject_id"].strip()
            if not subject_id:
                raise ValueError(f"Missing subject_id on line {line}")
            image_path = Path(row["image_path"])
            if not image_path.is_absolute():
                image_path = manifest.parent / image_path  # 相对路径以 CSV 所在目录为基准
            image = _image(image_path)
            faces = detector.detect_faces(image)
            if len(faces) != 1:
                raise ValueError(f"Expected exactly one face on line {line}; found {len(faces)}")
            quality = evaluate_face_quality(image, faces[0], quality_policy)
            if not quality["accepted"]:
                raise ValueError(f"QUALITY_REJECTED on line {line}: " + ", ".join(quality["reasons"]))
            ids.append(subject_id)
            aligned_batch.append(align_face(image, faces[0]["landmarks"]))
            # 攒够一批就推理一次，然后清空重新攒。
            if len(aligned_batch) == config["recognizer"]["batch_size"]:
                embedding_parts.append(recognizer.extract_embeddings(aligned_batch))
                aligned_batch.clear()
    # 处理最后不满一批的零头。
    if aligned_batch:
        embedding_parts.append(recognizer.extract_embeddings(aligned_batch))
    vectors = np.concatenate(embedding_parts) if embedding_parts else np.empty((0, 512), np.float32)
    i = config["face_index"]
    index = create_index(i["backend"], i["use_gpu"])
    index.add_batch(ids, vectors)
    index.save(args.output)
    print(f"Saved {index.size()} subject embeddings to {args.output}")


def _engine(config, index_path: str | None):
    """组装一个完整的识别引擎。

    没给索引路径时库为空，此时所有检出的人脸都会是 UNKNOWN——
    这样可以在没有人员库的情况下单独验证摄像头和检测是否正常。
    """
    detector, recognizer = _models(config)
    i = config["face_index"]
    index = create_index(i["backend"], i["use_gpu"])
    if index_path:
        index.load(index_path)
    else:
        print("No subject index supplied; detected faces will be UNKNOWN.", file=sys.stderr)
    return FaceRecognitionEngine.from_config(detector, recognizer, index, config)


def _recognize(args, config):
    """识别单张图片，把结果以 JSON 打印出来。"""
    engine = _engine(config, args.index)
    results = engine.recognize_image(_image(args.image), source_id=args.source_id)
    print(json.dumps(results, ensure_ascii=False, indent=2))


def _stream(args, config):
    """持续识别一路视频源，每条结果输出一行 JSON（JSON Lines）。

    解码在这里完成，算法核心拿到的始终是已解码的帧（摄像头连接不进算法层）。
    """
    # Keep backend diagnostics visible on stderr; stdout stays JSON Lines.
    # 建引擎时会打印后端信息，重定向到 stderr，
    # 保证 stdout 是纯净的 JSON Lines，方便下游管道消费。
    with redirect_stdout(sys.stderr):
        engine = _engine(config, args.index)
    source = int(args.source) if args.camera else args.source
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError("Cannot open video source")
    video = config["video"]
    try:
        def frames():
            """生成器：不断从摄像头/视频读帧，读不到就结束。"""
            while True:
                ok, image = capture.read()
                if not ok:
                    return
                position_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
                # 实时流拿不到有效位置信息，此时时间戳为 None。
                timestamp = position_ms / 1000 if position_ms > 0 else None
                yield Frame(image=image, timestamp_seconds=timestamp, source_id=args.source_id)

        for event in recognize_frames(frames(), engine,
                                      detection_interval=video["detection_interval"],
                                      recognition_interval=video["recognition_interval"],
                                      cache_enabled=video["cache_enabled"],
                                      cache_ttl_frames=video["cache_ttl_frames"],
                                      max_frames=args.max_frames):
            print(json.dumps(event, ensure_ascii=False), flush=True)  # flush 保证实时输出
    finally:
        capture.release()


def _camera_demo(args, config):
    """启动本机摄像头可见窗口演示。

    索引固定用 NumPy 且禁用 GPU：演示只登记 1 个人，
    用不着 FAISS，也避免占用显存。
    """
    from face_engine.camera_demo import run_camera_demo

    detector, recognizer = _models(config)
    index = create_index("numpy", use_gpu=False)
    engine = FaceRecognitionEngine.from_config(detector, recognizer, index, config)
    return run_camera_demo(engine, camera=args.camera, subject_id=args.subject_id,
                           status_file=args.status_file, diagnostic_dir=args.diagnostic_dir)


def _benchmark_camera(args, config):
    """测量摄像头相关耗时。

    两种模式：
    1. 默认（不带 --full）：只测"读帧 + SCRFD 输入预处理"，不加载任何模型。
       在还没有正式模型权重时也能跑，用于验证采集链路。
    2. --full：加载真实模型，按 sample_every 抽帧，
       分别测检测、对齐、特征批量、检索以及总耗时。

    注意：读帧耗时包含等待下一帧的时间，
    所以 observed_fps 反映的是实际观测帧率，不是算法处理能力的上限。
    """
    if args.frames <= 0 or args.warmup < 0:
        raise ValueError("frames must be positive and warmup must be nonnegative")
    detector = recognizer = index = None
    if args.full:
        detector, recognizer = _models(config)
        i = config["face_index"]
        index = create_index(i["backend"], i["use_gpu"])
        if args.index:
            index.load(args.index)
    source = int(args.source) if args.camera else args.source
    # Windows 上 DirectShow 通常比默认后端更快更稳，用 --directshow 开启。
    backend = cv2.CAP_DSHOW if args.directshow else cv2.CAP_ANY
    capture = cv2.VideoCapture(source, backend)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError("Cannot open video source")
    read_ms, preprocess_ms, shape = [], [], None
    stage_ms = {key: [] for key in ("detection", "alignment", "arcface_batch", "search", "total")}
    processed_frames = faces_total = 0
    try:
        # 预热：先丢掉若干帧，避开摄像头刚打开时的曝光/白平衡调整阶段。
        for _ in range(args.warmup):
            ok, _ = capture.read()
            if not ok:
                raise RuntimeError("Video source ended during warmup")
        started = time.perf_counter()
        for frame_index in range(args.frames):
            t0 = time.perf_counter()
            ok, frame = capture.read()
            t1 = time.perf_counter()
            if not ok:
                break
            read_ms.append((t1 - t0) * 1000)
            shape = frame.shape
            if not args.full:
                # 只做预处理：letterbox + 归一化，不跑模型。
                prepare_scrfd_input(frame, tuple(config["detector"]["input_size"]))
                preprocess_ms.append((time.perf_counter() - t1) * 1000)
            elif frame_index % args.sample_every == 0:
                # 完整链路分阶段计时。
                faces = detector.detect_faces(frame)
                t2 = time.perf_counter()
                aligned = [align_face(frame, face["landmarks"]) for face in faces]
                t3 = time.perf_counter()
                if aligned:
                    embeddings = recognizer.extract_embeddings(aligned)
                    t4 = time.perf_counter()
                    index.search_batch(embeddings, top_k=config["face_index"]["top_k"])
                else:
                    t4 = time.perf_counter()  # 没检出脸就不用提特征/检索
                t5 = time.perf_counter()
                processed_frames += 1
                faces_total += len(faces)
                # t1→t2 检测，t2→t3 对齐，t3→t4 特征，t4→t5 检索，t1→t5 总计。
                for key, value in zip(stage_ms, (t2-t1, t3-t2, t4-t3, t5-t4, t5-t1)):
                    stage_ms[key].append(value * 1000)
        elapsed = time.perf_counter() - started
    finally:
        capture.release()
    if not read_ms:
        raise RuntimeError("No frames captured")

    def summary(values):
        """汇总均值和 P95（95 分位），P95 比最大值更能反映稳定表现。"""
        ordered = sorted(values)
        return {"mean_ms": round(statistics.mean(values), 3),
                "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3)}

    result = {"mode": "camera_full_pipeline_stages" if args.full else "camera_capture_and_preprocess_only",
              "model_inference": args.full, "frames": len(read_ms), "frame_shape": shape,
              "observed_fps": round(len(read_ms) / elapsed, 2),
              "read_including_frame_wait": summary(read_ms)}
    if args.full:
        result.update({"processed_frames": processed_frames, "faces_total": faces_total,
                       "index_size": index.size(), "stages": {key: summary(value) for key, value in stage_ms.items()}})
    else:
        result["scrfd_preprocess"] = summary(preprocess_ms)
    print(json.dumps(result, indent=2))



def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并分发到各子命令。

    参数：
        argv: 参数列表；None 表示从 sys.argv 取（正常命令行调用）。
    返回：
        退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(prog="python -m face_engine", description="Venue face recognition engine")
    sub = parser.add_subparsers(dest="command", required=True)


    enroll = sub.add_parser("enroll", help="build an index from a subject_id,image_path CSV")
    enroll.add_argument("manifest")
    enroll.add_argument("output")
    enroll.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    recognize = sub.add_parser("recognize", help="recognize all faces in one real image")
    recognize.add_argument("image")
    recognize.add_argument("index")
    recognize.add_argument("--source-id")
    recognize.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    stream = sub.add_parser("stream", help="continuously recognize frames from one RTSP/USB/MP4 source")
    stream.add_argument("source", help="RTSP URL, MP4 file, or USB camera number with --camera")
    stream.add_argument("index", nargs="?", help="optional saved subject index; omit for detection-only testing")
    stream.add_argument("--camera", action="store_true", help="interpret source as integer USB camera number")
    stream.add_argument("--source-id", help="opaque source label to include in each face result")
    stream.add_argument("--max-frames", type=int, help="stop after reading this many frames")
    stream.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    camera_demo = sub.add_parser("camera-demo", help="visible webcam self-enrollment and recognition demo")
    camera_demo.add_argument("--camera", type=int, default=0)
    camera_demo.add_argument("--subject-id", default="SELF")
    camera_demo.add_argument("--status-file", type=Path, help="optional local status file for smoke tests")
    camera_demo.add_argument("--diagnostic-dir", type=Path, help="save only frames marked in the visible window")
    # 演示默认用 configs/demo.yaml（临时研究模型），不动正式配置。
    camera_demo.add_argument("--config", type=Path, default=_PROJECT_ROOT / "configs" / "demo.yaml")

    benchmark = sub.add_parser("benchmark-camera", help="measure live capture and preprocessing without models")
    benchmark.add_argument("source", help="RTSP URL or USB camera number with --camera")
    benchmark.add_argument("--camera", action="store_true")
    benchmark.add_argument("--directshow", action="store_true", help="use DirectShow on Windows")
    benchmark.add_argument("--frames", type=int, default=90)
    benchmark.add_argument("--warmup", type=int, default=10)
    benchmark.add_argument("--full", action="store_true", help="profile actual detector, ArcFace, and search models")
    benchmark.add_argument("--index", help="saved subject index for full model profiling")
    benchmark.add_argument("--sample-every", type=int, default=15, help="recognize every Nth captured frame in full mode")
    benchmark.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "benchmark-camera" and args.sample_every <= 0:
            raise ValueError("sample-every must be positive")
        if args.command == "enroll":
            _enroll(args, config)
        elif args.command == "benchmark-camera":
            _benchmark_camera(args, config)
        elif args.command == "stream":
            _stream(args, config)
        elif args.command == "camera-demo":
            return _camera_demo(args, config)
        else:
            _recognize(args, config)
    except KeyboardInterrupt:
        # Ctrl+C 停止视频流属于正常操作，给出友好提示而不是堆栈。
        print("Stream stopped.", file=sys.stderr)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0
