# 安保人脸与跨摄像头系统：开源项目调研

核查日期：2026-10-12。仅查看官方仓库、官方源码和官方文档；未安装依赖、下载权重、连接设备或访问生产。以下适配判断是对当前 P1 Python 推理、C++ 算法与 Go 服务分层的工程分析，尚未在本地跑这些候选项目。

## 人脸检测、识别与部署组件

| 项目 | 可复用部分 | 与当前系统的接法 | 许可与限制 |
| --- | --- | --- | --- |
| [InsightFace](https://github.com/deepinsight/insightface) | SCRFD 检测、ArcFace 识别、对齐、训练与评估工具。官方 SCRFD 提供 ONNX 转换和 ONNX Runtime 推理。[SCRFD 文档](https://github.com/deepinsight/insightface/tree/master/detection/scrfd)、[识别模块](https://github.com/deepinsight/insightface/tree/master/recognition) | 与已有 SCRFD/ArcFace 方向接近，优先参考预处理、特征归一化和评估方法；Python 可保留既有 P1 接口，C++ 使用自己的 ONNX 推理适配层，Go 继续负责业务服务。此处是适配建议，不代表已证明现有权重与官方包相同。 | 代码 MIT；官方训练数据及其预训练模型只供非商业研究，自动下载也受限制。商用模型须另取得许可，不能从代码 MIT 推导权重可商用。[官方许可说明](https://github.com/deepinsight/insightface#license)、[模型许可与自有许可模型接入](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md#model-licenses) |
| [OpenCV Zoo：YuNet + SFace](https://github.com/opencv/opencv_zoo) | YuNet 轻量人脸检测、五点关键点；SFace 人脸特征与比较；两者均提供 ONNX 模型及 Python/C++ 示例。[YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)、[SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | 适合作为 CPU 离线部署候选和独立对照基线。原生 C++ 示例使用 OpenCV 接口，可在 P1 算法适配层接入；更换识别模型后需重建特征库、重新校准阈值，不能直接沿用 ArcFace 向量和阈值。后者是工程判断。 | Zoo 根目录 Apache 2.0，但各模型分别授权：YuNet 目录声明所有文件 MIT；SFace 目录声明所有文件 Apache 2.0。应保留所选目录的 LICENSE 与模型版本。[YuNet LICENSE](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE)、[SFace LICENSE](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE) |
| [SeetaFace6 开放版](https://github.com/SeetaFace6Open/index) | C++ 人脸检测、关键点、识别、质量评估、活体检测等 SDK；Windows/Linux 构建脚本和自有 TenniS 推理引擎。[开放版源码说明](https://github.com/SeetaFace6Open/index#开源模块)、[构建说明](https://github.com/SeetaFace6Open/index#编译) | 可评估需要 C++ CPU SDK、质量筛选或活体组件的场景；官方模型为 `.csta`，与当前 ONNX 管线不同，接入需新增适配与特征库版本。不是直接替换 SCRFD/ArcFace 的同格式模型。[官方模型列表](https://github.com/SeetaFace6Open/index#下载地址) | 仓库 LICENSE 是两条款 BSD 形式；官方 README 明确开放版可免费用于商业和个人用途。模型下载包尚未下载检查，不能将仓库声明扩展为未知第三方数据或其他版本的许可。[LICENSE](https://github.com/SeetaFace6Open/index/blob/master/LICENSE)、[官方用途声明](https://github.com/SeetaFace6Open/index#联系我们) |

### 成熟度和维护状态

- InsightFace 核心 SCRFD/ArcFace 是长期公开的算法组件；当前 README 记录 2026-10-03 的 PersonAnalysis 2.1 更新，增加人脸与人体 ReID。距检索仅 9 天，只列观察项，优先评估已有 SCRFD/ArcFace。Python 文档明确它的输出描述当前帧，API 不创建轨迹或历史数据库，因此它仍需外接我们的跨摄像头时空、活动、留存和证据逻辑；新功能不因处在成熟仓库就自动具备现场验收证据。[官方更新](https://github.com/deepinsight/insightface#changelogs)、[PersonAnalysis 范围](https://github.com/deepinsight/insightface/blob/master/python-package/README.md#personanalysis)
- OpenCV Zoo 当前 README 声明项目模型和在线示例迁到官方 Hugging Face。不能仅根据 GitHub 提交频率判断停止维护；后续取模型应同时核对官方迁移入口。YuNet 文档区分 OpenCV 4.x 的 `2023mar` 模型与面向 OpenCV 5.x ONNX Runtime 可变输入的 `2026may` 版本，需按实际运行库选择，避免随意替换。[迁移声明](https://github.com/opencv/opencv_zoo#readme)、[YuNet 兼容说明](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/README.md)
- SeetaFace6 有完整开放 SDK 和官方商业用途声明，但本次核查的两个官方入口 GitHub 最近 push 都停在 2021-11-29。适合作为已有 C++ SDK 方案的备选，对当前编译器与目标设备的兼容性需离线实测；不将它称为近期活跃项目。[发布入口](https://github.com/seetafaceengine/SeetaFace6#源码发布)、[源码入口](https://github.com/SeetaFace6Open/index)

### 本次 API 核查记录

以下是 2026-10-12 对 GitHub REST API 的只读查询结果；`pushed_at` 是仓库 push 时间，不等同于某个目录最后修改时间，也不是模型发布日期。未采用 `updated_at` 推断代码活跃度。

| 官方仓库 | archived | pushed_at (UTC) | 官方 API |
| --- | --- | --- | --- |
| deepinsight/insightface | false | 2026-10-04 12:46:41 | [repository API](https://api.github.com/repos/deepinsight/insightface) |
| opencv/opencv_zoo | false | 2026-05-28 14:46:51 | [repository API](https://api.github.com/repos/opencv/opencv_zoo) |
| SeetaFace6Open/index | false | 2021-11-29 08:10:15 | [repository API](https://api.github.com/repos/SeetaFace6Open/index) |
| seetafaceengine/SeetaFace6 | false | 2021-11-29 08:08:43 | [repository API](https://api.github.com/repos/seetafaceengine/SeetaFace6) |

### 复用顺序建议

先核实现有 SCRFD/ArcFace 权重来源与许可，复用 InsightFace 的训练、评估及预处理经验。需要轻量 CPU 对照时，另建隔离评估分支测试 YuNet/SFace。需要原生 C++ SDK 和质量/活体组件时，再评估 SeetaFace6。这三者都不能替代本项目已经约束的活动隔离、跨摄像头时钟判断、拓扑候选、168 小时留存、可靠投递及维护恢复；这是结合本项目范围的判断。

### 调研方法与踩坑

查官方 README 后逐个核对模型子目录 LICENSE，避免把根目录代码许可当成权重授权；查源码/接口声明确认项目到底输出单帧匹配、单路轨迹还是跨摄像头关联。浏览工具无法直接读取 GitHub API 时，使用 PowerShell `Invoke-RestMethod` 只读查询仓库元数据，保留上表日期。未引用二手博客或未验证的速度、容量宣传。

## 单摄像头跟踪、跨摄像头关联与多路视频

| 项目 | 实际输入与输出 | 可复用部分与适配判断 | 许可/维护边界 |
| --- | --- | --- | --- |
| [ByteTrack](https://github.com/FoundationVision/ByteTrack) | 单摄像头连续帧的检测框/分数，输出轨迹ID | 低分检测框续接和轨迹关联，可用于同镜去重、抓拍选帧；不会自动证明跨镜同一人。官方有 [TensorRT C++](https://github.com/FoundationVision/ByteTrack/tree/main/deploy/TensorRT/cpp) 与 ncnn 部署实现 | 代码 [MIT](https://github.com/FoundationVision/ByteTrack/blob/main/LICENSE)；检测模型与训练数据另查。经典稳定基线，默认分支最新提交2022-12-11；README中旧ifzhang地址不能当当前维护主体。[提交API](https://api.github.com/repos/FoundationVision/ByteTrack/commits/main) |
| [FastReID](https://github.com/JDAI-CV/fast-reid) | 人体裁剪图，输出外观特征用于跨镜相似检索 | 有全身视频时可为脸不可用的事件提供候选关联信号，再结合人脸和可信时空判断。其 [ONNX/TensorRT部署工具](https://github.com/JDAI-CV/fast-reid/tree/master/tools/deploy)可作为C++推理接入基础，后者是工程适配判断 | 代码 [Apache-2.0](https://github.com/JDAI-CV/fast-reid/blob/master/LICENSE)，各权重/训练数据分别核对。默认分支最新提交2024-07-15，不能称持续活跃维护。[提交API](https://api.github.com/repos/JDAI-CV/fast-reid/commits/master) |
| [Open Model Zoo多摄像头跟踪demo](https://github.com/openvinotoolkit/open_model_zoo/tree/master/demos/multi_camera_multi_target_tracking_demo/python) | 多段视频/摄像头帧，检测/分割加ReID，输出对象ID和历史 | 适合离线MTMC基线、关联流程与评估参考；完整demo是Python，接入现C++核心仍需适配 | 仓库Apache-2.0，模型逐项查看model.yml中的license。所查 [person-reidentification-retail-0277](https://github.com/openvinotoolkit/open_model_zoo/blob/master/models/intel/person-reidentification-retail-0277/model.yml)指向Apache-2.0，不推广到其他模型。官方 [README](https://github.com/openvinotoolkit/open_model_zoo#readme)明确maintenance mode；近期提交不等于积极扩展功能 |
| [NVIDIA DeepStream](https://github.com/NVIDIA/DeepStream) | 多路视频、GPU解码/推理/跟踪；[MV3DT参考](https://github.com/NVIDIA/DeepStream/tree/main/src/apps/reference_apps/deepstream-tracker-3d-multi-view)输出多视图关联 | C++/GStreamer多路管线可参考；MV3DT要求同步视频、相同分辨率和相机投影矩阵，不能用当前人脸抓拍/未经核实的拓扑边替代其输入。接入先在隔离数据和目标硬件验证 | 主仓代码Apache-2.0、文档CC-BY-4.0，专有runtime及Release资产另外授权。官方主仓声明持续维护；旧 [reference_apps](https://github.com/NVIDIA-AI-IOT/deepstream_reference_apps)已停止更新并指向新主仓。具体OS/CUDA/驱动按所选版本核对，不自动升级 |

Deep SORT可作为经典比较项，但原 [nwojke/deep_sort](https://github.com/nwojke/deep_sort)示例依赖旧TensorFlow、代码 [GPL-3.0](https://github.com/nwojke/deep_sort/blob/master/LICENSE)，不列为当前C++/Go直接复用首选。这里的许可描述依据项目声明，未对下游组合给法律结论。

同镜track ID、人体外观相似、人脸实名身份、跨镜候选身份应分别记录。以上组件没有自动提供本项目的权威活动作用域、可信时钟准入、摄像头拓扑完整性、出闸身份绑定与168小时全存储清理；这是对所查接口及本项目合同的工程分析。跨镜候选不能直接升级成确定人脸身份、精确坐标或唯一路径。

## 完整人脸服务与C++运行组件

- [CompreFace](https://github.com/exadel-inc/CompreFace)是可自托管的人脸服务，提供识别/验证/检测REST API、管理界面、角色权限与Docker配置，适合参考图库管理、接口与管理流程。[README](https://github.com/exadel-inc/CompreFace#readme)与[API文档](https://github.com/exadel-inc/CompreFace/blob/master/docs/Rest-API-description.md)。代码[Apache-2.0](https://github.com/exadel-inc/CompreFace/blob/master/LICENSE)；具体模型及其依赖仍分别核对。这里不把现有C++/Go架构改成该整套服务，也未验证其目标设备性能。
- [ONNX Runtime](https://github.com/microsoft/onnxruntime)是已有ONNX模型的成熟推理运行时，代码MIT，提供[C++ API与安装说明](https://onnxruntime.ai/docs/get-started/with-cpp.html)。优先考虑让C++继续运行已核实许可的同一SCRFD/ArcFace模型，再由Go承接业务编排；不是新增识别算法，也不意味着迁语言自动获得600路容量。版本、Execution Provider、预处理及输出特征应做同输入等价核验。

## 对当前项目的复用建议及交接

1. 保留现有SCRFD/ArcFace研究基线，先确认权重来源和用途授权；原生部署优先用ONNX Runtime C++，不为换框架重建第二份身份状态。
2. 若输入有连续帧，离线评估ByteTrack的同镜关联与抓拍去重；仅有离散人脸图时，不把它当跨镜追踪算法。
3. 若取得全身视频，再评估FastReID或OpenVINO MTMC；若有同步视频/投影标定/NVIDIA GPU，则评估DeepStream管线。人员ReID属于候选信号，仍用当前人脸身份门槛及可信时空证据决定关联。
4. CompreFace参考系统管理、REST和角色边界；OpenCV YuNet/SFace与SeetaFace6作为轻量/CPU或SDK对照。更换特征模型需重建图库，原ArcFace阈值和向量不能混用。

后续具体评估前固定repo commit、代码LICENSE、模型文件SHA/许可、训练数据说明和部署平台；选用已有离线真同人/异人标注，比较误关联、漏关联、拒绝率、时延及恢复行为。若用户要求继续实施且数据/用途授权缺失，应先询问必要资料。本次只有公开源码调研和文档更新，候选代码未安装运行，没有后端联调、生产部署或容量验收。现有业务源码与上次维护修复测试不变。

新踩坑记录：官方仓库迁移不能用旧地址的活跃度代替新入口；新PersonAnalysis功能上线仅9天，不继承老SCRFD/ArcFace的验证成熟度；框跟踪、外观ReID与人脸身份的输入/证据含义不同；“源码许可宽松”不能推导“所有权重和训练数据均已授权”。
