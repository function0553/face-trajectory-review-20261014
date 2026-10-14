> 2026-10-07 测试清理：本文保留带日期的技术/验证记录；自有测试已移入[统一历史归档](../../workspace-docs/archives/project-cleanup-20261007.zip)，旧测试命令不再作为当前步骤。运行与接手以项目唯一 HANDOFF.md 为准。

# 人脸算法报错定位与处理

本文覆盖当前活动代码：`src/face_engine/` 与 `deployment/edge_face_20260924/api.py`、`core.py`。不覆盖第三方库、测试夹具、历史 evidence 快照、旧 FacePoC 页面后端和 `face-trajectory`。

## 使用方法

1. 从日志或 HTTP 响应复制完整错误文本。
2. 在本文搜索错误文本的固定部分；不要依赖行号，因为增加注释后行号会变化。
3. 先按“处理方法”修正输入、配置或环境，再重试。不要删除校验或修改模型哈希来绕过错误。
4. HTTP 调用方仅对 429、502、503 做有界重试；400、401、403、413、415、422 必须先修正请求。

项目根目录下的标准安装命令：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[runtime,test]"
```

## 1. 配置、依赖和模型启动错误

| 错误原文或固定前缀 | 来源 | 原因 | 处理方法 |
|---|---|---|---|
| `Configuration must be a YAML mapping` | `config.py:load_config` | YAML 顶层不是键值映射，常见原因是缩进错误或写成列表。 | 用 YAML 校验器检查文件；顶层应包含 `device`、`detector`、`recognizer`、`face_index`、`matching`、`video` 六个对象。 |
| `Configuration requires: ...` | `config.py:load_config` | 缺少必需配置段。 | 对照 `configs/default.yaml` 补齐提示中的段，不要在代码里补隐式默认值。 |
| `Require -1 <= low_threshold < high_threshold <= 1` | `config.py`、`image_pipeline.py` | MATCH/REVIEW 阈值顺序或范围错误。 | 确保 `low_threshold` 小于 `high_threshold`，两者都在余弦范围 `[-1,1]`；生产值必须由现场标注数据标定。 |
| `ONNX model not found: ...` | `onnx_backend.py:create_session` | 配置指向的模型文件不存在。 | 放入有使用授权的 ONNX 权重，或修改配置中的 `model_path`；不要复制其他模型后只改文件名。 |
| `Missing ONNX model weights: ...` | `cli.py:_models` | 检测或识别模型缺失。 | 按错误列出的绝对路径放置权重，或用 `--config` 指定正确配置。检测和识别权重必须成套。 |
| `Install onnxruntime-gpu or onnxruntime` | `onnx_backend.py:create_session` | 当前 Python 环境没有 ONNX Runtime。 | 在项目虚拟环境执行标准安装命令；完成后从同一虚拟环境启动，不要混用系统 Python。 |
| `device must be 'cuda' or 'cpu'` | `onnx_backend.py:create_session` | `device` 拼写或大小写不符合约定。 | 配置为小写 `cuda` 或 `cpu`。 |
| `cpu_threads must be positive` | `onnx_backend.py:create_session` | CPU 线程数为 0 或负数。 | 删除该配置让 ORT 自行决定，或设为不超过服务器可用逻辑核数的正整数。 |
| `provider must be cpu or cuda` | `edge_face/core.py:FacePipeline.__init__` | 边缘服务 provider 参数非法。 | 把 `FACE_PROVIDER` 设为小写 `cpu` 或 `cuda`。 |
| `Invalid detection threshold` | `edge_face/core.py:FacePipeline.__init__` | 检测阈值不在 `(0,1]`。 | 使用 `0 < threshold <= 1`；改动后需在真实抓拍图上重新检查漏检与误检。 |
| `Profiling is unavailable for this adapter` | `edge_face/core.py:FacePipeline.__init__` | 向当前适配器传入了 `profile_dir`。 | 去掉 `profile_dir`；性能测量使用项目 benchmark 或 `verify_live.py`。 |
| `Model integrity check failed: ...` | `edge_face/core.py:FacePipeline.__init__` | 模型 SHA-256 与经过验证的权重不一致，可能是文件传错、损坏或被替换。 | 停止启动，重新复制正确权重并核对来源；正式换权重时应同步更新审核过的哈希、重建全部特征并重新标定，不能只改哈希常量。 |
| `Requested CUDAExecutionProvider, but model loaded on another provider` | `edge_face/core.py:FacePipeline.__init__` | 请求 CUDA，但模型实际落到 CPU；常见原因是驱动、CUDA/cuDNN 或 ORT GPU 环境不匹配。 | 检查驱动和 `onnxruntime-gpu` 运行库；若当前只允许 CPU，显式设置 `FACE_PROVIDER=cpu`，不要把静默回退当作 GPU 正常。 |
| `FAISS is unavailable; use create_index for NumPy fallback` | `faiss_index.py:FAISSIndex.__init__` | 直接实例化 FAISSIndex，但环境未安装 FAISS。 | 常规代码改用 `create_index()` 以获得明确降级；要使用 FAISS 时执行标准安装命令后重启环境。 |
| `backend must be 'faiss' or 'numpy'` | `faiss_index.py:create_index` | 索引后端名称非法。 | `face_index.backend` 只能设为 `faiss` 或 `numpy`。 |
| `Set FACE_API_TOKEN_FILE to a private token file` | `edge_face/api.py:create_app` | API 未注入 token，也没有配置 Token 文件。 | 创建仅服务账户可读、内容至少 32 字符的 Token 文件，将 `FACE_API_TOKEN_FILE` 指向它；Java 使用同一值发送 `Authorization: Bearer ...`。 |
| `API token must be at least 32 characters` | `edge_face/api.py:create_app` | Token 为空或过短。 | 更换为至少 32 字符的随机 Token；不要在源码、日志或 URL 中保存 Token。 |

## 2. SCRFD 输入与模型结构错误

| 错误原文或固定前缀 | 来源 | 原因 | 处理方法 |
|---|---|---|---|
| `image must be uint8 BGR [H, W, 3]` | `scrfd.py`、`face_align.py` | 输入不是 OpenCV BGR 三通道 `uint8`，可能是灰度、RGBA、RGB、float 或空对象。 | Java 发送 JPEG/PNG 字节；Python 侧统一用 `cv2.imdecode(..., IMREAD_COLOR)` 解码。直接调用库时转换为连续的 `[H,W,3] uint8 BGR`。 |
| `image dimensions must be positive` | `scrfd.py:prepare_scrfd_input` | 图像宽或高为 0。 | 检查解码是否成功、帧读取返回值和上游裁剪坐标；不要提交空数组。 |
| `input_size must be positive [width, height] divisible by 32` | `scrfd.py:SCRFDDetector.__init__` | SCRFD 输入宽高非正数或不是 32 的倍数。 | 使用已验证的 `[640,640]` 或 `[320,320]`；其他尺寸需同时验证模型输出形状和精度。 |
| `SCRFD model must have 9 or 15 outputs including five keypoints` | `scrfd.py:SCRFDDetector.__init__` | ONNX 不是当前适配器支持的 SCRFD 五点模型，或导出输出节点不同。 | 换成标准 9 输出/15 输出 SCRFD 五点权重；若必须使用其他结构，应新增适配器，不能猜输出顺序。 |
| `Unexpected SCRFD output shape at stride ...` | `scrfd.py:detect_faces` | 输出行数与当前输入尺寸、stride 或 anchor 数不匹配。 | 核对模型导出结构、输入尺寸和输出节点顺序；确认未把其他检测模型误命名为 SCRFD。打印 ONNX 输入输出元数据后再适配。 |
| `small_image_max_side must be positive` | `scrfd.py:SizeAwareSCRFDDetector.__init__` | 小图分支阈值为 0 或负数。 | 使用正整数；当前已验证配置为 `576`，更改后需分别验证 320/640 分支。 |

## 3. 人脸数量、对齐和 ArcFace 错误

| 错误原文或固定前缀 | 来源 | 原因 | 处理方法 |
|---|---|---|---|
| `face_count: Expected exactly one face; detected N` | `edge_face/core.py:embed` | `/v1/extract` 或 `/v1/compare` 的单张输入中不是恰好一张脸。 | 抓拍摄像头应发送单脸抓拍图；N=0 时检查清晰度、姿态和检测阈值，N>1 时收紧摄像头裁剪或选择正确单脸图，不能默认取第一张脸。 |
| `face_too_small: Use a face at least 40 pixels wide and high` | `edge_face/core.py:embed` | 检出人脸短边小于 40 像素。 | 使用更清晰、更近或分辨率更高的抓拍图；不要放大低清小脸冒充有效信息。 |
| `landmarks must contain five finite [x, y] pairs` | `face_align.py` | 关键点不是 `[5,2]`，或包含 NaN/Inf。 | 确认检测器返回左眼、右眼、鼻尖、左嘴角、右嘴角五点；检查模型输出解析和坐标还原。 |
| `cannot align degenerate face landmarks` | `face_align.py` | 五点重合、近似共线或变换缩放退化。 | 丢弃该抓拍；检查检测器输出和极端姿态/遮挡。若大量出现，应诊断 SCRFD 输出而不是降低对齐校验。 |
| `only 112x112 ArcFace alignment is supported` | `face_align.py:align_face` | 调用了非 112 的对齐尺寸。 | 保持 `output_size=112`；若更换不同输入尺寸的识别模型，需实现独立适配器并重建特征库。 |
| `aligned faces must be uint8 BGR [N, 112, 112, 3]` | `arcface.py:prepare_arcface_input` | ArcFace 输入未经过标准五点对齐，或形状/dtype/通道不正确。 | 始终先调用 `align_face()`；检查批次维度和 BGR `uint8`。不要把整帧直接送入 ArcFace。 |
| `ArcFace model input must be [batch, 3, 112, 112]` | `arcface.py:ArcFaceRecognizer.__init__` | ONNX 输入布局或尺寸不兼容。 | 使用 NCHW 112×112 ArcFace ONNX；NHWC 或其他分辨率模型需单独适配预处理。 |
| `ArcFace ONNX model needs a dynamic batch dimension` | `arcface.py:ArcFaceRecognizer.__init__` | 模型 batch 维被固定为 1。 | 重新导出动态 batch ONNX；不要用循环逐脸推理代替，否则多人和录像性能会明显下降。 |
| `batch_size must be positive` | `arcface.py:ArcFaceRecognizer.__init__` | 批大小小于 1。 | 使用正整数；当前默认 32，最终值根据目标 GPU 的延迟、显存和吞吐实测调整。 |
| `ArcFace output must be [N, 512]` | `arcface.py:extract_embeddings` | 模型输出维度不是当前特征协议要求的 512，或输出节点选错。 | 核对 ONNX 输出；SFace 128 维、其他 512 维模型也不能与当前库混用。换模型时建立新的 Feature Space 并重建特征。 |
| `ArcFace returned non-finite or zero embeddings` | `arcface.py:extract_embeddings` | 推理输出含 NaN/Inf 或全零，通常是模型、provider、输入预处理或硬件执行异常。 | 保存非敏感诊断信息（模型 ID、输入形状、provider），换同一图片在 CPU 复测；确认权重、归一化和 CUDA 环境，禁止将该向量写库。 |
| `invalid_feature: ...` | `edge_face/core.py` | 对齐失败或识别器返回异常批形状。 | 查看响应 `message` 的内层错误，按本节相应条目处理；单张坏抓拍可丢弃并记录 source/frame 元数据，持续出现则检查模型和预处理。 |
| `登记时画面中须恰好一张脸...` | `camera_demo.py:_enrollment_sample` | 本机演示登记画面为 0 张或多张脸。 | 保持画面中只有登记人、正对摄像头并改善光照后重试。 |
| `人脸太小，请靠近摄像头后重试` | `camera_demo.py:_enrollment_sample` | 演示登记人脸短边不足 60 像素。 | 靠近摄像头或提高采集分辨率，不要用插值放大替代真实细节。 |

## 4. 特征和向量索引错误

| 错误原文或固定前缀 | 来源 | 原因 | 处理方法 |
|---|---|---|---|
| `embeddings must have shape [N, 512]` | `numpy_index.py:_normalized_matrix` | 查询或入库特征不是二维 512 维矩阵。 | 单条特征先变成 `[1,512]`；核对 `modelId` 和维度，禁止把 128 维 SFace 或其他特征空间写入当前库。 |
| `at least one embedding is required` | `numpy_index.py:_normalized_matrix` | 要求非空的调用传入了 0 条特征。 | 建库前过滤无脸/失败样本；若整批为空则跳过本批，不调用入库。 |
| `embeddings must be finite` | `numpy_index.py:_normalized_matrix` | 特征含 NaN/Inf。 | 丢弃该特征并回查 ArcFace 推理；不要用 0 或截断值替换异常元素。 |
| `zero embeddings are invalid` | `numpy_index.py:_normalized_matrix` | 特征模长为 0，无法归一化。 | 重新提取原图；若持续发生，检查模型输出和预处理，禁止入库。 |
| `subject_ids and embeddings length must match` | `numpy_index.py:add_batch` | ID 数量与特征行数不同。 | 在批量提取后按原始样本索引映射成功结果；失败样本的 ID 和图片应一起移出批次。 |
| `subject_id must be a nonempty string` | `numpy_index.py:add_batch` | 人员 ID 为空或类型不是字符串。 | Java/数据源提供稳定且非空的业务主键；算法层不应使用姓名代替主键。 |
| `duplicate subject_id` | `numpy_index.py:add_batch` | 本批内或与现有库出现重复 ID；当前 Python Gallery 每人只允许一条模板。 | 先按业务主键去重。需要一人多模板时由 Java 向量库使用独立 templateId 管理，或接入支持多模板的 Gallery，不要随机改 ID 掩盖重复。 |
| `KeyError(subject_id)` | `numpy_index.py:remove` | 删除了不存在的人员 ID。 | 先检查人员是否已同步/删除；删除操作应设计为业务侧幂等，算法库调用前判断存在性。 |
| `top_k must be positive` | `numpy_index.py`、`faiss_index.py`、`image_pipeline.py` | Top-K 小于 1。 | 设置为正整数；当前默认 5。库小于 K 时会自动返回全部，无需将 K 调成 0。 |
| `unsupported index schema` | `numpy_index.py:load` | `.npz` 文件 schema 不是当前版本 1。 | 使用匹配版本的软件读取，或从原始登记照片重新提取并建库；不要手工改 schema 数字。 |
| `invalid saved index` | `numpy_index.py:load` | 保存库的 ID/向量数量、ID 唯一性或类型校验失败。 | 视为库文件损坏或来源不可信，从原始数据重建；不要部分加载。 |

## 5. 流水线和视频参数错误

| 错误原文或固定前缀 | 来源 | 原因 | 处理方法 |
|---|---|---|---|
| `source_ids and images length must match` | `image_pipeline.py:recognize_batch` | 帧数与来源标签数不同。 | 为每帧提供一个 sourceId；没有来源时整个参数传 `None`，不要传长度不一致的列表。 |
| `source_id must be a string or None` | `image_pipeline.py:recognize_batch` | sourceId 类型非法。 | 使用 Java 字符串 ID，或不提供；时间戳、对象和数字应放在调用方元数据中。 |
| `recognizer returned incorrect batch shape` | `image_pipeline.py:recognize_batch` | 特征器返回行数与对齐人脸数不一致，或维度不是 512。 | 检查识别模型适配器和批次切分；禁止截断或补零继续检索。 |
| `index returned incorrect batch length` | `image_pipeline.py:recognize_batch` | 索引返回的查询结果数与输入特征数不同。 | 检查自定义索引实现；每条查询必须返回一组候选，空库也要返回等长的空列表。 |
| `frame intervals must be positive` | `video_pipeline.py:recognize_frames` | 检测或识别采样间隔小于 1。 | 两个间隔都设为正整数；直播降低负载时增大 `recognition_interval`。 |
| `max_frames must be positive` | `video_pipeline.py:recognize_frames` | 最大读取帧数为 0 或负数。 | 设为正整数，或传 `None` 表示读到输入结束。 |
| `cache_ttl_frames must be positive` | `video_pipeline.py:recognize_frames` | 缓存有效帧数小于 1。 | 设为正整数；不需要缓存时使用 `cache_enabled: false`。 |
| `max_age_frames must be positive` | `recognition_cache.py:RecognitionCache.__init__` | 完全相同帧缓存的有效期非法。 | 使用正整数，通常取识别间隔的约两倍；该缓存不能替代跟踪。 |

## 6. CLI、摄像头和文件错误

| 错误原文或固定前缀 | 来源 | 原因 | 处理方法 |
|---|---|---|---|
| `Cannot decode image: ...` | `cli.py:_image` | 文件不存在、不是有效图片、读取权限不足或编码损坏。 | 确认路径和扩展名，使用图片查看器验证文件；重新导出为正常 JPEG/PNG。中文路径由当前读取方式支持。 |
| `CSV requires subject_id,image_path columns` | `cli.py:_enroll` | 建库 CSV 缺少固定表头。 | 第一行使用 `subject_id,image_path`，保存为 UTF-8/UTF-8 BOM。 |
| `Missing subject_id on line N` | `cli.py:_enroll` | 指定行人员主键为空。 | 回到 CSV 第 N 行补齐稳定业务 ID；不要用行号作为长期主键。 |
| `Expected exactly one face on line N; found M` | `cli.py:_enroll` | 登记照片中不是恰好一张脸。 | M=0 时换清晰正脸照；M>1 时裁剪为单人照片。不要自动取检测分最高的人脸。 |
| `Cannot open video source` | `cli.py:_stream/_benchmark_camera` | RTSP/视频文件/USB 摄像头无法打开。 | 检查地址、文件路径、摄像头编号、网络和占用情况；Windows USB 可尝试 `--directshow`。凭据由部署环境安全提供，不写入日志。 |
| `frames must be positive and warmup must be nonnegative` | `cli.py:_benchmark_camera` | benchmark 帧数不正或预热帧数为负。 | `--frames` 设正整数，`--warmup` 设 0 或正整数。 |
| `Video source ended during warmup` | `cli.py:_benchmark_camera` | 输入在预热阶段就结束，常见于损坏/过短视频或断流。 | 换完整录像，减少 `--warmup`，或先解决 RTSP 断流。 |
| `No frames captured` | `cli.py:_benchmark_camera` | 视频源虽然打开，但未成功读到任何帧。 | 用播放器验证源；检查解码器、网络、摄像头权限和占用，不能据此判断算法故障。 |
| `sample-every must be positive` | `cli.py:main` | 完整 benchmark 抽帧间隔小于 1。 | `--sample-every` 设为正整数。 |
| `subject_id must not be empty` | `camera_demo.py:run_camera_demo` | 演示登记 ID 为空。 | 提供非空 `--subject-id`。正式系统使用业务主键。 |
| `无法打开本机摄像头 N` | `camera_demo.py:run_camera_demo` | USB 摄像头编号错误、被占用或无权限。 | 关闭占用程序，尝试 0/1 等正确编号，并检查 Windows 相机权限。 |
| `无法编码诊断帧` | `camera_demo.py:save_diagnostic` | OpenCV 无法把当前帧编码为 PNG。 | 检查帧是否有效、OpenCV 安装和目标内存；失败时不要写空文件。 |
| CLI 退出码 `2` / `error: ...` | `cli.py:main` | 上述可预期配置、输入或运行错误被统一转换为命令行错误。 | 读取 `error:` 后的原始文本，在本文查找对应条目；退出码 2 不代表 Python 崩溃。 |
| `SystemExit(main())` | `__main__.py` | 正常把 CLI 返回码交给操作系统，不是算法错误。 | 返回码 0 表示成功；非 0 时查看此前输出的具体错误。 |

## 7. HTTP API 状态码与调用方处理

| 状态/响应文本 | 来源 | 原因 | Java/调用方处理 |
|---|---|---|---|
| `401 Please sign in... or supply a valid API token` | `api.py:authenticate` | 缺少或使用了错误 Bearer Token。 | 修正 `Authorization: Bearer <token>`；不要自动无限重试，也不要把 Token 打进日志。 |
| `403 Use the algorithm page` | `api.py:authenticate` | 浏览器会话发起写请求但缺少同源页面头。 | Java 不应走浏览器 Cookie，改用 Bearer Token；本机页面请求由原页面代码发送。 |
| `415 Use application/json` | `api.py:get_payload` | Content-Type 不是 `application/json`。 | 设置正确请求头，并发送 JSON；图片字段是 Base64 字符串，不是 multipart。 |
| `413 Request body exceeds 15 MiB` | `api.py:get_payload` | 单张大图或帧批 Base64 后超过 15 MiB。 | 抓拍图保持合理分辨率/JPEG 质量；录像减小每批帧数。不要拆断单张 Base64，应该拆请求。 |
| `400 Invalid JSON` | `api.py:get_payload` | JSON 语法或 UTF-8 编码错误。 | 使用标准 JSON 序列化器，不手工拼接；检查 Base64 是否被错误换行或截断。 |
| `422 JSON body must be an object` | `api.py:get_payload` | 顶层发送了数组、字符串或 null。 | 顶层必须是对象，例如 `{"image":"..."}` 或 `{"frames":[...]}`。 |
| `429 An inference request is already running` | `api.py:compute` | 当前唯一推理槽正在执行。 | 抓拍图片进入有界队列后短暂退避；实时流丢弃过期帧，只保留最新帧；不要无界并发重试。 |
| `422 frames must contain 1 to 8 items` | `api.py:analyze_frames` | `frames` 缺失、不是数组、为空或超过 8 帧。 | 实时流每批 1～4 帧，录像每批不超过 8 帧。 |
| `422 each frame must be an object` | `api.py:analyze_frames` | frames 中元素不是对象。 | 每项使用 `{"sourceId":...,"frameId":...,"capturedAt":...,"image":"..."}`。 |
| `422 threshold must be numeric or null` | `api.py:compare` | threshold 是布尔值、字符串或对象。 | 发送 JSON 数字或省略/null；不要把配置文本未经解析直接转发。 |
| `503 Hikvision device is not configured` | `api.py:hik_*` | 未配置可选海康代理。 | 普通 SCRFD/ArcFace 接口不受影响；确需设备接口时设置并验证 `HIKVISION_CONFIG_FILE`。 |
| `422 {"error":"face_count|face_too_small|invalid_feature",...}` | `api.py:face_error` | 输入人脸或特征生成失败。 | 按本文第 3 节对应 error code 处理；修正图片后再提交。 |
| `502 {"error":"device_*",...}` | `api.py:face_error` | 外部设备代理调用失败。 | 记录设备错误码，检查设备网络/状态；使用指数退避做有限重试，不能阻塞本地算法接口。 |

### HTTP 重试矩阵

| 状态 | 是否自动重试 | 策略 |
|---|---|---|
| 400/401/403/413/415/422 | 否 | 先修正请求、认证、图片或配置。原样重试只会重复失败。 |
| 429 | 是，有限 | 抓拍任务短退避；实时监控丢旧保新；设置队列上限。 |
| 502/503 | 是，有限 | 外部设备恢复前指数退避；达到上限后告警并降级。 |
| 500 | 否，先调查 | 保存请求 ID、模型 ID、provider 和堆栈，修复服务端问题后再重试。 |

## 8. 这些提示不是错误

| 日志 | 含义与处理 |
|---|---|
| `CUDA provider unavailable; falling back to ONNX Runtime CPU` | CUDA provider 不可用，程序明确降级 CPU。功能可运行但性能不同；要求 GPU 时应修环境。 |
| `CUDA provider initialization failed; falling back...` | ORT 看到了 CUDA provider，但依赖加载或初始化失败；检查驱动/CUDA/cuDNN。 |
| `FAISS GPU unavailable; falling back to FAISS CPU` | FAISS GPU 不可用，检索继续在 CPU；需用目标规模实测延迟。 |
| `...falling back to NumPy CPU` | FAISS 包不可用，使用 NumPy 精确检索；小规模可用，4～5 万库需做容量验证。 |
| `No subject index supplied; detected faces will be UNKNOWN.` | 未加载人员库，适合只验证检测；所有人脸返回 UNKNOWN 是预期行为。 |
| `Stream stopped.` | 用户按 Ctrl+C 正常停止流，不需要修复。 |

## 9. 快速定位顺序

1. **服务起不来**：先看 Token、模型路径、模型 SHA、provider 和依赖。
2. **HTTP 不通**：按状态码检查认证、Content-Type、请求大小和 JSON。
3. **抓拍图失败**：优先看 `face_count`、`face_too_small`、关键点和图片解码。
4. **有特征但检索异常**：核对 `modelId`、512 维、有限值、归一化及特征库版本。
5. **直播延迟累积**：统计 429 和队列长度；实时流采用丢旧保新，不增加无界工作线程。
6. **分数不稳定或误识**：这不是程序异常；使用目标相机带身份标注样本联合标定阈值、Top1-Top2 margin 和 REVIEW 区间。
