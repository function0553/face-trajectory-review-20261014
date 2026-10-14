# 2026-10-12 成熟开源方案与现有算法源码对照

## 范围与取舍

本次目标是完善现有 SCRFD10g / ArcFace512 与安保关联链路，优先减少误认和错关联。保留现有模型、512D 特征空间及质量/身份阈值；不以轻量模型、降维或放松门槛替代当前算法。调研、回归只在本机源码及新隔离内存 ONNX/测试资料中进行，未访问生产、服务器、相机或生产数据库。

本节核对了 P1 检测、五点对齐、ArcFace、ORT 后端、质量和图片流水线，以及既有原生 C++ ORT 对照。来源采用 InsightFace 官方仓库固定提交 [`81929474ec02e54e3655f6841bced800009c592d`](https://github.com/deepinsight/insightface/commit/81929474ec02e54e3655f6841bced800009c592d)，2026-10-04；本机读取日期 2026-10-12。后续不能把浮动 master 的新行为混入同一验收。

## 人脸模块：可以直接复用的能力与已经存在的能力

当前已经具备 SCRFD RGB/float32/NCHW 预处理、五点全点相似变换、112×112 对齐、ArcFace 动态批次和 512D/L2 检查；不需要再完整引入一套同类服务。参见 [SCRFD](../../face-recognition-engine/src/face_engine/detector/scrfd.py)、[对齐](../../face-recognition-engine/src/face_engine/alignment/face_align.py)、[ArcFace](../../face-recognition-engine/src/face_engine/recognizer/arcface.py)。ORT 创建器已经核对实际 provider，并打印 CUDA 初始化失败的 CPU 回退，[后端](../../face-recognition-engine/src/face_engine/onnx_backend.py)第33–65行。图片流水线已经先做质量、再提特征，并按不同人员聚合 top1/top2，而不是把同人的重复模板当第二竞争者，[流水线](../../face-recognition-engine/src/face_engine/pipeline/image_pipeline.py)第14–26、72–123行。

下文第1、2项是用现有代码和真实 CPU ORT 复现的具体合同/门槛缺口；第3、4项是进一步成熟化的验收工作。复现中的 Constant 模型是主动构造的错误/边界输入，不能由此推断现有真实研究权重已经输出错误，也不能当作人脸精度验证。

### 1. 高优先级：复用官方 SCRFD head 合同，拒绝静默错解

**复用内容：** 官方9个输出固定每网格2个 anchor，15个输出固定1个 anchor；不是由任意输出行数反推。官方也区分原始图输入约束与 session 有效形状。[官方 head 定义](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/model_zoo/scrfd.py#L553)、[输入尺寸/会话检查](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/model_zoo/scrfd.py#L348)。

**现有缺口，修复前行号：** `scrfd.py`第85–102行只校验配置尺寸和输出数量，没有检查模型输入 rank/channel/batch/dtype/static空间大小；第124–143行先 flatten/reshape，再用 `len(score)//cell_count` 推 anchor 数。错误输出若仍能整除格点数，就会被当成另一种 anchor 排布。

**真实最小复现：** 真实 ORT 1.30.0，模型输入 `[1,3,height,width]`，9个 Constant heads 的分数行数为12800/3200/800（640×640、每格2 anchor）；调用配置320×320。旧代码推成每格8 anchor并返回3个检测框，首框 `[0,0,80,80]`，没有拒绝。假模型只在内存中序列化，无文件模型替换。这个例子验证的是错误导出/接线资料会被误接受的边界。

**已封存红测试：** [verify_scrfd_contract.py](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/verify_scrfd_contract.py)与[before.json](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/before.json)：8/20项通过，12项不符合合同仍被接受，命令退出1。正常动态320/640、静态320、输出batch1以及15头640均可用。错误静态输入配置、batch/channel/rank/uint8、错误头行数、rank和batch2均包含在回归中。

**局部修复及绿标准：** 构造阶段检查输入 `[1,3,H,W]` 或空间动态的 float32 合同；实际推理输出必须为 `[count,1/4/10]` 或 `[1,count,1/4/10]`，每层 count严格等于配置格点数×官方anchor数，不能通过 reshape 掩盖其它结构。保持正常模型、预处理和阈值。动态模型不能仅因输出元信息仍写640而被拒绝：本机 `det_10g.onnx` 输入动态、输出声明仍是640行数，既有320分支实际结果正确但曾出现元信息警告。检查实际数组，不能把这个已知警告等同于本轮错解证明。

```powershell
$reusePython = 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe'
$reuseEvidence = 'face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse'
# 每次取新目录。--expect仅标注证据；旧版必须显式加载冻结模块。
$reuseRun = Join-Path $reuseEvidence ('recheck-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $reuseRun | Out-Null
& $reusePython "$reuseEvidence/verify_scrfd_contract.py" --expect before --detector-module "$reuseEvidence/before/face-recognition-engine__src__face_engine__detector__scrfd.py" --output "$reuseRun/scrfd-before.json"
# 上一条预期退出1、8/20；下一条预期退出0、20/20。
& $reusePython "$reuseEvidence/verify_scrfd_contract.py" --expect fixed --output "$reuseRun/scrfd-after.json"
```

### 2. 高优先级：保留裁边前的检测框，使现有 FACE_CLIPPED 门槛有效

**复用内容：** 官方解码没有默认把框裁到图内；其 forward调用 `distance2bbox`时未传 max_shape，后续再恢复原图尺度。[官方解码](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/model_zoo/scrfd.py#L473)、[forward调用](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/model_zoo/scrfd.py#L731)。

**现有缺口，修复前行号：** `scrfd.py`第152–154行先clip四边；`quality.py`第126–130行再用收到的 bbox计算被裁面积。当前检测器返回的框都已在图内，因此这个路径的 `clipped_fraction`被抹成0，已有 `max_clipped_fraction=0.08`不能检测原始框越界。

**封存最小复现：** 合法160×160、9头每格2 anchor的本地生成隔离 ONNX，唯一高分候选在stride8中心 `(80,80)`，解码原始框 `[-40,0,120,160]`。五点为 `[[30,50],[90,50],[60,85],[35,120],[85,120]]`；固定种子清晰随机图只用于满足锐度条件。旧检测器返回 `[0,0,120,160]`，质量接受、clip比例0；同图同点使用未clip框时，比例0.25，既有门槛返回 `FACE_CLIPPED`。这验证门槛传递，不代表该合成图是真实人脸或具有识别准确率。

**已实现：** 外部显示框继续clip；检测结果新增可选 `raw_bbox`，保存同一候选在原图坐标下裁边前的框，随过滤和NMS索引一起传递。质量函数验证其有限、正面积及 `bbox == clip(raw_bbox)`，仅裁边比例使用原始框，其余尺寸、锐度、姿态仍按原测量。旧检测器没有该字段时保持原bbox兼容；这个兼容路径不能恢复已经丢失的裁边信息。没有修改64px/20px/0.08等质量阈值，没有通过放大低清图取得合格像素数。

修复约束新进入的图像，不自动审计或删除历史登记模板。已存向量不能反推出原脸边界/锐度；排查过去低质量登记需提供原登记图和来源，在本地独立复核后再决定显式同步，本轮没有改写现有图库。

实际图片入口和 `TrajectoryExtractor` 都沿用先质量后特征顺序；边界候选被拒绝后，不调用ArcFace或身份索引。正常候选的完整输出、实际112×112对齐像素及活动/route/source/time/session/feature_space传递与冻结旧版本一致。此入口测试使用真实ORT检测和计数型512D提特征桩；真实模型的GPU数值对照另外列在实施结果中。

### 3. 中优先级：复用上游差分测试，冻结现有预处理/对齐/NMS行为

当前ArcFace `(value-127.5)/127.5`与研究 `w600k_r50.onnx`相符，其前8个节点没有嵌入Sub/Mul归一化；官方还支持带嵌入归一化的其它模型，不能把其自动适配直接套成更改当前模型的理由。[官方ArcFace](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/model_zoo/arcface_onnx.py#L13)。当前五点模板与官方相同，但当前为自有闭式解，官方用 `SimilarityTransform`；需要矩阵、裁剪像素、特征的多样本差分，不能仅因数学同类就声称完全等价。[官方对齐](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/utils/face_align.py#L6)。

已确认行为差异：本地letterbox使用round和理论scale，官方用int及实际缩放高度；本地OpenCV NMS使用连续面积，官方IoU含 `+1`，官方相同分数采用stable排序。示例64×64框横向相差27.6px，在IoU阈值0.4附近，两种算法可有不同去重结果。本地先用 `>=0.5`选候选，再交NMSBoxes的严格 `>0.5`分数筛选，恰0.5候选会被再次丢弃；这是接口语义差异，不是现场精度结论。[官方letterbox/NMS](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/insightface/model_zoo/scrfd.py#L809)、[官方测试](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/tests/model_zoo/test_scrfd_call_threshold.py)。

建议复用它的测试思路并保留本地合同：宽高奇数/非方图、画面边缘、相同分数、IoU边界、五点噪声与退化输入；逐阶段比较预处理、未clip框、关键点、变换矩阵、对齐像素、512D向量及最终状态。NMS/letterbox改动会改变检测与对齐，必须先记录基线和明确验收，不应与本轮fail-closed合同守卫混在一起。

已有[原生ORT证据](../../face-recognition-engine/evidence/2026-10-10-inference-performance/REPORT.md)第20–28行证明固定浮点输入40个输出张量最大差0；没有覆盖解码、检测后处理、对齐、质量和完整业务链路。C++可复用现有原生接口与张量基线，再实现这些同合同步骤。该证据不是已完成的P1 C++迁移，也不支持稳定语言倍数提速。

### 4. 高优先级、需人工真值：复用官方评估计算，标定误认与UNKNOWN/REVIEW边界

当前质量和身份输出仍明确 `thresholds_calibrated=False`，质量默认值和默认0.35/0.50身份阈值是开发起点。[质量](../../face-recognition-engine/src/face_engine/quality.py)第3、16–23、99行；[图片流水线](../../face-recognition-engine/src/face_engine/pipeline/image_pipeline.py)第85、116–123行；[默认配置](../../face-recognition-engine/configs/default.yaml)。已有[质量边界证据](../../face-recognition-engine/evidence/2026-10-10-monitoring/quality/verification-summary.json)为工程检查，不是现场误认率。

官方 `verification.py`提供ROC、交叉验证、指定FAR下的验证率及误接受计数；可以提取计算方法作离线评估，不必引入其训练侧MXNet/Torch环境。[官方评估](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/recognition/arcface_torch/eval/verification.py#L54)。它主要是成对验证，不能代替当前全库开放集识别；需增加未登记人员对全库top1的误接受、不同人top2竞争、重复模板、UNKNOWN/REVIEW比例及质量拒绝分层。其距离阈值也不能直接当本地余弦阈值复制。

需要的资料：人工确认同人/异人及未登记人员的真实照片，含登记与跨相机/时段抓拍；按人员/会话分离标定集和冻结验收集，并附相机、原图像素、姿态、遮挡、光照与resize来源。历史自动GID、同图变换和合成身份不能当真值。本轮保持门槛，后续先约束误认，再比较漏认和质量拒绝；重点/普通人员同门槛，不因优先调度改变身份接受规则。

## 现有关联、跟踪和运行层：保留深度，不重复引入

已核对 [C++图库](../cpp/src/gallery.cpp)、[关联](../cpp/src/association.cpp)和 [Go worker客户端](../go/internal/worker/client.go)。当前图库按完整feature_space隔离，512D全量搜索、double累加、同人多模板取最大分数；关联在完整图库比较不同人竞争者，再执行拓扑可行性与margin约束。返回给客户端的top8是显示范围，不是只查8个身份。Go已经用单worker队列、事务及重启恢复。不能把这些能力写成“缺少FAISS/多模板/top2”，也不能引入ANN使竞争者消失来得到更高接受率。

主监控入口 `MonitoringRuntime → TrajectoryExtractor` 没有使用旧CLI的RecognitionCache；旧POC `short_tracks.py` 也已有embedding、IoU、中心/面积及双向唯一/歧义拒绝。它们的用途是短时去重/展示，不能等同于正式跨相机身份轨迹。这轮没有往正式链路叠加另一个缓存或tracker。

| 成熟组件 | 可取部分与条件 | 本轮状态 |
| --- | --- | --- |
| [ByteTrack固定提交](https://github.com/ifzhang/ByteTrack/tree/d1bf0191adff59bc8fcfeaa0b33d3d1642552a99)（MIT） | C++ Kalman、两阶段高低分框关联、LAPJV。默认每次调用dt=1/frame_id递增，需适配当前丢帧/最新帧队列的实际间隔和重连epoch；低分框只能延续匿名运动，不能绕过人脸质量、身份、时钟或拓扑门槛。 | 源码对照完成，未接入。先取得连续视频及独立标注。旧Linux/TRT演示不是本机Windows生产运行层。 |
| [TrackEval固定提交](https://github.com/JonathonLuiten/TrackEval/tree/12c8791b303e0a0b50f753af204249e622d0281a)（MIT） | 连续frame/bbox真值下的IDF1/HOTA等评估。当前P2是摄像头节点观测，FaceObservation不携带完整连续框轨迹，不能填造帧/框算HOTA。 | 借鉴独立真值、完整覆盖和对照思想；本轮实现P2事件层身份/匿名关联指标，未安装TrackEval。 |
| InsightFace官方验证计算 | ROC/FAR、成对同异人验证与阶段数值对照；需要开放集全库扩展。 | 复用SCRFD合同和裁边来源，保留原对齐/预处理；没有加入训练侧环境或改模型。 |

既有1,288条历史抓拍的GID/person_id字段标为unverified、框由模型生成；采集真值模板仍为空。它们可以验证数值兼容和取证格式，不能当作独立身份真值。已有单probe诊断、30张无标签候选和单人重复特征压测不能证明开放集误认率或5万模板容量。

## 本轮已实施及实际证据

P1业务源码仅修改 `detector/scrfd.py`、`detector/base.py`、`quality.py`：同模型输入/输出合同校验及已有质量门槛的数据传递。没有重写推理链、替换权重或增加Python生产模块；原C++核心和Go保持。新Python评估器只读保存的离线文件。

| 验证对象 | 实测与边界 | 证据 |
| --- | --- | --- |
| 冻结旧SCRFD → 当前SCRFD | 旧8/20（退出1），新20/20（退出0）；实际加载路径和SHA核对通过 | [旧版复跑](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/scrfd-frozen-before-r2.json)、[最终新版](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/scrfd-contract-final-r2.json) |
| 裁边质量 | 旧边界被接受/比例0；新比例0.25并FACE_CLIPPED；阈值上下/等于、默认0.08、8个非法输入、旧字段兼容、正常质量完整JSON共23项通过 | [红报告](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/clipping-before-final.json)、[绿报告](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/clipping-after-final.json) |
| 实际P1入口接线 | 23项通过；边界在TrajectoryExtractor观测0/提特征0，图片入口候选0/提特征0/实际NumPyIndex调用0；正常活动字段、完整输出、112对齐像素与冻结基线一致 | [入口报告](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/clipping-pipeline-final.json)；真实ORT detector、计数型recognizer，不是模型精度 |
| 现有真实模型/CUDA | 两张已有照片分别320/640，4/4对照通过；其中320两例实际512D差0，640两例旧/新均无脸，仅空输出一致 | [GPU对照](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/existing-cuda-parity-r2.json)；无身份真值，样本有限 |
| 维护兼容回归 | 原8项通过，真实C++适配器、HTTP和新本地隔离库 | [维护报告](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/p1-maintenance-current.json)；原维护修复证据仍由P1第19节追溯 |
| P2离线评估工具 | 真实现有C++worker/new SQLite的11事件，独立合成GT先写后执行；59项通过，预写23条完整请求与实际发送一致，HOLD/PROVISIONAL保持。 | [评估测试](../evidence/2026-10-12-no-downgrade-reuse/identity-evaluation-05/summary.json)，合成数据仅验证工具合同 |

模型SHA保持：SCRFD `5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91`、ArcFace `4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43`；原recognizer、配置、对齐不变。当前worker仍 `535bf8b496253ac21b3ffb63caaa1d8be57f5164633aea562b8dcb6105d065c7`。封存的基线、源码/模型/程序、全部证据与文档SHA见[本次清单](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/final-manifest.json)。InsightFace研究权重的许可仍需按现场用途另行解决，本次没有获得商用许可。

## 离线身份/错关联评估输入合同

2026-10-12用户确认“暂无标注，先完善离线验证工具”。工具已完成59项本地合成合同验证；当前不执行真实误认率测量或阈值标定。新增[人工标签填空模板](../collection/templates/identity_truth.template.jsonl)，复制后按[填写说明](../collection/README.md)独立核验；模板默认不完整，不能直接作为GT通过评估。旧final-manifest是上一轮快照，本次补充材料及文档SHA见[无标注后续清单](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/no-labels-followup-manifest.json)。

[evaluate_identity_events.py](../tools/evaluate_identity_events.py)只读取保存的P2事件结果和独立标签，不执行推理、不生成GT，也不修改配置或数据库。范围固定 `input_stage=P2_FEATURE_EVENTS`：已经提取的特征观测，包括P2 HOLD/EXPIRED。P1整图漏检、质量拒绝及视频丢帧不在这个分母中；因此不能用此报告声称整条图片链路的误认/漏认改善。

| 文件 | 必需字段及要求 |
| --- | --- |
| truth.jsonl | `(activity_id, source_id, observation_id)`唯一键；独立 `episode_id/truth_person_id`；`enrolled_target_id`为已登记target或显式null；`truth_complete=true`、`truth_evidence_id`和 `label_origin=INDEPENDENT_REVIEW`。合成工具测试只能用 `SYNTHETIC_INDEPENDENT_FIXTURE`。同活动同真值人员的登记状态必须一致，不能把两人标成同一登记target。 |
| predictions.jsonl | 同一完整键集合、每条 `input_sha256`、完整 `feature_space` 和保存的worker `result`。result须ok=true，作用域/观测/来源/policy一致；入流status只接受ACCEPTED/HOLD/EXPIRED，自动关联只接受MATCHED/CREATED/UNASSIGNED/PROVISIONAL/CONFLICT。人工纠错 `CORRECTED`拒绝，非确认状态不能带target。缺行、重复、跨活动/source、未知状态、worker错误、非有限JSON均报错。 |
| manifest.json | `schema_version=1/dataset_id/truth_sha256/worker_sha256/evidence_kind/ground_truth_independent=true/input_stage/embedding_dim=512`；完整六字段feature_space、实际生效identity_policy、完整八字段quality_policy、每活动全部登记target；完整模板和配置登记资料的enrollment_sha256、graph_sha256、clock_profiles_sha256。 |
| manifest重放来源 | `replay_mode=AUTOMATIC_OBSERVATION_REPLAY`、完整有序非GT请求JSONL的 `request_timeline_sha256`、`operation_inventory`（op→正整数次数）。允许clock_profile_put/activity_sync/activity_bindings/activity_monitoring/monitoring_person_put/monitoring_observe，拒绝association_correct及其它操作。基线/候选时间线摘要、inventory必须相同。 |

feature_space六字段为 `recognition_model_sha256/detector_model_sha256/detector_profile/alignment_version/preprocessing_version/output_version`。identity_policy要求实际生效的match/new-target/margin/template-update/template-similarity、检测质量、时钟误差、模板上限、topology设置和重叠相机完整值；不能只写policy_id。完整可解析合成范例、模板向量、图、clock、顺序请求及I/O见 [identity-evaluation-05目录](../evidence/2026-10-12-no-downgrade-reuse/identity-evaluation-05)。范例阈值/特征是测试资料，不得复制为现场策略。

报告单列登记正确/错误关联、未登记误接受为登记人员、HOLD/PROVISIONAL和确认覆盖率。匿名身份按同一 `(activity_id, episode_id)` 内人工same/different配对算false_merge/false_split，跨来源可同组，跨活动不比较；自动生成target字符串不能直接拿来等同人工person ID。匿名拒绝会破坏同人配对并计false_split，不能通过全拒绝取得零错并；零分母为null，缺结果直接错误。

基线对照冻结除worker SHA以外的全部manifest及每事件输入SHA，要求全局和每活动的错误关联、未知误接受、匿名错并/错分不增加，登记正确数、匿名确认覆盖不下降。A活动改善不能掩盖B退步。只有提供基线才产生non-regression结论，单次报告没有“优化通过”含义。

工具检查的是提供的文件和声明，`worker_execution_verified/label_independence_verified/timeline_execution_verified=false`；合成资料 `field_accuracy_claim=false`。测试驱动另外逐条核对实际发送请求与预写ledger，证明本fixture执行；通用读取器不能证明标签真的人工独立复核或未披露的纠错没有发生。真实标注评估需另存原始完整请求/结果、独立标签审阅证据及冻结验收集，不能拿自动GID替代。

```powershell
# 替换成已经保存的独立标注和同输入基线/候选文件；不会运行或修改生产。
$reusePython = 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe'
& $reusePython 'face-trajectory/tools/evaluate_identity_events.py' --truth '本地标注/truth.jsonl' --predictions '本地标注/candidate.jsonl' --manifest '本地标注/candidate-manifest.json' --baseline-predictions '本地标注/baseline.jsonl' --baseline-manifest '本地标注/baseline-manifest.json' --output '本地标注/新评估报告.json'
```

退出码0为输入有效且提供的比较通过（未提供基线时只是单次计算成功），1为比较退步，2为输入合同错误。output必须不存在。没有真实已标注样本前，本轮只交付确定性质量修复、数值兼容证据和评估工具，不宣称现场误认率已下降。

## 本地接手和复跑

以下从 `D:\人脸算法` 运行，只新建本地隔离资料；不要传生产库。每次选新输出目录。当前所有回归脚本拒绝覆盖报告；两个clipping脚本的模型fixture写在该输出旁独立目录，目录已有也拒绝，不再写回原封存fixture。

```powershell
$env:PYTHONUTF8 = '1'
$reusePython = 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe'
$reuseEvidence = 'face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse'
$reuseRun = Join-Path $reuseEvidence ('recheck-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $reuseRun | Out-Null
& $reusePython "$reuseEvidence/verify_scrfd_contract.py" --expect before --detector-module "$reuseEvidence/before/face-recognition-engine__src__face_engine__detector__scrfd.py" --output "$reuseRun/scrfd-before.json"
# 上一条预期退出1、8/20；本轮修复目标是下一条20/20。
& $reusePython "$reuseEvidence/verify_scrfd_contract.py" --expect fixed --output "$reuseRun/scrfd-after.json"
& $reusePython "$reuseEvidence/verify_clipping_quality.py" --phase before --source-root "$reuseEvidence/clipping-before-src" --output "$reuseRun/clipping-before.json"
# before报告预期RED_REPRODUCED；after读取该次before资料对照正常输出。
& $reusePython "$reuseEvidence/verify_clipping_quality.py" --phase after --before-report "$reuseRun/clipping-before.json" --output "$reuseRun/clipping-after.json"
& $reusePython "$reuseEvidence/verify_clipping_pipeline.py" --output "$reuseRun/pipeline.json"
& $reusePython "$reuseEvidence/verify_existing_parity.py" --device cuda --output "$reuseRun/gpu.json"
& $reusePython 'face-recognition-engine/evidence/2026-10-10-activity-retention/verify_p1_maintenance.py' --adapter 'face-recognition-engine/evidence/2026-10-12-p1-maintenance-recovery/adapter-after.exe' --output "$reuseRun/maintenance.json"

$identityRun = 'face-trajectory/evidence/2026-10-12-no-downgrade-reuse/recheck-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $reusePython 'face-trajectory/evidence/2026-10-12-no-downgrade-reuse/verify_identity_evaluation.py' --worker 'face-trajectory/cpp/build-activity/Release/face_trajectory_worker.exe' --output $identityRun
```

GPU命令明确要求实际CUDA provider，不以CPU结果代替CUDA通过。合成质量和提特征桩、两张真实照片数值对照、P2事件评估分别记录，不能相加成模型精度样本数。回退/对照只加载封存旧模块到测试进程；不要替换当前运行文件、模型或数据库。

## 踩坑与接手

- 从固定提交源码提取能力，区分“已有能力”“真实复现缺口”“后续评估”；不根据项目名或GitHub热度整套替换。
- 动态输入模型的过期静态输出元信息必须与实际输出分开。只读描述符不能证明运行错解，也不能因警告就重写权重。
- 错误heads复现用真实内存ONNX/ORT，而不是只让假session抛异常；红报告保留source与模型SHA。回归输出拒绝覆盖已有文件。
- 一次 `--source-root clipping-before-src` 复跑被该测试快照的包初始化转到当前类，错误地得到20/20。保留 [该次记录](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/scrfd-frozen-before-rerun.json) 为测试入口事故；现在显式模块路径并核对实际类文件SHA。它不是旧版绿、也不是业务故障。
- GPU脚本首版误要求每个输入分支必须检出脸；两例640原本就无脸，修正为单列空输出对照，并要求每张图至少一个分支实际走512D。入口计数测试也不能硬猜内部size调用次数，改为与实际旧基线比较；未修改业务逻辑来满足脚本。
- 两个clipping脚本初版虽然报告路径可变，模型fixture仍写在脚本旁固定目录；最终改为每报告独立fixture目录并拒绝已有路径，旧封存文件SHA不变。复跑隔离需检查所有输出位置，不能只看报告参数。
- 评估器需记录实际入口生效的策略默认值；monitoring_observe与独立association配置的时钟默认值不同。本次合成fixture记录实际60000ms，没有改业务值。人工纠正CORRECTED不能作为自动识别成绩。
- 随机清晰图只验证质量门槛的数据传递；一次同图相似度、30张无标签图及原生张量一致都不能替代人工身份精度验收。
- 本次文件是源码复用调研的单一归档；实施结果、复跑命令和未完成边界由主任务更新P1/P2现有HANDOFF，不新建另一份交接入口。
