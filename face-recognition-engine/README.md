# 场馆多人脸识别算法引擎

2026-10-10 [重点/普通/匿名监控运行入口](docs/monitoring-runtime.md)：自拉视频、海康抓拍、低清主码补图、统一质量与特征、三类公平优先队列，实际比对及轨迹交P2 0.13同事务完成。本轮P1仅修改活动字段与兼容/维护握手；P2 C++/Go提供底层多活动隔离、后端签名授权与168小时清理。真实前后端/生产及600路容量待验收，模型/图库状态以[唯一交接](HANDOFF.md)为准。

当前新增P1运行入口仍是Python；P2核心为C++、接口为Go。用户要求正式链路沿用C++/Go，P1运行层迁移尚未实施，不能把当前原型等同于完整C++/Go交付。业务服务器只读核查和迁移范围见交接第10节。

2026-10-10 用户确认正式模型由Codex评估成熟预训练方案并验证，效果不足再考虑微调。已完成本机CPU/CUDA阶段计时、C++调用数值对照及YuNet/SFace首轮筛查；候选未获正式批准，详见[选型与性能证据](evidence/2026-10-10-inference-performance/REPORT.md)及交接第11节。

2026-10-05：新增 `face_engine.trajectory_bridge`，复用检测/对齐/特征模块并以 SQLite outbox 向 P2 投递不可变抓拍事件。未知采集时间不补成 UTC，重试去重与容量保护已实现。见 [P2 接入说明](../face-trajectory/docs/runtime-and-geometry.md) 与 [本次真实图片验证](../face-trajectory/evidence/2026-10-05-repair/real_bridge.json)。

本项目是 **P1 人脸识别引擎**，负责检测、对齐、特征和候选匹配。独立交接见 [HANDOFF.md](HANDOFF.md)，资料见 [文档目录](docs/README.md)，三个项目的关系见 [工作区总览](../README.md)。C++ 轨迹与时间轴属于独立的 P2。

Python 3.10+ 算法库。输入 OpenCV BGR 图片，检测全部人脸、五点对齐、分批提取 512 维特征、一次批量搜索约 50,000 个候选，输出每张脸的 TopK、相似度与 MATCH / REVIEW / UNKNOWN。人员标识统一为外部提供的 `subject_id`，不含姓名或票务等业务字段。

本 Python 核心库处理人脸输入；新轨迹工程位于 [face-trajectory](../face-trajectory/)，实现进度以交接文档为准。

## 安装与模型

在本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[runtime]"
```

## 报错排查

代码中的显式异常、HTTP 状态码、触发原因、修复步骤和重试策略统一见
[人脸算法报错定位与处理](docs/ERROR_TROUBLESHOOTING.md)。请按错误原文检索，
不要依赖会随代码调整变化的行号，也不要通过删除校验绕过模型、特征或输入错误。

正式配置尚无已批准权重；`models/demo/`和`models/candidates/`分别保留研究演示和隔离评估模型。当前生产适配合同要求有使用授权的 **SCRFD 五点关键点 ONNX** 与 **ArcFace 512 维 ONNX**，通过 `configs/default.yaml` 指定路径。当前 SCRFD 适配标准 9 输出（三层）或 15 输出（五层）结构；ArcFace 需要动态 batch 输入 `[N,3,112,112]`、输出 `[N,512]`。不符合这些结构的模型会明确报错，需要添加相应适配器。候选SFace输出128D，不能直接替换；生产使用前必须验证模型版本、预处理方式、输出定义及使用授权，并用同一识别模型重建整个特征库。

CUDA 不可用时 ONNX Runtime 报告 CPU 降级；FAISS GPU 不可用时报告 FAISS CPU 降级；FAISS 不可用时 `create_index` 报告 NumPy CPU 降级。运行时打印各组件实际后端。NumPy 搜索主要供开发测试。

## 使用

统一主入口是 `python -m face_engine`。场馆摄像头实时帧的演示命令如下；持续读取、按配置的识别间隔输出 JSON Lines，按 `Ctrl+C` 停止：

```powershell
.\.venv\Scripts\python.exe -m face_engine stream 'rtsp://host/stream' subjects.npz --source-id CAMERA_TEST_01
.\.venv\Scripts\python.exe -m face_engine stream 0 subjects.npz --camera --source-id USB_TEST_01
```

只想验证摄像头的人脸检测、暂时没有人员库时，可省略 `subjects.npz`；所有检测结果会标为 UNKNOWN。也可用 `--max-frames 300` 限制读取帧数。此入口只演示单路视频接入，不包含断线重连或多摄像头调度。算法核心只处理已解码帧，未来视频接入层可直接调用 `recognize_image` / `recognize_batch`。

正式配置 `configs/default.yaml` 的模型尚未批准。由Codex评估成熟预训练权重，通过授权、兼容性和目标场景验证后再接入，不要求用户先自行训练。以上命令需有符合当前合同的权重才能运行；可先执行 `python -B -m face_engine --help` 核查命令入口。真实识别效果需用授权模型和已核验样本验收。测试源码及模拟 selftest 已按要求归档移出。

五点对齐现按 ArcFace 标准模板对全部五点拟合相似变换。对齐算法变更后，任何用旧版本生成的持久化特征库都必须从原始登记照片重新生成；不能混用新旧特征。匹配阈值仍需用目标场景的同人、异人样本校准。

### 本机摄像头临时识别演示

在本机运行以下命令，会打开可见窗口。面向摄像头，点击“登记本人（1 张）”，保持画面中只有自己；登记成功后离开画面再回来，观察“匹配 / 待复核 / 未知”和相似度。此处只登记一张正面帧，以模拟正式场景每人只有一张登记照。Esc 关闭窗口。预览水平镜像，算法输入仍为原始帧。登记特征只存在进程内存中，关闭后消失；不会保存摄像头画面。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[preview]"
.\.venv\Scripts\python.exe -m face_engine camera-demo --camera 0
```

此演示使用 `configs/demo.yaml` 指向的 [InsightFace 官方临时模型](models/demo/README.md)，仅限本机非商业研究验证；`configs/default.yaml` 仍指向尚未提供的正式模型。两份配置现在都对最长边不超过 576 像素的小图使用 320×320 SCRFD 输入，对大图保留 640×640；照片和抓拍（不含 `sub`/`main` 视频帧）的大图在 640 输入检测到 0 张脸时，再用 320 输入复检一次，以检出几乎占满画面的人脸特写；两种输入各自加载检测会话，启动时会多占用一份检测模型显存。该尺寸选择来自服务器抓拍小图测试，不能当作所有摄像头场景的最优设置。演示配置优先使用 CUDA；在 Windows 上可在虚拟环境安装 `onnxruntime-gpu[cuda,cudnn]` 提供运行库，界面会显示检测和特征模型实际使用的后端。模型及演示阈值不能直接用于场馆安保，也不能从只登记一人的测试推断误认率。摄像头界面用一个后台推理任务处理最新帧，避免推理时冻结预览和积压旧帧；显示层用短时帧间位移补偿让框跟随画面，身份判定仍只来自模型。算法核心仍与摄像头解耦。

如需复现检测失败，可额外传 `--diagnostic-dir <本机临时目录>`。窗口会显示“保存基准帧”和“标记失败帧”按钮，仅在主动点击时保存当前原始帧；诊断后请删除该目录。演示默认使用精度较高的 `det_10g.onnx`：本机低头样本显示轻量版漏检，切换后可以检测到这些帧，但严重低头或遮挡仍可能得到 REVIEW / UNKNOWN。

`benchmarks/diagnose_face_pair.py` 默认请求 CUDA，并输出检测和特征模型实际启用的后端；需要对照 CPU 时可传 `--device cpu`。这个脚本用于比较两张图片的特征相似度，命令的总运行时间还包含模型加载和图片解码，不能当作单次推理耗时。

在正式权重选定之前，可以直接用本机 USB 摄像头测试采集和 SCRFD 输入预处理耗时；这不会保存画面，也不会输出人脸身份：

```powershell
.\.venv\Scripts\python.exe -m face_engine benchmark-camera 0 --camera --directshow --frames 90
```

需要**看见实时画面**时，安装可选预览依赖并运行 `python examples/preview_camera.py --camera 0`。预览窗口可按 Esc 关闭；它只显示画面，不运行识别模型，也不保存图片。

输出分别报告读帧耗时（包含等待下一帧）、输入预处理耗时和观测帧率，**不包含检测模型或 ArcFace 推理**，不能当作端到端识别性能。当前模型的 ONNX 接口要求见 `models/README.md`。

符合当前合同的模型准备好后，可在同一摄像头上运行真实阶段计时；`--index` 应指向由同一识别模型建立的人脸库：

```powershell
.\.venv\Scripts\python.exe -m face_engine benchmark-camera 0 --camera --directshow --full --index subjects.npz --frames 300 --sample-every 15
```

这会分别测量检测（含其预处理）、对齐、ArcFace Batch、检索及采样帧总耗时。正式模型尚未批准，因此尚无正式模型在目标服务器上的阶段测量值；演示/候选模型的本机数据不能代替服务器测量。

在项目根目录执行。先准备 UTF-8 CSV：

```csv
subject_id,image_path
FACE_000001,photos/one.jpg
FACE_000002,photos/two.jpg
```

每张登记照片必须检测到恰好一张脸；存在多张脸或没有脸时建库会报错。建库只保存 `subject_id` 和归一化 embedding。

建库是候选人脸库准备步骤，不要求摄像头画面由人工逐张提交。统一主入口建库命令：

```powershell
.\.venv\Scripts\python.exe -m face_engine enroll subjects.csv subjects.npz
```

```powershell
.\.venv\Scripts\python.exe examples/enroll_faces.py subjects.csv subjects.npz
.\.venv\Scripts\python.exe examples/compare_two_faces.py first.jpg second.jpg
.\.venv\Scripts\python.exe examples/recognize_image.py camera.jpg subjects.npz
.\.venv\Scripts\python.exe examples/recognize_video.py clip.mp4 subjects.npz
.\.venv\Scripts\python.exe examples/recognize_video.py 0 subjects.npz --camera
.\.venv\Scripts\python.exe examples/recognize_video.py 'rtsp://host/stream' subjects.npz
```

`recognize_image` 对空画面返回 `[]`。有脸但索引为空时返回 UNKNOWN、`similarity: null`、`top_k: []`。低于低阈值时 `subject_id: null`；REVIEW 保留候选 `subject_id`，须人工复核。输出包含检测框、五点关键点、检测分数和完整 TopK。

`FaceRecognitionEngine.recognize_image(image, source_id=None)` 处理单张帧；`recognize_batch(images, source_ids=None)` 接收多张帧，逐帧检测后汇总全部人脸，再共用一次批量特征调用和一次批量搜索。它不连接或管理摄像头；一套模型实例可处理不同来源的帧。每张脸结果都带可选 `source_id`，算法只透传这个标签。

视频按 `detection_interval` 与 `recognition_interval` 的共同采样帧输出 JSON Lines；未采样帧无识别结果。MP4/USB/RTSP 解码仅在 `examples/recognize_video.py` Demo 中；算法模块的 `recognize_frames` 只接收已解码帧。可选缓存只复用字节完全相同、且未过期的帧，不推断跨帧身份。默认关闭。

## 阈值与评估

配置中的低/高阈值 **仅为开发占位值**，绝不能作为正式场馆识别阈值。正式阈值必须用真实票务照片与海康摄像头抓拍人脸建立同人/不同人测试集后重新确定，并至少报告 FAR、FRR、ROC、TAR。还须针对光照、距离、角度、遮挡及多人画面评估误识别。当前没有真实数据，因而无法声称识别准确率或生产吞吐量。

## Benchmark

```powershell
.\.venv\Scripts\python.exe benchmarks/benchmark_index.py --cpu
.\.venv\Scripts\python.exe benchmarks/benchmark_recognition.py
.\.venv\Scripts\python.exe benchmarks/benchmark_pipeline.py camera.jpg subjects.npz
```

索引基准构造 50,000×512 随机 L2 归一化向量，测量 batch=1/10/20/50/100、TopK=5 的实际延迟与 QPS。识别器与 Pipeline 基准需要真实模型权重，Pipeline 还需要一张至少含一张可检测人脸的图片和索引。Pipeline 基准针对 1/5/10/20/50 张脸分别输出检测、对齐、ArcFace Batch、搜索、总延迟与 faces/sec；**检测始终运行在原始输入图片上**，其后通过重复已检测人脸模拟不同人数，输出 `source_detected_faces` 以明确检测负载。若需评价真实 50 人同框检测延迟，必须使用真实 50 人画面。基准脚本仅输出当前机器的测量值，不预填性能数字，也不能据此推算服务器支持的摄像头路数。容量评估须结合实际分辨率、FPS、场景人数、检测频率、模型、GPU 和视频编码压测。

## 验证边界

单元测试覆盖空/单/多人检测、对齐、embedding 维度与归一化、余弦搜索、TopK、跨帧批量查询、保存读取、状态分级以及 50,000 人模拟库。本机临时研究模型已验证 SCRFD/ArcFace CUDA 推理和摄像头演示，但正式模型、真实场馆视频、服务器虹软接口、FAISS GPU、场馆准确率与多路容量仍未验证。模型与索引接口允许将来替换为 TensorRT/C++ 实现；当前 ONNX Runtime 使用普通输入输出绑定，未来可在适配器内引入 I/O Binding 或 CUDA Buffer，减少 CPU/GPU 数据搬运，而无需更改上层 `recognize_image` / `recognize_batch` 定义。
