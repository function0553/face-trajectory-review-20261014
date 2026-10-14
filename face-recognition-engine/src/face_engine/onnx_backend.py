"""【ONNX Runtime 后端选择】统一创建推理 session，并把"实际用了什么后端"显式打印出来。

为什么要把后端选择单独抽出来：
    检测器和特征提取器都需要建 session，如果各写一份，很容易出现
    "一个在 GPU 上跑、另一个悄悄降级到 CPU"却没人发现的情况。
    这里统一处理，并且强制打印结果，方便一眼看出真实运行环境。

设计原则：降级可以，静默降级不行。
    CUDA 不可用时自动回退 CPU，但一定会打印提示，
    绝不让使用者误以为自己跑在 GPU 上。
"""

from pathlib import Path


def create_session(model_path: str | Path, device: str = "cuda", cpu_threads: int | None = None):
    """创建一个 ONNX Runtime 推理 session。

    参数：
        model_path: ONNX 模型文件路径，不存在直接抛 FileNotFoundError。
        device: "cuda" 表示优先尝试 GPU；"cpu" 表示直接用 CPU。
        cpu_threads: 限制 CPU 推理时的线程数；None 表示交给 ONNX Runtime 自行决定。
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
    if device not in ("cuda", "cpu"):
        raise ValueError("device must be 'cuda' or 'cpu'")
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
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:
        if device == "cuda":
            print("CUDA provider unavailable; falling back to ONNX Runtime CPU")
        providers = ["CPUExecutionProvider"]

    options = ort.SessionOptions()
    if cpu_threads is not None:
        options.intra_op_num_threads = cpu_threads
    session = ort.InferenceSession(str(path), sess_options=options, providers=providers)

    # 以实际生效的第一个 provider 为准，而不是以"请求了什么"为准。
    active = session.get_providers()[0]
    backend = "CUDA" if active == "CUDAExecutionProvider" else "CPU"
    # 请求了 CUDA、provider 也在列表里，但最终没用上 → 说明初始化失败，明确告知。
    if device == "cuda" and backend != "CUDA" and "CUDAExecutionProvider" in available:
        print("CUDA provider initialization failed; falling back to ONNX Runtime CPU")
    print(f"ONNX Runtime Backend: {backend}")
    return session, backend
