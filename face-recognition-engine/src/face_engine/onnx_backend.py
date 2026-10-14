"""【ONNX Runtime 后端选择】统一创建推理 session，并把"实际用了什么后端"显式打印出来。

为什么要把后端选择单独抽出来：
    检测器和特征提取器都需要建 session，如果各写一份，很容易出现
    "一个在 GPU 上跑、另一个悄悄降级到 CPU"却没人发现的情况。
    这里统一处理，并且强制打印结果，方便一眼看出真实运行环境。

设计原则：降级可以，静默降级不行。
    CUDA 不可用时自动回退 CPU，但一定会打印提示，
    绝不让使用者误以为自己跑在 GPU 上。
"""

import os
from pathlib import Path


def _resolve_device(device: str) -> tuple[str, int | None]:
    """Normalize a requested device and resolve an optional CUDA device id.

    ``cuda`` keeps ONNX Runtime's default device selection unless
    ``FACE_ENGINE_CUDA_DEVICE_ID`` is set.  An explicit ``cuda:<id>`` always
    wins over the environment variable.
    """
    if not isinstance(device, str):
        raise ValueError("device must be 'cuda', 'cuda:<id>', or 'cpu'")

    if device == "cpu":
        return device, None

    if device == "cuda":
        env_device_id = os.getenv("FACE_ENGINE_CUDA_DEVICE_ID")
        if env_device_id is None or not env_device_id.strip():
            return device, None
        return device, _parse_device_id(env_device_id, "FACE_ENGINE_CUDA_DEVICE_ID")

    if device.startswith("cuda:"):
        return "cuda", _parse_device_id(device[5:], "device")

    raise ValueError("device must be 'cuda', 'cuda:<id>', or 'cpu'")


def _parse_device_id(raw_device_id: str, source: str) -> int:
    """Parse a non-negative CUDA device id from a config or environment value."""
    try:
        device_id = int(raw_device_id.strip(), 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} CUDA device id must be a non-negative integer") from exc
    if device_id < 0:
        raise ValueError(f"{source} CUDA device id must be a non-negative integer")
    return device_id


def create_session(model_path: str | Path, device: str = "cuda", cpu_threads: int | None = None):
    """创建一个 ONNX Runtime 推理 session。

    参数：
        model_path: ONNX 模型文件路径，不存在直接抛 FileNotFoundError。
        device: ``"cuda"``、``"cuda:<id>`` 或 ``"cpu"``。
            ``FACE_ENGINE_CUDA_DEVICE_ID`` 为 ``"cuda"`` 提供设备编号兜底。
        cpu_threads: 限制 ORT CPU 线程数；未提供时默认使用 2 个 intra-op 线程。
    返回：
        (session, backend)：session 是推理会话，backend 是 "CUDA" 或 "CPU" 字符串。
    """
    path = Path(model_path)
    if not path.is_file():
        raise FileNotFoundError(f"ONNX model not found: {path}")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError("Install onnxruntime-gpu or onnxruntime") from exc
    available = ort.get_available_providers()
    device, device_id = _resolve_device(device)
    if cpu_threads is not None and cpu_threads <= 0:
        raise ValueError("cpu_threads must be positive")

    if device == "cuda" and "CUDAExecutionProvider" in available:
        # Loads CUDA/cuDNN DLLs from the venv's NVIDIA packages on Windows.
        # A listed provider alone does not prove its dependent DLLs can load.
        # provider 出现在列表里，只说明包被装上了，不代表 CUDA/cuDNN 动态库真能加载。
        # Windows 上需要显式预加载这些 DLL，否则会在建 session 时才失败。
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
        # 同时给出 CPU 作为兜底 provider，GPU 初始化失败时 ONNX Runtime 会自己接上。
        if device_id is not None:
            providers = [
                ("CUDAExecutionProvider", {"device_id": str(device_id)}),
                "CPUExecutionProvider",
            ]
        else:
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:
        if device == "cuda":
            print("CUDA provider unavailable; falling back to ONNX Runtime CPU")
        providers = ["CPUExecutionProvider"]

    options = ort.SessionOptions()
    options.intra_op_num_threads = cpu_threads if cpu_threads is not None else 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(path), sess_options=options, providers=providers)

    # 以实际生效的第一个 provider 为准，而不是以"请求了什么"为准。
    active = session.get_providers()[0]
    backend = "CUDA" if active == "CUDAExecutionProvider" else "CPU"
    # 请求了 CUDA、provider 也在列表里，但最终没用上 → 说明初始化失败，明确告知。
    if device == "cuda" and backend != "CUDA" and "CUDAExecutionProvider" in available:
        print("CUDA provider initialization failed; falling back to ONNX Runtime CPU")
    if backend == "CUDA":
        resolved_device_id = "default" if device_id is None else str(device_id)
        print(f"ONNX Runtime Backend: CUDA (device_id={resolved_device_id})")
    else:
        print("ONNX Runtime Backend: CPU")
    return session, backend
