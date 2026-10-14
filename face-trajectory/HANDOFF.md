# P2 摄像头点位时空算法交接

**2026-10-14修复后当前状态：** D-01～D-07与可选拓扑参考折线已完成本地验证，见[统一修复交接](docs/repair-topology-2026-10-14.md)及P2本轮validation-summary.json。旧ZIP/旧SHA代表修复前快照；真实业务、生产、现场准确率和容量未验收。

**当前审核包（2026-10-14）：** `D:\人脸算法\时空算法-审核包-20261014.zip`，说明见[第33节](#review-package-20261014)。

**当前本地交付（2026-10-13）：** 普通摄像头点到点连接接口与只读本地查看已完成，见[第32节](#point-connections-20261013)。当前新worker在`cpp/build-point-connections/Release`；既有服务未自动切换。鹰眼单镜连续轨迹后置。

**2026-10-13调研：** GTR、ByteTrack及YOLO/SCRFD/ArcFace组合的论文/源码核查、适配判断和下一步实验见[第31节](#gtr-bytetrack-20261013)。本次只更新文档，候选算法尚未接入；下方2026-10-12实施状态和证据保留原日期。

**本轮优化（2026-10-12）：** 保持现有模型的P1质量修复及P2离线错关联评估见[第30节](#no-downgrade-reuse-20261012)；不改变正式C++/Go、512D、身份/时间/拓扑/保留政策。

更新：2026-10-12，Asia/Singapore；本轮实现多活动隔离、签名授权与168小时滚动保留并完成收尾核对。项目目录：`D:\人脸算法\face-trajectory`。本文件是当前交接入口；接口细节以 [OpenAPI 0.13.0](openapi.yaml) 和专项文档为准。

**活动/保留交付见第27节；后续P1维护恢复修复、当前worker及配套Python维护兼容见第28节。第25节是实施前只读核查，第26节保留阶段1完成时的历史进度；其中“未实现/实施中”不代表当前源码状态。生产部署、真实前后端联调和容量验收仍未执行。**

**当前状态：本地已实现时间校正、摄像头经过序列、历史候选连接、末次观测后的可达范围及新观测修正。已有独立逻辑和接口验证，真实同人轨迹、场地连接及全场容量尚未验收。**

最新三类监控改造见第18节及[监控接口](docs/monitoring.md)，成对构建在 `cpp/build-monitoring/Release`，未替换既有本地查看进程或部署生产。第12节的[一致性合同](docs/consistency.md)和第17节的60秒时间规则继续适用。已支持“算法结果直接推送、按需查询”，后端真实接收接口尚待联调；详见第10节。

## 1. 目标与工作边界

人员已关联的抓拍记录进入算法，按摄像头点位和时间形成经过序列，再推断可能经过的连接、可能到达的摄像头或通道。相机安装位置表示观测节点；节点之间的顺序连线不证明人的详细步行路线。

- **生产服务器及其 Java、数据库、模型、摄像头只允许读取。** 开发、构建、测试和查看均在本地；先前两次连接在SSH banner阶段超时的记录保留为历史状态，2026-10-10最新成功只读核查见第19节。
- 用户2026-10-10明确扩展为实际人脸比对：人员资料提供 ID、重点/普通标记、照片；P1自行拉流或接海康/后端抓拍并提取兼容特征，P2负责比对关联与轨迹。已确认的经过记录接口继续可用，不能把此历史接口当成本轮全部链路。
- C++17 负责算法，Go 提供薄 HTTP 接口，Java 负责业务资料、已有结果接入和权威原始记录保存，生产前端负责最终显示。SQLite 保存算法状态与不可变版本。
- 既有 `AssociationStore` 能复用兼容的 512D 多模板特征判定匿名同人，但现场特征合同未接通。Java 的实名资料映射与视觉同人判定是不同职责。
- 座位号查询、实际落座定位、鹰眼 ARM 设备算法包后置。鹰眼点位可以作为场景数据保留，当前 Windows 产物不属于该设备的上传包。

## 2. 当前处理流程

| 环节 | 当前行为与关键限制 |
| --- | --- |
| 接收经过记录 | 要求稳定事件 ID、camera_id、已有归属证据及原始抓拍时间。身份未确认、设备未绑定或时间不合格的记录保持 HOLD，不强行拼成同一人。 |
| 时间校正 | 按不可变相机校准版本处理时区、偏差、文本精度、误差和有效期。正偏差表示相机快，校正时减去；未知来源、过期、回拨歧义或溢出均 HOLD。不会自动从一次读时诊断估出历史校准。 |
| 形成点位轨迹 | 保留合格经过事件、全部位置候选及时间顺序歧义。历史核心结合前后观测与给定图计算候选连接；图内唯一候选不等于唯一真实路线。 |
| 推断后续位置 | 从可能的最后观测出发，按时间、通行下界、开放窗和停留扩展候选相机/通道。保留岔路，不输出精确人物坐标或未经校准的概率。默认预测期限 5 分钟。 |
| 新观测修正 | 更新完整记录集形成新版本，比较旧预测，再从新版本重新推断。历史依据被更正、撤回或补入旧记录后，旧预测不能继续用于判定新事件矛盾。 |

前向预测是保守可达集合，历史整链一致性由已有轨迹核心负责。时间误差使最后事件次序不确定时保留多个起点；查询落在末次抓拍误差区间内则返回 UNKNOWN。默认允许在节点及通道停留，开放窗关闭不代表强制离开。新观测使候选重新计算，不保证每次都是旧集合的子集。

## 3. 已复用的数据与缺少的证据

以下数字来自本地保留的 2026-10-07 只读快照，不代表持续监测结果。

| 数据 | 当前可用情况 |
| --- | --- |
| 后台设备和发布标注 | 853 台摄像头；两份发布方案有 103 个标注，其中 94 个摄像头标记、9 个场地标记。 |
| 明确点位绑定 | 63 个标记绑定 62 台设备，31 个摄像头标记未绑定。南门同设备有两个位置，相距约 108.349 米；按用户要求全部保留，不取均值、不自动选点。 |
| 模型连接候选 | `camera-points-8a83765513dd48d41e56` 包有 878 条未核定主边，覆盖 52 台设备；另 10 台缺少主边。64 条涉及冲突位置的完整候选单独保留，2136 个未完成模型查询保留未知。 |
| 实际模型局部副本 | 1792 个 GLB，178,910,980 字节，提取 2,265,334 个三角形。省略纹理、植被实例等；局部文件量不是全场资源总量，部分源节点未经现场实测。 |
| 真实抓拍与时间 | 30 条历史元数据尚未关联，6 份时间配置未核定；新接口实测仍为 30 条 HOLD、0 个确认锚点。最近只读检查的 `face_comparison` 表无记录。 |

导入器从场景和全部模型查询结果构建通用候选图，不读取测试人的顺序或未来路线。模型长度结合速度假设得到的时间界限尚非现场实测下界，`topology_verified=false`、`topology_is_complete=false`；不能依据缺边或超出这些候选界限删除可靠观测。

先后读时诊断曾出现约 48 秒、251 秒的相对偏差，哪端时钟变化尚未定位。`image_time/utc_time` 的来源及历史时区语义也未确认；NTP 开启或两字段相差八小时不足以建立校准。第三方 opaque 特征与核心 512D 特征未证明兼容；图片引用、face_id 或设备局部 track ID 不可直接当跨相机身份。

来源及导入细节见[点位数据说明](docs/point-graph-data.md)、[最新只读复核](evidence/2026-10-07-reassessment/REVIEW.md)和[模型取证报告](evidence/2026-10-07-model-route/REVIEW.md)。模型报告中的轨迹还原通过结论已撤回，见第 7 节。

## 4. 接入契约

先登记场景、点位图和时间配置，再提交已有归属记录。完整字段与请求示例见[点位接入](docs/point-trajectories.md)。下列均为 POST：

| 用途 | 接口 |
| --- | --- |
| 场景登记及相机上下文 | `/v1/site-scenes`、`/v1/site-scenes/camera-context` |
| 时间配置及单条归一化 | `/v1/clocks/profiles`、`/v1/clocks/normalize` |
| 点位图登记及读取 | `/v1/point-graphs`、`/v1/point-graphs/get` |
| 完整记录集重建及点位读取 | `/v1/point-trajectories/rebuild`、`/v1/point-trajectories/get` |
| 前向预测及新观测比较 | `/v1/point-trajectories/predict`、`/v1/point-trajectories/compare` |

接手时必须遵守：

1. `rebuild` 接收该目标的**完整权威记录集**，不是追加接口；只传新一条会移除未包含的旧记录。接入端保存原始记录、已有同人结果及返回的 `admissions`，补齐资料后重放。相同合格输入重放不增加版本。
2. 每条记录至少包含 `observation_id/camera_id/source_id/link_status/attribution_evidence_id/clock_profile_version/raw_time`。身份和时间均需合格，不能为了测试把真实未知记录改成 CONFIRMED 或 `verified:true`。
3. 图、场景和校准按版本保持不可变；内容变化使用新版本。预测的图核验状态来自登记包，单次请求不能提升它。适配成功不等于核心接收：整秒时间加 100ms 校准误差得到 1099ms 总误差，可能超过核心默认 1000ms 门槛。
4. 保存预测使用的 `version`、`as_of_unix_ms`、`at_unix_ms` 和返回依据。`as_of` 是名义抓拍截止时间，不能冒充后台当时收到消息的时间，也不能还原后来才完成的身份修正。
5. `CONDITIONAL_CANDIDATE` 表示未核定图下的候选；`UNKNOWN` 表示依据不足；`TRUNCATED` 表示搜索不完整。候选之外不等于不可能。比较只有在旧依据未变、图核定完整且搜索完整时，才可能报告模型矛盾，仍保留新观测。
6. 旧 `/v1/timeline/query` 继续用于历史查询，最后观测之后用新 `predict`；`get` 返回点位顺序与位置候选。几何接口可按图版本和 edge_id 绑定外部折线，不能把它当实际脚步真值。

时间和预测的完整语义分别见[时间适配](docs/clock-adapter.md)、[前向可达](docs/point-reachability.md)。已有特征自动关联接口另见[关联说明](docs/association.md)，运行状态、重试与备份见[运行说明](docs/runtime-and-geometry.md)。

## 5. 代码定位与本地运行

| 位置 | 作用 |
| --- | --- |
| [clock_adapter.cpp](cpp/src/clock_adapter.cpp) | 原始时间归一化、校准和误差出处 |
| [point_trajectory.cpp](cpp/src/point_trajectory.cpp) | 点位图登记、场景绑定、经过序列及位置冲突 |
| [reachability.cpp](cpp/src/reachability.cpp) | 多起点前向可达、停留、期限和资源上限 |
| [trajectory.cpp](cpp/src/trajectory.cpp)、[visits.cpp](cpp/src/visits.cpp)、[time_constraints.cpp](cpp/src/time_constraints.cpp) | 历史路线、观测聚合和整链时间约束 |
| [worker.cpp](cpp/src/worker.cpp)、[Go 路由](go/internal/api/server.go) | 入流适配、版本修正和 HTTP 薄接口 |
| [build_point_graph.py](collection/build_point_graph.py) | 从现有快照导入通用候选图 |
| [当前验证与查看工具](evidence/2026-10-07-point-trajectory/REVIEW.md) | 请求/返回、独立测试、SHA 清单及本地 3D 查看 |

<a id="build"></a>
### 构建

依赖 VS2022 BuildTools C++、CMake 和 Go。在项目根目录执行；若本地查看服务正在持有同名 exe，先停止该查看服务，或选用新的构建目录。不要覆盖运行中的生产文件。

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
$p2CMake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
& $p2CMake -S cpp -B cpp/build-point-connections -G 'Visual Studio 17 2022' -A x64
& $p2CMake --build cpp/build-point-connections --config Release
Push-Location -LiteralPath go
try {
    go build -o ../cpp/build-point-connections/Release/face-trajectoryd.exe ./cmd/face-trajectoryd
    go vet ./...
    go test -count=1 ./...
} finally { Pop-Location }
```

当前CMake构建算法库、SQLite、worker及`point_connections_test`（BUILD_TESTING可关闭），新增CTest目标`point_connections`。较早测试源码仍在归档，不恢复其历史通过数；当前点位检查与复跑见[点位连接交付](docs/point-connections.md)，2026-10-08记录保留原日期。

### 本地查看与复起

当前普通摄像头点位连接查看入口为[http://127.0.0.1:8793/](http://127.0.0.1:8793/)，服务只读静态页面，浏览器读取保存的新版查询JSON。默认空白，明确点击后才加载合成接口示例；普通业务输入仍通过带签名与可信UTC的P2监控入流。

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' tools/point-viewer/serve_viewer.py --port 8793
```

旧8769三维查看使用2026-10-07的raw/rebuild和旧认证合同，本次未修改其历史证据或运行进程，也不再作为当前0.13入流方式。新查看页不代理API、不持有凭证、不加载完整模型；查真实结果仍使用第27节签名/可信UTC服务配置。503/504按原事件ID及内容重试；SQLite仍需单权威写入与一致性备份，参见[运行说明](docs/runtime-and-geometry.md)。

## 6. 已有验证与实际限制

2026-10-07 的本地实现验证如下；第 9 节单列 2026-10-08 新核心复跑结果，不改写历史报告。

| 验证 | 已记录结果 |
| --- | --- |
| C++ Release、Go 构建和 go vet | 通过；产物在 `cpp/build-point/Release` |
| 时间适配 | 51 项，使用独立 Python 日期基准，覆盖时区、有效期、回拨、溢出与恢复 |
| 前向可达 | 1248 个独立整数时间穷举案例及 18 项命名检查 |
| 图导入 / 真 Go→C++ / 修正撤回 / 既有场景回归 | 分别 15 / 23 / 26 / 32 项 |
| OpenAPI | 严格 YAML 无重复键，22 条路径、74 个结构、220 个引用可解析；未运行完整 OpenAPI 校验器 |
| 浏览器交互 | 实际模型加载；提交 0/1/2 条时显示 0/0/1 段顺序线；第一次预测 4 个点位，另行输入的新点位不在其中时返回 UNRESOLVED，随后按新观测计算 3 个候选；真实 30 条全部 HOLD |

上述测试证明本地逻辑、协议和来源保留，不证明现场身份准确率、真实通行关系或 400～500 台/4～5万人规模可用。本机吞吐已有容量边界。记录、脚本和哈希见[当前验证报告](evidence/2026-10-07-point-trajectory/REVIEW.md)与[清单](evidence/2026-10-07-point-trajectory/manifest.json)；历史性能与修复另见[运行修复报告](evidence/2026-10-05-repair/REVIEW.md)。

## 7. 踩坑、处理与经验

| 问题 | 已处理方式及后续要求 |
| --- | --- |
| 预设路线循环验证 | 曾把同一路线同时用于模拟行走和算法候选，相关“轨迹还原通过”结论已撤回。见[撤回判定](evidence/2026-10-07-model-route/VALIDATION-ASSESSMENT.json)。当前图不按测试人建边，未来事件隔离；独立真值仍需现场取得。旧 8767/8768 演示不作当前验收入口。 |
| 旧起点被修正仍判新抓拍时间矛盾 | 比较前核对旧依据；更正、撤回或插入旧历史时返回 `UNRESOLVED/PRIOR_EVIDENCE_CHANGED_OR_RETRACTED`，26 项回归覆盖。 |
| 安装点、设计动线、最短路被当真实路线 | 保留节点、候选和观测的区别；南门双点不合并；模型部分路径不当完整到达；图内唯一性注明 `SUPPLIED_GRAPH_ONLY`。 |
| 时区、偏差和接收时间混淆 | 保留原始时间、校准来源、误差及有效期；一次读时和 NTP 状态不作历史校准，不把接收时间当抓拍时间。 |
| 时间误差或等待被忽略 | 整链时间共享约束；最后时刻不确定时保留多起点或 UNKNOWN；通道开放窗只约束出发，不强制离开。 |
| 状态及验证文件被覆盖 | SQLite 不可变版本、冷历史恢复；原始记录在接入端保留；复跑应使用新的输出目录。Windows 测试先关闭明确的本地 worker，再处理 SQLite 临时目录。 |

首次合成修正测试有一份临时目录清理被自动审批拒绝，保留位置及原因见当前验证报告；不影响后续通过结果，无需接手者为算法工作优先处理。

## 8. 接手顺序与资料入口

1. 确认平台已有同人结果的读取合同和稳定人员/事件 ID；按同一接口接入，保留未关联与多候选。若只能提供已有特征，先核对格式及特征空间，继续遵守不重新提特征的要求。
2. 核对原始抓拍字段语义、可信参考时间、校准有效期和误差上界，以新时间配置重放原始记录；软件校正不需要写相机配置。
3. 逐步核定相机连接、通行时间、方向和开放限制，以新图版本登记；不要求用户提供逐米脚步路径。南门点位冲突未确认前继续保留两候选。
4. 用独立确认的同人摄像头序列验收分叉、漏拍、停留、迟到及修正，冻结预测后再对照后续观测，保留失败和歧义。再按真实负载验收吞吐、留存和恢复。

接口、专项说明和历史证据按用途见[文档目录](docs/README.md)。现场采集参考[collection](collection/README.md)；其中座位/鹰眼内容仅作后置资料，不扩大当前范围。

本次整理将重复流水、过时“未实现”描述、旧启动入口及打包细节移出主交接；保留当前能力、运行契约、撤回结论与原始证据。整理前正文和清单已压缩保存并逐项校验，见[整理记录](evidence/2026-10-08-handoff-cleanup/cleanup-report.json)。工作区原有三项目交付 ZIP 未重打包，不能作为本次算法完善后的版本；旧打包详情在[工作区打包记录](../workspace-docs/project-package-20261007.json)。

## 9. 2026-10-08 模型变更续接

**怎么做的：** 沿用 C++/Go/SQLite 和 Recast/Detour，未引入新算法依赖。C++ 对已声明的矩阵、模型信息、设备通道、标记坐标及边绑定逐项核对；模型候选包须保留全部绑定点位。新增 `collection/check_model_coverage.mjs` 显式读取 scene/NavMesh/geometry-report，`build_point_graph.py` 策略升至 `@2` 并传递 `model_binding/model_provenance`；新增 `assess_point_graph_update.py` 给出变更和使用旧图目标的重放范围。场景导入支持 `model_revision`，只采用最后一份表快照并拒绝明示不完整数据。

**踩坑与处理：** 旧图仅改版本号可绑定平移 1000 米、标记从 9 移到 999 的新场景，修复前反例已保存，现拒绝。多个审计快照合并会复活旧标记，而末尾部分快照又可能让 103 个标注变成 7 个；现不合并历史快照，且检查完整性声明，历史缺声明保留未核定状态。输入／输出显式化，新增 CLI 拒绝覆盖已有输出。变更影响可能通过中间通道传播，所以不能只重放直接变化点位的人员。

| 本轮验证 | 结果 |
| --- | --- |
| C++ Release、Go 构建、go vet | 通过；新 worker SHA256 为 `a03ba07f59170df0f7d5e8fb9f9bc0d51cf3702ef518045a2430f7efec1cc8ea` |
| 模型绑定／版本／SQLite 恢复 | 46 项通过；新来源图包实际登记通过 |
| 导入及更新差异／快照完整性／通用模型诊断 | 分别 28／36／8 项通过 |
| 时间／可达范围／修正／真实 Go→C++ | 51 项／1248 穷举案例及 18 项／26 项／23 项通过 |
| 旧局部模型复跑 | 3078 点对，942 完整模型路径；878 主边、64 冲突候选和 2136 未完成查询完整保留 |

最终候选图为 `camera-points-f33a041126289e8ea3f7`，保存在[graph-final](evidence/2026-10-08-continuation/graph-final/graph-packet.json)。这次与旧图没有点位或主边几何变化；新版本来自诊断来源和导入契约变化，不代表服务器发布了新建筑模型。所有模型来源及拓扑仍未现场核定，30 条旧抓拍在新核心 HTTP 回归中仍全部 HOLD。

**服务器与交接：** TCP 建立后两次 SSH banner 超时，发生在认证前，不是口令错误；口令未写入文件。今天没有取得服务器新模型、同人结果或时间资料。已准备有界只读刷新脚本，连接恢复后可检查发布选择和索引；它不是完整标注导入器。后续按[模型更新流程](docs/model-updates.md)获取新来源、重建几何／网格、诊断并登记新图，以完整权威记录集重建和重新预测，旧版本保留。新矩阵／点位校验不能证明 GLB 内容、生成链或现场准确性，任意新模型的自动下载与几何重建仍需按实际发布格式接入。

完整方法、复跑、失败记录及 SHA 清单见[本轮报告](evidence/2026-10-08-continuation/REVIEW.md)。生产、现有运行状态和旧 ZIP 未改动。

## 10. 2026-10-10 结果直接推送与按需查询

**传给后端的数据：** HTTP POST 的 JSON 正文，不是 `.js`、算法源码文件或 SQLite 文件。完整算法结果保留人员/事件 ID、原抓拍时间、版本、摄像头经过序列、位置及连接候选、admissions 的 HOLD/ACCEPTED、归属修正受影响人员。C++ 执行算法，Go 主动发送。历史点位、历史时间轴和前向候选仍按目标、版本与时刻查询。

**如何做的：** 在已有 C++ worker 的 SQLite 事务里同时保存结果及 `push_outbox` 消息，复用现有 state 表与索引，不新增消息中间件或识别模型。五类产生结果的操作为 association observe/correct/rebuild、trajectory rebuild、point trajectory rebuild。Go 独立发送器按提交顺序自动 POST；2xx 后 ack 删除队列项，失败保留并有界退避，重启读取未确认项继续发送。事件带持久化流 ID/递增序号；查询和配置不入队。新增 `GET /v1/push/status` 区分排队启用、URL 配置与待发送数量。

**踩坑和处理：** 原接口仅同步回复，没有后端主动接收链路。把 HTTP 回复后保存消息会留下结果已提交但消息丢失的窗口，现改为核心同事务写入，入队失败回滚结果；专项通过 SQLite trigger 注入失败验证。后端提交后回执丢失会重发，因此接收方必须按 event_id 去重，按目标/版本幂等更新；生产者重复重建可能产生新的消息 ID，不能仅按消息 ID防重复业务更新。Go 布尔参数不能写成 `-push-results true` 再跟地址，这会把 true 当位置参数并忽略后续配置；现启动时拒绝多余位置参数，正确写法是 `-push-results` 或 `-push-results=true`。首次误写参数的隔离测试日志保留，不作为通过证据。

**经验与交接：** 直接推送是传输行为，不等于同人/拓扑准确率验收。用户明确后端目前没有接收接口，本次未修改生产 Java 或数据库，也未配置猜测地址。可先用 `-push-results` 和独立持久状态文件排队，取得实际 URL 后以同一状态文件加 `-push-url` 重启；旧历史不会凭空补发。鉴权仅预留环境变量 `FACE_TRAJECTORY_PUSH_AUTHORIZATION`，真实合同尚待后端约定。最多 10,000 条待发送，满时新结果写入 503，查询继续可用；本次容量边界采用隔离计数注入，不是全场/满队列压力验收。

传输改造当次 worker/daemon 在 `cpp/build-push/Release`；本节保留当时证据，当前使用第12节的 build-consistency 成对产物。C++/Go构建、go vet通过；33项推送专项、24项五类操作/FIFO、已有23项真实Go→C++回归均通过。OpenAPI无重复键、23条路径/76个结构/227个引用解析通过，未运行完整OpenAPI校验器。完整接收协议、启动配置与按需查询表见[后端结果推送](docs/backend-push.md)；失败记录及哈希见[验证报告](evidence/2026-10-10-direct-push/REVIEW.md)。生产投递与业务接收验收待实际接收接口配置后完成，旧交付 ZIP 不含本轮改动。

## 11. 2026-10-10 剩余缺陷评估

**如何核查：** 读取当前C++/Go源码和已有证据，以build-push worker及独立合成SQLite复现，没有改算法或访问生产。详细结论和请求/返回见[剩余缺陷核查](evidence/2026-10-10-direct-push/REMAINING-DEFECTS.md)。

**当次审计确认的两处缺口（现已修复，见第12节）：** 全量重建缺少权威输入修订/expected_version保护，迟到的[A]能把已提交[A,C]的当前视图变回[A]并继续推送；旧完整版本仍保留。归属修正仅更新association，旧目标当前轨迹仍可返回被移走的观测，新目标没有自动生成轨迹，需调用方明确重建受影响目标，查询目前没有过期标记。

**踩坑和经验：** 单进程串行及相同输入幂等不能阻止业务旧请求覆盖新输入；算法轨迹版本号递增也不证明原始权威输入更新。同人修正推送成功不等于旧轨迹已重算。保留不可变历史与当前结果有效性是不同责任，需要修订前置条件和关联修订/过期保护，不能只按记录条数拦截合法撤回。

**交接：** 两处缺口在当次审计仅评估；后续修复及新证据见第12节。下一步接通真实同人/时钟合同、核定点位通路、配置实际推送接收器并做目标服务器长期容量验证。预测仍是保守可达集合，缺独立精度标签、经标定概率与无脸接续；当前采集截止审计不能还原实际消息到达历史。历史版本仍需留存/归档策略。2026-10-05吞吐报告是旧本机合成测量，不作当前生产容量结论。原80项传输/接口检查通过不覆盖这两种业务一致性情形；原证据及其当时哈希不改写。


## 12. 2026-10-10 版本与归属一致性修复

**独立服务器部署确认：** 已核对 CMake、Go 启动入口和 worker 子进程调用。C++ worker 与 Go daemon 同机运行，使用算法服务器自己的持久SQLite、场景/点位图/时钟配置；业务或采集端提交已有输入，算法按 push-url 向另一台服务器的后端主动 POST JSON，查询与预测按需调用。Java无需同机，时空核心无GPU推理依赖。当前仅验证Windows构建，Linux/其他架构须重新构建并做目标机验证；既有exe不能直接用于Linux。跨机接入须配置受控内网/鉴权网关，默认loopback仅本机可访问；单状态库单权威写入实例。此次仅确认架构并补充交接，未部署或修改服务器。

升级保护作用于后续写入，不能自动恢复此前已被旧缺陷覆盖的完整记录。已有状态需核对外部权威资料后按新前置版本重放；升级前缺少新绑定字段的积压消息仅作历史审计，不应直接覆盖当前业务视图。本轮未升级实际业务状态库。

**如何做的：** 先用旧 build-push worker、独立合成 SQLite 重现两处缺陷，六项断言均失败；随后修改现有 C++/Go，未新增识别或跟踪依赖。三类完整重建增加 expected_version，已有目标的改变输入须基于当前轨迹版本；不一致返回409 / VERSION_CONFLICT，拒绝写入不推送。同当前输入和归属绑定完全相同的回执丢失重试仍幂等；明确基于最新版本的合法撤回可减少或清空记录。

每目标持久保存关联修订，轨迹快照保存构建时 association_revision。新确认观测或归属修正使受影响当前快照失效，当前 get/point get/timeline/predict/geometry 返回409 / REBUILD_REQUIRED，历史明确版本保持可读并标明 freshness。observe/correct 推送 trajectory_effects，列出需重建的旧目标/新目标及最新轨迹版本；调用方按需 association_rebuild。对本地关联管理的目标，point/raw 输入与当前关联 Sightings 不一致时拒绝 SOURCE_CONFLICT。错误返回 conflict 元数据，便于核对当前版本。SQLite事务、推送和回滚绑定保持一致。

**踩坑与处理：** 单进程串行和相同输入幂等不足以保护旧完整请求；采用版本前置条件，不按观测条数拦截。全局关联版本会使无关人员失效，因此改用逐目标修订。归属修正后只更新前端标记不能消除算法旧当前视图，因此在核心当前读取处阻断；历史快照不改写。持久 Get 只有一个冷缓存，跨版本使用时保留独立拷贝。旧数据库缺新字段时仅补解码默认0，关联目标采用稳定迁移基线1并要求一次真实重建，不能直接写入当前修订冒充重算。事务回滚后重新绑定读取器，验证了 outbox 故障后的热状态恢复。第一次新产物测试在编译完成前启动，FileNotFoundError 不作为算法结果；等待构建完成后重新验证。

**成熟方案评估：** 按用户提供的 supervision 仓库及官方文档核查了单视频 ByteTrack、二维 PolygonZone 和版本弃用说明。它们可用于未来连续视频采集端局部跟踪/区域事件，需要实际帧、检测框和区域规则；不能修复本轮完整集合覆盖与跨目标归属状态问题，因此未安装或引入。许可、文档来源和适用范围见[评估记录](evidence/2026-10-10-consistency-fix/SUPERVISION.md)。

| 本轮最终验证 | 结果 |
| --- | --- |
| C++ Release、Go build、go vet、查看端JS语法 | 通过；新成对产物为 cpp/build-consistency/Release |
| 原六项失败反例 | 修复后6项通过；拒绝旧集合、保留最新观测、拒绝写入不推送、当前失效、历史保留、显式重建恢复 |
| 边界、重启、故障回滚、旧形状迁移、内存模式 | 66项通过；包括新确认观测失效与无关目标不失效 |
| 真实Go→C++ HTTP冲突与并发 | 14项通过；两个不同输入使用同一基础版本仅一个提交，另一个409 |
| 原推送/五种操作FIFO/既有HTTP回归 | 33/24/23项通过；全部写到新证据目录，旧报告不改写 |

最终共166项检查通过，采用明确合成身份和时间；不作现场精度或负载结论。OpenAPI升至0.10.0；严格重复键、引用和协议字段检查见本轮manifest，未运行完整OpenAPI校验器。

**经验与交接：** 后端须同时维护目标轨迹版本和 current_association_revision，消费归属修正后标记相关业务轨迹待重建。推送 freshness 仅代表提交时状态，积压的旧结果不能覆盖修正后的业务状态。409后需核对并重新准备完整权威输入，不能只更新 expected_version 再重发旧资料。直接嵌入C++核心的宿主须绑定 SetAssociationRevisionReader 并提供完整当前关联记录，worker已默认绑定。现有本地查看提交端已传前置版本，但运行进程未重启或替换。

最新构建和复跑方法见[修复报告](evidence/2026-10-10-consistency-fix/REVIEW.md)，API及升级见[一致性合同](docs/consistency.md)。用户明确实际推送接收接口暂时没有，继续保持可配置持久队列；生产Java/数据库/相机/模型未修改，旧ZIP未重打包。真实同人/时钟合同、现场通路、独立精度标签、长期留存与全场容量仍需资料和独立验收；本次没有新增无脸跨镜ReID或身份概率模型。持续4xx仍按原FIFO保留并阻塞后续，需监控和修正接收配置，没有擅自丢弃事件。

## 13. 2026-10-10 模型完成后的接入条件复核

**结论：** 可以等场地三维模型完成后接入新场景，现有时空核心已有版本化接入能力；不能据此认定只差模型或生产上线已就绪。同人数据、时钟合同与后端接收联调可以先做。当前能力是摄像头经过序列和条件可达范围，不保证精确人物坐标、唯一真实路线或经标定的概率。

**如何核查：** 对照当前交接、模型更新/一致性/推送合同、2026-10-10修复证据，并检索C++版本与归属保护、Go错误映射及FIFO重试代码。两处已复现的一致性缺陷已有本地修复；本次未重新运行测试、未做全面源码审计、未访问生产，不能宣称不存在其他缺陷。最新成对Windows产物已存在，运行进程及实际状态库仍未升级。

| 剩余条件或短板 | 接手处理 |
| --- | --- |
| 真实同人及时间未接通；保留的30条元数据仍为HOLD | 明确稳定人员/事件ID、已有归属证据、抓拍字段语义、时钟误差与有效期，核实后重放；不为联调伪造确认状态 |
| 完成三维建模不等于真实通路完整；旧模型图仍未核定 | 新模型提供完整发布场景、坐标系/变换、设备绑定及源资源；核定可通行边、方向、门禁/开放限制及通行时间；保留点位冲突和未知连接 |
| 任意新模型自动下载及几何重建尚未实现 | 使用该版本真实几何重新生成NavMesh、诊断建图并登记新版本；不能只改旧网格的版本号；按最新版本前置条件重放所有使用旧图的目标 |
| 缺独立现场精度标签；稀疏抓拍和分叉使候选可能扩大，无脸跨镜接续未实现 | 用独立确认的同人点位序列验收漏拍、停留、迟到、归属修正；冻结预测后对照后续事件，统计漏掉真实后续点位及候选范围大小，不使用预设路线循环验证 |
| 实际后端接收器、目标系统构建与长期运行未验收 | 完成真实POST接收、去重、双版本消费和409重建联调；在目标系统验证构建/恢复、实际峰值及多人历史；确定归档备份与队列监控，持续4xx会阻塞后续投递 |

**踩坑、处理与经验：** 模型画面能展示、旧局部网格能查询、合成检查通过分别只证明对应层能力；无法替代新模型来源、真实身份/时钟及现场精度证据。模型变化后保留旧证据，用完整权威输入生成新结果；缺边或搜索截断不得解释为人员不可能经过。

**交接顺序：** 等待建模期间先确定同人/时间输入和后端接收合同；新模型完成后按[模型更新流程](docs/model-updates.md)重建场景与点位图，再用完整权威记录联调。先做少量已核定点位与真实人员试点，再在目标服务器按实际负载验收。局部试点通过不自动代表全场可用。

## 14. 用户及协作方需补充的资料

**当前先提供（同人及时间项已在第15节更新安排）：**

1. **已有同人/抓拍数据（后端或识别供应方）：** 数据来源与接口说明；少量脱敏的原始JSON或表记录，包含同一人跨不同相机的实际经过，以及未确认或修正记录（如有）。说明事件唯一ID、设备/通道ID、稳定人员ID、确认/候选状态、归属证据在哪里；记录迟到、归属修正和撤回怎样通知，完整人员记录集怎样读取。只有实名名单或图片地址不足以证明跨相机同人。已有同人结果可用时不要求另提特征。
2. **抓拍时间说明（采集/后端方）：** image_time、utc_time等实际字段的生成方、是否抓拍时间、时区、精度；相机与可信参考时间的偏差、校准测量来源、有效时段及误差范围。有原始读时/同步日志可先提供；没有可信校准则明确未知，继续HOLD，不猜零偏差。
3. **建模交付约定（建模方）：** 计划交付格式、预计完成时间，以及能否提供完整场景、资源清单、发布版本、坐标单位/原点/轴向/变换、相机标记到真实设备/通道的绑定。可先给导出样例，算法侧负责格式适配与建图，不要求协作方先制作算法JSON。

**模型完成时提供：** 可读取的模型文件或受控只读资源入口、完整资源与场景清单及正式版本；设备点位绑定；门/楼梯/楼层连接、围栏或封闭区域、单向规则与开放时段等实际通行资料。未绑定或冲突点位注明待核定，不能按名称猜绑定；提供相机点位与通行关系即可，不要求逐米人物脚步路径。模型无法表达的规则可用表格补充。

**试点和上线前提供：**

- 后端实际JSON接收URL、接口负责人、鉴权方式、业务去重/版本更新及归属修正处理合同；暂无接口可以先约定协议，不要求立即提供凭据。
- 算法服务器OS/架构、CPU/内存/可用磁盘、跨机网络条件、预估或实测平均/峰值事件率、人员规模与历史保留时长；约定可接受延迟、候选范围和错误指标。
- 独立确认的真实同人摄像头经过序列，含时间和确认依据，尽量覆盖漏拍、分叉、停留及修正；不以算法输出或模型预设路线当真值。先小范围试点，不要求首次补齐全场标签。

**如何整理、经验与交接：** 本清单从当前点位、时间、模型更新和一致性合同提取，仅更新文档，未变更算法或生产。先使用业务原始格式和字段说明；脱敏时保持跨记录ID映射稳定。未知信息明确写“未知/待确认”，由接入端保留原始记录及待核验状态；算法侧完成字段映射、误差处理、版本建图和验证，不能把业务接入资料缺口转成猜测的确认身份或时间。

## 15. 2026-10-10 同人测试分工与摄像头时间容差确认

**用户已明确：** 同人数据后续由同事测试，暂不要求用户重复准备同人验收资料。经过事件直接使用摄像头抓拍时间；允许每台相机相对可信共同参考不超过60秒的偏差，超过60秒（包括半小时级偏差）不能进入确认时间轨迹。使用摄像头原值作为名义时刻，做必要的明确时区表示转换；零校正不表示零偏差，误差区间必须保留。时间门槛不能提升未确认身份。

**已复用的数据来源：** 用户指定聊天 `codex://threads/01a0e6d6-5d18-7cd1-9560-f6478dfcf019` 已通过read_thread读取；主要包含2026-09-28设备清单、算法能力/启用核验及后续可访问性/凭据操作记录，可用于设备身份线索，不含可据以认证当前抓拍误差上界的测量。没有向该聊天发送消息。现有工作区资料不要求用户重新提供。

**现有时间证据边界：** 本地2026-10-07诊断先有约47～48秒、后有约250～251秒的相机相对服务器时差，参考端变化未定位；详见[只读复核](evidence/2026-10-07-reassessment/REVIEW.md)。这些旧诊断不能认定当前设备符合60秒门槛，也不能据此批量平移历史记录。先前image_time/utc_time样本存在显式Z与疑似本地时间表达冲突；用户确认采用摄像头时间后仍须从采集字段映射明确实际设备时间和时区，不能对带Z字符串擅自减八小时。

**接入处理与踩坑：** 名义摄像头时间附带经核实的误差区间，顺序区间重叠则保留歧义；两台相机各允许±60秒时，相对误差最坏可达120秒，不强行按字符串先后拼接。可信测量表明超过60秒或误差上界未知时保留原始记录并HOLD，后续核验再重放。门槛应覆盖偏差、测量/漂移误差及时间量化误差；不能只看校正后的残差，让原本超限的相机通过。

**实现及交接状态：** 本轮只确认规则、读取聊天/本地证据及更新文档，未改算法、未重跑测试、未访问相机或生产。现有TrajectoryOptions/AssociationPolicy默认时间误差上限仍为1000ms，接入时须显式配置60000ms；单独提高该参数不构成设备原始偏差超限的独立检查。直接沿用名义设备时间时，时间适配配置的零校正须有来源说明，uncertainty包含该设备实际偏差及量化误差，且总界不超过60000ms；无核验来源继续使用未验证配置。正式数据接入后验证60秒边界、超限HOLD、区间重叠和未知时钟处理，不把当前文档更新当作已部署60秒策略。

**后续资料：** 当前优先确认建模交付格式及相机设备ID绑定；同人测试按同事进度接续，时间测量优先复用已有记录或后续只读核验。后端接收及目标服务器资料仍在联调/上线阶段补齐。

## 16. 2026-10-10 竣工图读取、局部放大及CAD显示问题处理

**结果：** 用户提供的竣工图RAR含9张DWG，已离线提取、核对来源哈希，用本机AutoCAD原生DXFOUT完成9张转换。生成[矢量查看页](evidence/2026-10-10-asbuilt/preview/viewer.html)，包含全部图纸及3个入口局部，支持放大、拖动和恢复文字候选搜索；另有4000×3000局部PNG。原压缩包及9张DWG哈希保持一致，无生产改动、无图纸上传。用户暂不需要自行重装CAD或重新导出。

**如何做、踩坑及处理：** 使用ezdxf读取普通CAD图元，按区域重绘并在渲染副本中替换黑体解决缺字；关闭自动调整纸张尺寸，SVG裁掉区域外图元。1264个天正等代理对象的图形缓存只有8字节头，单纯放大无法恢复，转用官方LibreDWG读取原始数据并保存字串候选、源句柄、图层及位偏移。候选还包含内部属性及可能误识别内容，不能当作完整还原。部分插座/通信编号不能混作摄像头编号；块插入点还存在大偏移，已记录变换后符号包围盒。失败.NET导出及初始模糊预览保留在attempts，未算成功产物。

**经验与交接边界：** 当前可精细检查普通图元中的房间、门、楼梯和监控符号；天正专用标注位置、箭头及排版仍未完整恢复。图内日期涉及2017/2018，单位未指定，同层多个分图及图例需去重。监控电气线不是人员通路；尚未形成已验证拓扑或后台设备绑定，不能直接投入确认轨迹。先整理带来源的候选关系，后与当前模型及现场核对。摄像头60秒容差和同人测试分工沿用第15节，未因本轮CAD读取改变实现。

完整方法、工具、坑、复现及核验见[图纸复核报告](evidence/2026-10-10-asbuilt/REVIEW.md)。解析工具放在临时目录，未纳入算法运行依赖。浏览器工具拒绝file协议，本轮未绕过；HTML脚本语法和12个SVG静态结构已检查，局部PNG经视觉核查，查看页实际浏览器交互仍未验收。

## 17. 2026-10-10 摄像头时间60秒规则已实现

**当前结果：** 第15节的规则已从文档落实到0.11.0本地算法。点位重建固定使用CAMERA_CAPTURE_60S，保留摄像头名义抓拍时间；偏差绝对值、测量/漂移和量化误差之和不超过60000ms才准入。超过60秒、半小时偏差、未核验、过期、时间非法或校准解释不唯一均HOLD。点位入口默认60000ms，可收紧，不允许提高上限或切回旧校正模式绕过。已构建成对产物`cpp/build-clock-policy/Release`，未替换运行中服务/真实状态库，旧ZIP未重打包。

**如何做及踩坑：** 先用旧产物复现半小时偏差被校正成零残差后NORMALIZED的问题；在ClockAdapter增加明确策略，并由点位重建入口强制调用。保留真实offset证据、applied_offset_ms=0及新误差上界，避免把offset伪填成0。对大整数先检查后取绝对值/相加。独立clocks/normalize默认CORRECTED_UTC保留兼容用途；现场预检须明确传新策略，底层已认证sighting接口仍由调用方负责原始时间认证。

**验证：** C++ Release、Go build、go vet通过。真实本机Go→C++接口75项、旧时钟契约51项、可达检查18项（含1248组独立枚举）通过，共144项。两台相机各±60秒时，相隔120秒仍保留顺序歧义；重叠连接保持UNKNOWN且没有primary路线，预测保留所有可能最后相机。旧可达套件首次缺少expected_version被409合同拒绝，内存适配当前版本字段后通过，未修改旧断言/独立oracle或覆盖旧证据。

**经验与接手：** 校正后的残差不能替代设备原始偏差门槛，60秒容差不能替代真实核验或提升身份状态。按[时间合同](docs/clock-adapter.md)提供有来源及有效期的真实profile；同人仍由同事测试。旧快照保留历史，按最新expected_version和完整权威记录重放；内部关联来源不一致则保持SOURCE_CONFLICT，先核对资料。图纸继续用于候选通路/点位核查，当前不作为已验证拓扑。算法、现场精度、三维接入和部署仍分别验收。

方法、修复前反例、复跑命令、坑及SHA见[实现报告](evidence/2026-10-10-clock-policy/REVIEW.md)。OpenAPI升至0.11.0；本轮只做严格重复键/引用和新增门槛字段静态校核，未运行完整规范验证器。

## 18. 2026-10-10 人脸实际比对、三类人员与闸机监控

**当前范围：** 用户已确认ID+重点/普通标记+照片，未入库人员为第三类也建轨迹；闸机仅验票，出场事件需有明确人脸绑定才关闭该人监控，漏入口仍启动且标记UNKNOWN。P1自行拉流/接海康抓拍/后端图片并统一质量和特征，重点优先不改变确认身份标准。人脸库与场地模型后续提供；本轮实现本地算法接续，没有接管生产摄像头/Java/数据库或模型。

**如何做的：** C++ MonitoringStore新增版本化人员类别、原子多模板登记、不可变监控/闸机事件及可恢复投影。Go/OpenAPI0.12提供persons、observe、gate-event、get四个接口。monitoring/observe强制复用CAMERA_CAPTURE_60S，缺时间也持久保存HOLD，不调用关联、不创建匿名模板；合格时间与人脸比对后在同一SQLite事务用完整当前归属重建受影响轨迹。原CAS、历史版本、关联修订、错误回滚和可选结果推送保留。

**会话与乱序：** ENTRY/EXIT必须绑定已确认的人脸观测并带独立来源证据，单独票号/时间接近不做身份判断。未绑定票据、闸机先到脸后到、后补明确绑定均有状态。漏入口合格脸激活UNKNOWN入口，明确出闸CLOSED，后来再次出现可重开；未出现不等于出场。误差重叠保持AMBIGUOUS/继续监控，迟到旧证据按完整时间投影重放。已绑定的脸和gate时间分离时保留两项，避免后补EXIT抹掉原来的入场观察；同次时间重叠可合并表达，避免把EXIT脸立即视作再入场。

**回执与性能处理：** observe只返回监控紧凑摘要与轨迹版本引用，完整内容按版本get。回执receipt_scope=ORIGINAL_TRANSACTION，同ID重试可能返回历史状态；当前状态get查询，P1调度比较三项修订水位，不让旧ACTIVE覆盖新EXIT。长历史实测后将快照改为refs-v2不可变共享序列，原始输入只存一次；camera/frame/track派生索引同事务维护。可信纯新增证明允许单visit或满足严格尾部证书的多visit追加；更新incoming主/备选末节点边界，首时间范围变化、迟到、纠错或换图仍走完整计算。缓存与持久索引均覆盖回滚/重启，所有旧版本与原始记录保留。

**最终验证与容量边界：** 320项既有/新接口回归、824项对未改动完整核心逐版本对照、9项旧库迁移全部通过，另70组真实HTTP/OpenAPI实例通过。最终worker SHA256为`46725cf0f363afadf3c610af3bcf9ed823384d5731a475d0da1836d61698df4b`。同一1000条单相机合成输入末100条P50/P95由330.33/368.68ms降至30.20/80.97ms，完整SQLite页由72.25MB降至38.78MB；400条40次跨镜visit末段为48.27/130.76ms，复杂回退和跨镜身份约束仍随历史增长。计入object共享对象及全部namespace，没有只报快照根大小。详见[性能报告](evidence/2026-10-10-monitoring/p2/PERFORMANCE.md)。

**部署需求已确认：** 约600路，3090优先、低负载允许CPU；结果期望2秒、3秒可接受。上述是本机单序列逻辑/存储测量，不是600路、多人员高峰或3090容量证明。须结合原生抓拍比例、连续流路数、峰值人脸事件率、图库规模和现场完整链路验收；没有通过降低身份门槛、减少历史版本或漏掉自动轨迹来获得速度。旧refs-v1可读，但旧程序不认识refs-v2；升级前备份，回退必须恢复匹配的旧状态库，不能只换回旧exe。

**踩坑与经验：** 已注册但未观察的人查询应为NOT_STARTED；HOLD association为null需写入OpenAPI nullable并由消费端显式处理。人员类别、模板和轨迹分开提交会留部分成功，现同事务。模型维度一致不能替代特征契约；全库竞争和不同人员margin必须保留，不能只搜重点库就确认。旧history回执不是当前状态。只把观察绑定为gate却删除其原始时间会改变会话开始，现区分相交与分离区间。

**交接：** 新成对构建cpp/build-monitoring/Release；[监控合同](docs/monitoring.md)、[P1运行步骤](../face-recognition-engine/docs/monitoring-runtime.md)、[P2验证报告](evidence/2026-10-10-monitoring/p2/REVIEW.md)。源码/接口/本地照片与模拟输入验证同现场精度分开；真实图库标注由同事提供测试，时钟需可靠profile，进出场人脸绑定需独立证据。场地模型未接时只保留相机经过序列和未核定候选，不把电气图或相机连线当详细路线。本轮未替换在运行服务，旧ZIP未更新。

最终联接复核：最终P1公平接纳及本节worker一起重跑，真实照片/CPU模型/HTTP整链27项通过、模糊视频主码补图与海康Digest整链21项通过，见[P1当前交接第9节](../face-recognition-engine/HANDOFF.md)。P1新增相机完成处理间隔统计，600路调度仅完成虚拟时钟模拟；不把帧龄低、逻辑配置600项或单序列P2性能当作全场2–3秒验收。

## 19. 2026-10-10 业务服务器实查及语言边界纠正

用户提供211.139.12.141:22并要求自行查看，已成功SSH只读检查：Ubuntu22.04.5，双Xeon Silver4310，24物理核/48逻辑线程，内存62.35GiB、当时可用32.04GiB，未发现计算GPU。5秒CPU忙碌约1.57%，不代表高峰余量；内网千兆、公网百兆。515个历史已知地址中，499个RTSP端口可连接，500个至少HTTP/RTSP其一可连接，15个两端口均未连通；47个失败端口提高超时复核未恢复。没有登录摄像头、拉流、采集新图片、运行模型或修改服务/数据库/场地模型，10/20/30网段只做TCP连接。

P2仍是C++17核心+Go HTTP，并未改成Python。此前新增的P1人脸检测/特征、拉流/调度/持久投递确实是Python运行层，不只是测试代码；用户要求正式算法沿用C++/Go，故完整生产链路迁移仍是待办，不能以本地P1+P2接线验证替代语言架构完成。该服务器可评估承担抓拍接入及C++时空处理，CPU人脸吞吐、GPU部署路径及600路2–3秒容量没有验收。

方法、踩坑、网络分组、依赖现状、迁移范围和原始证据统一见[P1唯一交接第10节](../face-recognition-engine/HANDOFF.md)及[实查摘要](../face-recognition-engine/evidence/2026-10-10-business-server-readonly/summary.json)。本次仅新增只读核查脚本/本地证据并纠正文档，C++/Go/Python算法运行源码和已验收二进制没有改动。

## 20. 2026-10-10 人脸模型选型责任及特征合同边界

用户已确认由Codex评估成熟预训练人脸模型并验证，效果不足再考虑微调，不再以用户自行训练为前提。P1完成CPU/CUDA阶段计时、C++原生推理对照和OpenCV Zoo YuNet/SFace首批离线筛查，方法、问题修正、来源和结果见[P1交接第11节](../face-recognition-engine/HANDOFF.md)及[证据报告](../face-recognition-engine/evidence/2026-10-10-inference-performance/REPORT.md)。

P2当前gallery.hpp仍固定512D；候选SFace实测128D，因此未接入本P2图库、未替换正式模型。若采用须明确版本化维度/特征空间、C++/Go合同与索引迁移，并从原始登记照片重建模板；不能补零后沿用旧阈值，也不能将不同模型空间混搜。此轮未修改P2核心源码、二进制、持久数据或现场服务。候选速度、数值对照、无标签检测计数均不能替代同人/异人识别验收和3090全链路容量；纯C++/Go P1迁移仍为待办。经验是先冻结模型与特征合同，再做语言迁移和全链等价验证，避免同时换网络和实现而失去回归基准。

用户随后明确准确性优先。当前保留SCRFD10g/ArcFace512研究基线，不因SFace更快就替换或把512降128。两条实际推理复核SFace输出[1,128]；经业务机取得的真实海康图片是JPEG像素，向量由P1模型产生。PCA后的ArcFace128与SFace128仍为不同空间，不能直接混搜。已准备111条历史照片人工核验队列，标签不从自动GID生成；本轮P2合同与二进制未改。方法、现场GET边界及准确性方案见[P1交接第12节](../face-recognition-engine/HANDOFF.md)和[决策证据](../face-recognition-engine/evidence/2026-10-10-feature-dimension/DECISION.md)。

## 21. 2026-10-10 服务器已有抓拍及来源一致性

全量只读台账查询得到853条摄像头通道、168条抓拍设备，TDengine vcl_face含128个历史来源；最近24小时按utc_time复查8个来源1167条，确认抓拍仍在进入服务器。每个历史来源最新脸图/背景图共256个当前服务路径HTTP200且JPEG尺寸可读，实际位置为gat/gallery/face、服务端口9001。已有图片链路可复用，无需先拉全部视频。

需要保留关联/时间边界：31个历史来源不在当前camera表，7条抓拍绑定悬空；不得猜摄像头ID。image_time/utc_time有约8小时语义差异，即使按本地UTC+8假设换算，近期8源最新样本中7个仍差约193～240秒，不能冒充已通过≤60秒校验，也不能直接用入库时间替代采集时间。P2仍保持现有时间资格与HOLD规则，本轮未修改或部署核心。方法、问题处理、全部清单和经验统一见[P1交接第13节](../face-recognition-engine/HANDOFF.md)及[抓拍核查报告](../face-recognition-engine/evidence/2026-10-10-camera-server-survey/REPORT.md)。台账核查、图片可读和持续入库均不替代识别精度、真实时钟及容量验收。

## 22. 2026-10-10 原生客流数据与设备时间复核

只读直连58台摄像头，27台已读到当日进入/离开报表（26台启用、1台关闭）；原生聚合人数与人脸身份、闸机验票结果分别建模，不用人数差推断具体人员进出。设备均返回当天24个小时桶，计数重复拉取不能重复加总，每日重置时间为00:00:00+08:00。服务器ccic_media_master.flow_camera实际可读，但30个非空来源各仅1条，不能作为完整历史事件流。

58台直接读取的设备时间中，48台相对业务服务器的保守偏差区间超过60秒；10台只通过当次快照比较。没有修改校时、关联或HOLD门槛。完整数据、853条通道覆盖、对应的失败处理与接手说明统一见[P1交接第14节](../face-recognition-engine/HANDOFF.md)和[计数核查报告](../face-recognition-engine/evidence/2026-10-10-people-counting/REPORT.md)。本轮没有改动P2核心/合同、现场服务或数据库。

## 23. 2026-10-10 摄像头能力清单及直读计数证明

新增[摄像头能力清单 Excel](../outputs/01a1239f-6341-75f1-9b1e-4cb7656edcf5/摄像头能力清单-20261010.xlsx)。509个已知IP中58台本次读取设备信息；其余422个401、27个未连通及2个其他HTTP响应，保留待核验。27台人数报表均通过业务机直接请求摄像头接口取得，socket对端IP与请求/响应URL中的相机IP一致，26台计数开启。平台注册通道表另外标识服务器历史上报，二者没有混用。24台声明独立人脸抓拍、3台混合目标能力；能力、当前开关、报表读取、平台映射、码流与时间门槛逐项分列。

时间按每请求前后时刻与1秒时间字段粒度复核，48台超过±60秒，不能直接接受为轨迹时序依据；10台仅当前快照满足，未校时或放宽门槛。聚合人数是人次，不绑定人员ID，也不是现场占用量或人脸识别精度验收。多输入通道逐一检查，避免单通道遗漏；401不作为不支持、status=counting不作为有可读报表、型号不作为双目物理结构结论。表格四页渲染及导出数据校验完成；未修改C++/Go算法、生产Java/数据库/设备，未部署轮询服务。完整方法、踩坑和交接见[人脸引擎交接第15节](../face-recognition-engine/HANDOFF.md#15-2026-10-10-逐台能力清单与设备计数来源复核)与[本轮证据清单](../face-recognition-engine/evidence/2026-10-10-camera-capabilities/manifest.json)。

## 24. 2026-10-10 设备清单补齐与时钟准入交接

最新[补充核验 Excel](../outputs/01a1239f-6341-75f1-9b1e-4cb7656edcf5/摄像头能力清单-20261010-补充核验.xlsx)覆盖509个IP，原451个待核验地址用用户指定凭据只读复查后新增415台，合计473台设备信息可读。剩余7台401、27台未连通、404/301各1台，逐项保留原因。共59台声明人脸抓拍、27台设备计数报表可读，身份识别准确率和所有推送链路不在这次能力验证结论内。平台注册通道853条与设备IP不是同一口径，仍仅22条有可靠映射。

前项校时工作仅修改原已核验1/2/3网段50台NTP配置，58台最后三次采样均≤60秒，但10台在中间复查出现约4分钟回跳，不能据最后合格快照关闭问题。新增415台位于受保护10/20/30网段，实读均快约252～254秒，保持只读；超过60秒的摄像头时间不能直接用于轨迹时序准入。用户已交后端查询其他平台重复授时，本任务保留回跳证据，不扩大写入。需在冲突消除后重新复核持续时间稳定性；这次未改变C++/Go算法或放宽60秒门槛。

校时前后839项非时间能力/规则/码流配置对比一致，58台均可读、27台计数报表仍可读。服务器39个已知设备连接继续收到ISUP字节，5个抓拍来源有新增图、40个最新图片URL可访问；这些只证明相应观察窗口的连接和抓拍接收。未完成所有校时设备逐个推送映射、视频解码或新增服务器计数记录验证。完整方法、原始配置备份、坑点与后续交接见[人脸引擎交接第16节](../face-recognition-engine/HANDOFF.md#16-2026-10-10-待核验设备补齐校时与传输复核)。

## 25. 2026-10-10 活动作用域与7天滚动轨迹：实施前只读核对（历史快照）

用户明确：算法只对应指定活动；前端/后端创建新活动后要有其自己的闸机、摄像头与人员轨迹上下文，不能混算全部活动。轨迹滚动保留最近7天，按采集时间168小时窗口，不等活动结束重新计时。此次仅授权服务器查询，未改生产、未清理历史数据，也未改本地算法运行源码。

实查后端18个活动，security_activity.id与唯一activity_num映射，venue_id全部匹配security_place.place_id。重点人员和重点视频按activity_id关联，验票TD表按activity_num分区。31253条验票是所有活动总数，AT1012仅2379条；主表之外的1/123/003标签共67条需隔离。验票表无闸机ID、进出方向，图片列非空为0；具有direction/device_id/图片的门禁告警表目前0条。不能把票据或场地客流直接绑定为某人出闸。已实际验证带activityNum的只读分页查询空窗口HTTP200；网关鉴权和算法结果接收协议未验收。

场地共享配置有13个未删除出入口、44条摄像头绑定，其中13条在当前camera表找不到。AT1012所在场地5个出入口、33条绑定集中在1个入口，其余4个无绑定。场地805个type=3设备不等于该活动应启用805路。后端FlowGate查询按场地和type<>2关联活动，未给出显式每活动设备快照；AT1001与AT1002有同场地时间重叠，不能按场地/时间/最新活动自动猜归属。新活动要经权威活动上下文、明确启停及版本化设备绑定接入；计划结束时间不能自动替代实际结束指令。

**代码审查结论：** 现有0.12 monitoring合同无activity_id；cpp/src/monitoring.cpp的人员投影与monitor:target:event会话在当前状态库内未按活动隔离。P1/P2也没有完整7天轨迹清理，短帧TTL/调度优先TTL不算。正式交付待补：activity_id贯穿观测、闸机、关联/匿名ID、会话、轨迹版本、查询/回执/幂等及活动人员分类；A活动EXIT不得影响B活动。共享身份图库与活动内监控状态需明确分离。

保留期实施须覆盖旧版本、refs-v2共享对象、缓存索引、投递队列和重放来源，保证最近7天仍可用、过期轨迹不会因迟到/重试复活、进程重启后策略仍生效。不能只在查询时过滤，更不能误删仍被窗口内轨迹引用的节点。人脸登记库、活动主表、相机录像及后端验票历史不随轨迹7天规则删除。跨活动并发、相机复用、活动变更、迟到EXIT、7天边界、重放与磁盘回收等验收待实现后执行。

完整查询、接口字段、坑点、经验及交接见[P1交接第17节](../face-recognition-engine/HANDOFF.md#17-2026-10-10-闸机后端只读查询活动隔离及7天轨迹要求)和[只读证据摘要](../face-recognition-engine/evidence/2026-10-10-gate-backend-readonly/summary.json)。本节优先于早期没有活动维度/保留时长的交付描述，不能将已确认需求称为已完成实现。

## 26. 2026-10-10 多活动隔离与168小时保留实施进度（阶段1历史快照，已由第27节更新）

**当前状态：** 阶段1已实现并通过本地验证；阶段2授权、可信UTC接入、查询/订阅实施中；阶段3全存储滚动清理未实现；阶段4最终文档同步及逐条验收尚未完成。目标仍为原完整需求，不能将阶段1通过当成全部任务完成。生产、Java、生产数据库和摄像头没有写入或部署。

**统一设计：** [活动及保留合同](docs/activity-retention.md)先冻结活动作用域、持久化/幂等键及全部清理范围。activity_id是权威主键，activity_num唯一映射，不猜最新活动。创建默认停止，配置/绑定/启停分别CAS；计划结束不自动停。登记图库首版按活动显式同步，活动类别、关联、匿名模板、会话和轨迹独立；没有用拼接person_id冒充隔离。用户上下文与后台启停分开。

**阶段1实际实现：** C++ State用结构化活动namespace共享同一SQLite事务，各活动分别拥有Gallery/AssociationStore/TrajectoryStore/MonitoringStore和缓存。事件按activity/source/event幂等，同键异内容SOURCE_CONFLICT。过时配置/绑定、停止后迟到事件隔离，未知活动拒绝。旧缺活动状态保持未归属；新入口不允许旧裸UTC/任意raw重建绕过时间准入。匿名和会话标识采用不包含原事件内容的摘要，避免运行标识暴露旧采集历史。Go按活动轮转，单活动排队上限保留其它活动机会。P1仅传递活动字段、scope键和兼容隔离，没有重写推理、更换模型或新增Python清理。

**时间事实：** ClockAdapter确实要求不可变核验profile/来源/有效校准并使用CAMERA_CAPTURE_60S；误差包含abs(offset)+测量/漂移/量化，不超过60秒，未知和超限HOLD，重叠保留AMBIGUOUS。此前NTP实查只是历史快照，原代码没有持续可信当前UTC。新增核心仅接受Go专用管道的可信UTC印记并保持不回退水位；Go挑战签名/单调推进接入尚在阶段2实施，不能将当前system_clock视为可信参考。测试参考全部SYNTHETIC。

**本地验证：** VS2022 C++ Release通过；Go build、vet、test通过（阶段1源码）。真实C++/新建SQLite的[core-run-05](evidence/2026-10-10-activity-retention/core-run-05/summary.json)39项通过：活动创建、同人/同事件跨活动、不同来源同事件、内容冲突、共享设备规则、启停、迟到EXIT、时间HOLD/歧义、绑定修订、重启恢复、持久作用域及SHA256指纹。脚本[evidence/2026-10-10-activity-retention/verify_core.py](evidence/2026-10-10-activity-retention/verify_core.py)要求新的output目录。P1 11组字段/队列/兼容验证见[结果](../face-recognition-engine/evidence/2026-10-10-activity-retention/p1-scope-result.json)。Go fair_test验证活动轮转与单活动容量保护。不是Go+C++授权端到端验收，后者将在阶段2完成。

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
& 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe' --build cpp/build-activity --config Release
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' evidence/2026-10-10-activity-retention/verify_core.py --worker cpp/build-activity/Release/face_trajectory_worker.exe --output evidence/2026-10-10-activity-retention/core-new-run
Push-Location go
try { go build ./...; go vet ./...; go test ./... } finally { Pop-Location }
```

**踩坑及处理：** 工作区及两个项目没有Git仓库，改动前保存了55个相关文件副本/哈希，见[baseline](evidence/2026-10-10-activity-retention/before-manifest.json)，未动已有状态库。C++ auto多声明类型不同导致编译错误，拆开声明后构建通过。绑定新版本测试错误复用旧帧，触发正确的同帧身份冲突；使用独立新帧后通过，没有削弱比对门槛。State视图必须比根先析构，视图禁止自行Begin/Commit、物理管理操作不能逃逸scope。

**后续必须完成：** Go签名授权/可信UTC挑战、活动查询和订阅上下文、真实Go+C++端到端客户端；所有历史版本、object共享树、P1库、队列/缓存/重放清理以及dry-run/中断/重启/存储增长分项证据；最终OpenAPI/运行合同/示例/迁移回退与两个交接同步。当前只设计了168小时元数据及过期入口拒绝，没有物理删除实现，不能称7天清理已生效。

**四层证据边界：** 本地实现为阶段1已验证、后续实施中；后端联调和真实页面/登录/生产鉴权现场联调待完成；生产部署未执行；600路、3090优先、2秒目标/3秒可接受容量验收未执行。

## 27. 2026-10-10 多活动隔离及168小时滚动保留交付

### 实际范围与证据层次

正式实现是现有C++17核心、Go HTTP和内置SQLite，没有引入Python生产清理模块。P1 Python仅增加作用域字段、不可变投递及C++维护暂停/清缓存/重开兼容；没有更换模型或重写推理。改动前55个相关文件已保存副本和SHA，见[基线](evidence/2026-10-10-activity-retention/before-manifest.json)。现有运行库、Java、生产服务器/数据库、相机和三维资源未写入；本轮所有变更和删除验证均在新建本地隔离库，未对192.168.10/20/30网段写入。

| 层次 | 状态与限制 |
| --- | --- |
| 本地实现 | 活动核心、授权/查询/订阅/推送、168小时清理及恢复已实现；下面的重复脚本与证据为验收入口 |
| 后端联调 | 本地合成签名器和真实Go/C++客户端合同验证通过；真实登录/退出、页面切换、生产鉴权、UTC签发和结果接收器现场联调待完成 |
| 生产部署 | 未部署、未迁移、未开启生产清理；默认删除关闭 |
| 容量验收 | 未执行；约600路、3090优先、2秒目标/3秒可接受仍是设计约束 |

### 选择、授权与后台生命周期

创建、用户选择、监控启动是三件独立操作。创建默认停止；`activity_sync`、`activity_bindings`、`activity_monitoring`分别CAS，显式启动必须已有设备绑定。退出/关闭订阅/切换上下文只影响访问，不改变活动enabled，不按计划end_time自动停止。`security_activity.id`是scope主键，唯一activity_num和venue_id由可信后端同步，验票编号经该映射转换；编号/场地映射一旦建立不允许静默改绑。

State物理namespace是JSON结构 `["activity",activity_id,namespace]`；每活动Gallery、AssociationStore、TrajectoryStore、MonitoringStore及热缓存独立，共用根SQLite事务保证原子性。person_id保持后端原值，活动分类、匿名摘要、会话、轨迹版本与结果序列独立。登记身份库按活动显式同步；同一权威资料可主动同步多个活动，不隐式共享动态身份状态。幂等键至少为activity/source/event，原内容SHA保留；同内容原回执，异内容409，即使显式reconsider也不能改原事件。过期只保留不可还原轨迹的键/内容摘要，重试EXPIRED、异内容409。

共享设备必须有明确source/device/kind/route_rule和binding_version；缺失、错误归属、旧配置/绑定、新版本不匹配或停止后迟到新事件HOLD，未知活动拒绝，原已提交同内容重试仍安全。不会猜最新活动或按场地扇出。已准入闸机必须绑定同次通行已确认人脸，票据不能自报person。A EXIT不影响B，迟到旧EXIT不关闭后续会话。

Go接入队列按活动轮转，单活动排队32、全局128；持久推送单活动256、全局10000。容量检查只在真实新增push时进行且失败回滚，已提交幂等重试不会被满额预检误拒绝。推送选择各活动最早条目并轮转；失败活动独立1秒起、上限30秒退避，其它活动继续，保持活动内commit顺序。全局资源耗尽仍需容量告警；上述是公平保护的功能测试，不是现场吞吐证明。

Go所有业务HTTP校验可信后端Ed25519授权动作、session、context_id/generation、authz_revision与期限，覆盖配置/启停/查询/订阅/身份/输入；不信任前端裸activity_id。回包带活动身份/版本/client_context，订阅带同用户上下文和活动版本；前端切换时关闭旧订阅并丢弃不符回包，不能拿旧响应覆盖新活动。推送带活动身份、版本、期限及SHA Idempotency-Key，接收端仍须独立检查到期和最新业务版本。

### 可信时间与全存储清理

原ClockAdapter已有不可变verified profile、来源/校准有效期和CAMERA_CAPTURE_60S：名义相机采集时间，明确时区，`abs(offset)+测量/漂移/量化`总误差<=60000ms；未知/超限HOLD，误差区间重叠保留AMBIGUOUS。本轮新增服务器UTC挑战签名，Go验证权威issuer/key/challenge/source/evidence与有限lease后以单调计时推进，C++持久水位拒绝回退；没有把本机wall clock或历史NTP快照当可信当前UTC。Go冷启动需新challenge证明，C++子进程重启可沿用同一存活Go进程尚有效的单调lease。无有效证明时业务准入和清理失败关闭。

截止=`可信now-604800000ms`，已准入名义采集时间等于截止仍保留，早于截止删除。已知采集期限只从采集时间计算，不取接收时间较小值，不因重试/切换/停止重置。未知时间从首次可信隔离时间最多168小时；重新隔离/恢复不能延长。旧数据没有可信采集或原首次隔离证明时立即清除历史载荷，不能迁移时发放新窗口。比当前可信UTC晚超过60秒的采集HOLD，防止异常未来时间延长期限。

实际覆盖：每活动及未归属的observations、monitoring/gate及全部revision、intake内容/回执、association frame/track/summary索引、自动图库、所有trajectory历史refs-v1/v2与共享object/AVL、reentry/rejected/revision引用、结果通知、push outbox、重放来源及热缓存。历史版本按原版本号修剪重建，GC只回收不再被有效根引用的对象；保留版本水位，空旧版本删除，空当前头保留CAS语义。有效闸机自身采集仍在窗内时保留本次通行和当前已确认身份；过期脸图ID/绑定原文清除，只留证据hash，窗内人脸归属修正先更新再冻结。跨截止误差重叠通过散列依赖保留顺序歧义，不保存超窗时间/相机/事件原文。

最小monitoring_runtime只含必要状态、opaque会话/依赖摘要、入口未知和歧义标志，不含超窗轨迹点。登记人脸库、活动主表/分类、必要绑定/启停、clock/site/point图不按轨迹期限删除；后端验票历史及摄像头录像不在本地清理范围。

P1外部库由C++适配器按明确启动路径处理captures图像/metadata/result、deliveries payload/receipt/error、bridge frames metadata和outbox载荷；登记图库投递排除。C++使用同一ClockAdapter，不信任P1 UTC标签或主机created时间。维护marker要求P1停止接收、排空已准入操作、清空帧/结果/调度缓存、关闭SQLite释放lease，清理/截断完成后原HTTP服务重开；不配合旧进程显示BLOCKED而不冒险删。失败/kill后的marker与墓碑可重试恢复。跨P2/P1库和P1多库的过期绑定/重入依赖同样清理；被改写的未发送载荷进入QUARANTINED，不能以原ID发送变更内容。

核心dry_run在内存PlanningCopy执行完整修剪/GC，不更新源库任何列、水位、墓碑；报告计数是固定now下的精确计划。P1 dry-run读现有live WAL时用私有只读索引；关闭库在Windows持有禁止写共享句柄后才使用immutable读取，main/WAL/SHM/控制文件无改动，不能取得安全快照就BLOCKED。P1外库维护的非Windows冻结支持尚未验收并失败关闭。真正删除启用后定期执行，按期限到期的核心读取前也会清理；事务后secure_delete、checkpoint/TRUNCATE、VACUUM并再次截断，物理维护失败标PENDING、下一轮续做。

### 构建、重复验证与存储分项

从`D:\人脸算法`运行，VS2022 C++工具链、Go和已有两个测试Python环境是前提；测试目录必须新建，禁止把实际运行库传给脚本。正式构建命令：

```powershell
$activityCmake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
& $activityCmake -S face-trajectory/cpp -B face-trajectory/cpp/build-activity -G 'Visual Studio 17 2022' -A x64
& $activityCmake --build face-trajectory/cpp/build-activity --config Release
Push-Location face-trajectory/go
& 'C:\Program Files\Go\bin\go.exe' build ./...
& 'C:\Program Files\Go\bin\go.exe' vet ./...
& 'C:\Program Files\Go\bin\go.exe' test -v ./...
& 'C:\Program Files\Go\bin\go.exe' build -o ../evidence/2026-10-10-activity-retention/face-trajectoryd.exe ./cmd/face-trajectoryd
Pop-Location
$activityEvidence = 'face-trajectory/evidence/2026-10-10-activity-retention'
$activityWorker = 'face-trajectory/cpp/build-activity/Release/face_trajectory_worker.exe'
$activityPython = 'D:\人脸算法\.venv-ops\Scripts\python.exe'
& $activityPython "$activityEvidence/verify_core.py" --worker $activityWorker --output "$activityEvidence/recheck-core"
& $activityPython "$activityEvidence/verify_service.py" --worker $activityWorker --service "$activityEvidence/face-trajectoryd.exe" --output "$activityEvidence/recheck-service"
& $activityPython "$activityEvidence/verify_retention.py" --worker $activityWorker --output "$activityEvidence/recheck-retention"
& $activityPython "$activityEvidence/verify_retention_edges.py" --worker $activityWorker --output "$activityEvidence/recheck-edges"
& $activityPython "$activityEvidence/verify_outbox_fairness.py" --worker $activityWorker --output "$activityEvidence/recheck-outbox"
& $activityPython "$activityEvidence/verify_push_fairness.py" --worker $activityWorker --service "$activityEvidence/face-trajectoryd.exe" --output "$activityEvidence/recheck-push"
```

| 实测 | 证据入口 |
| --- | --- |
| 活动核心40项，含查询矛盾编号拒绝 | [core-final-03](evidence/2026-10-10-activity-retention/core-final-03/summary.json) |
| 真实Go→C++→隔离SQLite HTTP31项，含24次并发查询与定期清理 | [service-final-03](evidence/2026-10-10-activity-retention/service-final-03/summary.json) |
| 完整保留/真实事务中kill恢复/六轮存储81项 | [retention-final-03](evidence/2026-10-10-activity-retention/retention-final-03/summary.json) |
| 独立截止歧义、未来时间、有效gate依赖及归属修正、物理维护失败恢复45项 | [edges-final-03](evidence/2026-10-10-activity-retention/edges-final-03/summary.json) |
| A实际填满256配额后B接收及重复幂等5项 | [outbox-final-03](evidence/2026-10-10-activity-retention/outbox-final-03/summary.json) |
| 真实Go/C++/本地接收器A失败B继续、重启及scope顺序12项 | [push-final-04](evidence/2026-10-10-activity-retention/push-final-04/summary.json) |
| State视图/事务/管理权限、refs-v1/v2独立18项 | [State结果](evidence/2026-10-10-activity-retention/state-view-test/result.log)、[snapshot结果](evidence/2026-10-10-activity-retention/prune-test/result.json) |
| P1 scope/清理/真实维护/跨库依赖 | [P1第18节](../face-recognition-engine/HANDOFF.md#activity-p1-maintenance) |

State和snapshot专项驱动可用其目录的CMakeLists分别`-S/-B`、`--build --config Release`，运行Release exe并传**未存在的隔离SQLite绝对路径**；两者Windows入口明确转UTF8，支持当前中文目录。合同验证脚本、构建/Go日志、SHA和逐条要求映射见本目录最终证据清单；失败探索目录保留为诊断历史，不与final通过结果混用。

封存入口：[最终SHA/文件清单](evidence/2026-10-10-activity-retention/final-manifest.json)、[逐条要求与实测映射](evidence/2026-10-10-activity-retention/requirement-coverage.json)、[OpenAPI完整校验](evidence/2026-10-10-activity-retention/openapi-validation-final.json)、[Go最终结果](evidence/2026-10-10-activity-retention/go-final-summary.json)。`seal_evidence.py`只读取源码与报告、核对摘要及链接，不打开数据库或执行清理。

2026-10-12最终核对补齐非入流查询的可选activity_num一致性校验，矛盾编号返回SOURCE_CONFLICT，不忽略调用方自相矛盾的身份。OpenAPI实测覆盖52成功请求回包与10错误回包。失败历史单独保留：[验证历史](evidence/2026-10-10-activity-retention/validation-history.json)。P1维护首轮曾恢复断言失败，原报告未保存，当时8次复跑通过并增加诊断；后续同类故障的复现和修复见第28节。原封存SHA仍是当时快照，当前重建worker以第28节为准，不能用旧报告宣称新程序已完成全部现场验收。推送冷启动测试曾收到接近1秒上限的旧挑战并被正确拒绝，测试客户端改为等待新挑战及原子替换证明，没有放宽时间政策。

存储分项（合成固定fixture，详情以final报告为准）：到期查询、回执重放和恢复均不复活，所有SQL列及主库/WAL原字节检查通过；有效状态行214→154，逻辑227124→121278字节；逻辑删除后可复用180224字节，VACUUM后freelist为0；主库20480/WAL964112→主库221184/WAL0，不能只比较主库。六轮每轮12事件，清理后非墓碑逻辑约13556→13143字节；72条必要墓碑13464字节，主库40960→73728。证明轨迹载荷受约束，**不能宣称总库永不增长**；墓碑以及保留的登记/配置修订要单独纳入容量规划。

### 启动、新活动及观察清理

完整启动参数、签名字段、公钥配置示例、动作映射与九步请求示例见[运行合同](docs/activity-service.md)、[配置示例](collection/activity-service.example.json)、[接入设计](docs/activity-retention.md)。启动新隔离路径、提供可信身份/UTC公钥与issuer及proof文件；取`/health` challenge让授权UTC来源签发新证明。公钥示例和签名结构不是凭据，不从主机系统时间自行签发生产proof。示例启动默认不删除，定期dry-run；需要P1清理时逐个指定现存绝对本地库`-p1-retention-db`，不接收HTTP任意路径、不新建缺失库。

新活动：后端同步权威id/num/venue与config_version → 同步完整bindings/route及binding_version → 同活动登记模板/分类、核验clock profile → 明确monitoring enabled=true及monitoring_version → P1摄像头/闸机传相同scope → 用户签名授权新context打开订阅。设备修订按CAS提交完整范围，再更新生产者版本和客户端订阅；旧持久请求不能偷偷换版本。P1摄像头配置目前重新加载运行服务，算法核心不用每次改代码。

有全局retention权限时`POST /v1/retention/run {"dry_run":true}`先核对；`GET /v1/retention/status`观察attempts/successes/failures/running/last_duration_ms/last_error/last_result和每活动行数、版本/GC、push、P1表、before/after_logical/after_physical。经另行生产部署授权才加`-retention-enabled`并请求`{"dry_run":false}`；默认1分钟定期执行，缺签名时钟/库清单/维护配合报错或BLOCKED，不能当完整清理成功。接收器也必须按deadline拒绝过期消息。

### 迁移、回退、坑点及协作清单

迁移前停写并保存包含WAL的SQLite一致性备份、程序/模型/配置SHA；先在隔离副本试运行。旧全局数据不猜activity，缺可信期限不重新发168小时窗口；登记资料按权威数据显式重同步。旧程序不认识新scope和墓碑，不能直接在新库降级；回退用成对旧程序及匹配备份到隔离路径。恢复备份先取新可信证明、重新施行原采集截止清理再提供业务访问，不能用恢复重置历史期限。备份本身的历史保留由部署方另行管理。

踩坑及处理：已知时间一度误取首次接收导致窗内B提前过期，改为采集时间唯一口径；durable time_source数字枚举不能按字符串解析；只统计按旧对象树估算会错，改完整内存清理计划；删掉旧正事件会丢失跨截止顺序歧义，改最小散列依赖；有效gate仍藏有expired face binding，保留gate自身、清除依赖并冻结当前合法归属；P1关闭SQLite前尚有native/HTTP在途以及本地循环变量持图像，需要全入口barrier、排空、清缓存再释放租约；只读SQLite打开也可能创建WAL辅助文件，增加实测文件冻结/私有索引；全局FIFO和容量预检会让A堵B及拒绝安全重试，改scope配额/轮转/事务内实际新增检查。测试曾把未来70秒采集放在T0参考，新增未来保护正确HOLD，修正合成参考而未放宽算法或改变顺序断言。

前后端最小配合：权威活动映射/版本化范围与启停同步，签名动作授权和context更新，来源可追溯UTC证明持续刷新，实际相机时间profile，同次通行脸图关联，不可变source/event，旧响应/旧推送丢弃，接收器活动版本/去重/到期检查，全部P1库清单与维护握手。现场待完成：真实页面登录退出/切换、多用户授权撤销、真实接收器与UTC来源、实际同人/出闸、模型来源及点位完整性、目标平台恢复、长时间运行和600路/3090延迟验收。

待业务确认但已有保守默认：同一场地共享设备的具体route版本由谁发布（缺失HOLD）；登记图库是否由后端统一多活动同步（当前显式独立）；未知历史是否有可证原首次隔离时间（没有则立即清除）；活动取消和延时对应哪一权威monitoring状态（不按计划时间猜）；永久墓碑/配置修订及备份的容量归档策略（本轮不擅自删除必要防复活状态）；P1多实例维护和非Windows外库冻结需目标平台联调。以上不阻止已实现的本地保守合同。

<a id="p1-maintenance-recovery-20261012"></a>

## 28. 2026-10-12 P1维护恢复竞态修复

用户授权仅查清P1恢复失败、修改本地代码并在新隔离库验证；本次没有访问或改变生产服务器、数据库、Java或相机。原首次失败未保存报告，本轮实际复现了同类INCOMPLETE恢复故障，不能重建旧首次日志。完整方法、坑点、复跑命令及恢复操作见[P1第19节](../face-recognition-engine/HANDOFF.md#p1-maintenance-recovery-20261012)。

C++ `cpp/src/p1_retention.cpp` 的一次性MoveFileExW与P1的Path.exists相冲突，实捕Win32错误5，含提交后的PHYSICAL_CLEANUP_PENDING阶段。对标记原子替换/最终删除错误5/32/33以5ms有界重试约1000ms，保留已有屏障并在COMMIT前设keep；物理清理、统计、显式删除都成功才报告CLEARED/COMPLETE。删除失败不再被析构吞掉，不存在删旧标记再发布造成的短暂解暂停。持续占用仍INCOMPLETE，释放后下轮恢复。

P1两个既有Python文件仅修改维护兼容：实际消费者锁请求的EACCES/EAGAIN分类ConsumerLeaseBusy；标记消失但C++租约尚未释放时P1保持PAUSED、累计maintenance_resume_waits并在50ms下一轮重试。真正open/stat/unlock/reopen/native错误仍计入maintenance_errors，避免无限wait=True。在途capture的维护暂停在操作屏障内正常返回，先释放携图像的异常栈，再交出租约；真实decoded图像weakref在关闭DB和退出lease前均为空。没有更换模型、重写推理或增加Python生产模块。Go源码、活动/授权/可信时钟与168小时边界均未改。

验证：最小同文件轮询7个独立失败fixture，两个100轮绿对照；修复后最小100轮、完整Runtime合法PNG/8线程/300次恢复及逾万次HTTPhealth均通过；发布/删除的短时及持续真实Windows句柄占用4项通过；真实进程消费者租约交接红→绿，异常分类/持锁stop共5项通过。原维护8、保留10、跨库依赖7，以及当前正式Go/C++隔离HTTP31项通过；两个Release C++目标构建成功。A超窗数据不可复活、边界B与登记资料保留，没有把数据库体积或一次成功当成长期稳定性/容量证明。

当前worker为 `cpp/build-activity/Release/face_trajectory_worker.exe`（SHA前缀535bf8b4），适配器7c1ad7f3，Go服务c7f7e2b5保持原版。源码、配套Python、全部报告与失败fixture的SHA见[新封存清单](../face-recognition-engine/evidence/2026-10-12-p1-maintenance-recovery/final-manifest.json)，根因、排除对照及探索失败见[诊断记录](../face-recognition-engine/evidence/2026-10-12-p1-maintenance-recovery/diagnosis.json)。第27节旧final-manifest仍代表修复前那次封存，不能用其旧worker SHA认定当前二进制未变或重新通过全部旧测试。

本地验证完成；现场后端联调、生产部署/清理、目标Linux平台和600路/3090容量验收均未执行。启动和新活动接入沿用第27节，`-p1-retention-db`只接受明确配置库、默认dry_run、生产删除另需授权；生产需同步当前worker及两个Python维护兼容文件。观察INCOMPLETE的reason/phase/retry_limit_ms与每库CLEARED及P1健康状态，不手删request，不重置保留期限。迁移/回退仍用匹配程序和含WAL的一致性备份到隔离路径。经验是把“标记写入、物理清理、标记清除、消费者租约释放”分别验证，完成状态不能依赖忽略错误的析构清理；本次没有新增业务待确认项。

## 29. 2026-10-12 GitHub成熟方案复用调研

已按官方仓库/源码/模型许可检索，完整对照与来源见[调研记录](docs/open-source-security-comparison-2026-10-12.md)，P1侧交接见[P1第20节](../face-recognition-engine/HANDOFF.md)。本次只改文档；没有安装、模型替换、业务代码改动、后端联调或生产操作，第28节程序SHA及测试不变，原证据清单的文档SHA代表追加调研前快照。

可复用顺序：现有SCRFD/ArcFace + ONNX Runtime C++部署；连续视频用ByteTrack单镜关联；有全身数据时以FastReID/OpenVINO作跨镜候选基线；有同步视频、相机投影矩阵和NVIDIA平台时评估DeepStream。OpenCV YuNet/SFace与SeetaFace6作CPU/SDK对照，CompreFace参考完整人脸服务管理。算法部件复用不能直接替换当前活动作用域、可信时钟、拓扑候选、身份确认和168小时全存储合同。

经验与坑点：代码、权重、数据、SDK runtime许可分别查；旧仓迁移和maintenance mode以官方声明为准；新接口不能自动继承旧算法成熟度；单镜track ID、人体ReID相似度与登记人脸身份分开。后续离线评估先固定commit/模型SHA/平台/用途许可，用真实标注比较误关联、漏关联、拒绝率、时延和恢复；关键数据缺失先询问，不以开源benchmark承诺600路/3090目标已达标。该次仅调研完成，后续本地实施更新见第30节。

<a id="no-downgrade-reuse-20261012"></a>

## 30. 2026-10-12 保持512D与完整约束：开源复用和离线错关联评估

用户明确完善原算法、优先减少误认/跨摄像头错关联。本轮核对已有源码而非整套替换：C++图库已经完整512D/feature_space分桶、同人多模板最大分数、不同人top2竞争，关联保留可信时钟/拓扑可行性和margin；top8只是显示范围。未引入ANN隐藏竞争者、放松门槛、降维或更换模型，C++/Go源码及当前worker535bf8b4保持第28节版本。P1同模型SCRFD合同及裁边质量传递已修复，细节见[P1第21节](../face-recognition-engine/HANDOFF.md#no-downgrade-reuse-20261012)，不新增Python生产推理模块。

**新交付：** [tools/evaluate_identity_events.py](tools/evaluate_identity_events.py)读取保存的实际worker结果与独立GT，输出登记错误关联、未登记误接受为登记身份、匿名false_merge/false_split、HOLD/PROVISIONAL及覆盖率。GT以activity/source/observation唯一键关联，跨活动不混算；匿名自动ID不直接等同人工person，按活动/episode的同异人配对比较，拒绝会计同人错分，缺行/重复/非法状态/非有限值报错。baseline/candidate冻结完整模型feature_space、实际身份/质量政策、全登记模板、图、clock、独立GT、输入SHA与完整操作时间线；全局及每活动错误/错并/错分不增加，登记正确及匿名确认覆盖不下降。

自动评估只接受未人工纠错的结果，`CORRECTED`拒绝；manifest新增 `replay_mode=AUTOMATIC_OBSERVATION_REPLAY/request_timeline_sha256/operation_inventory`，仅允许clock_profile_put/activity_sync/activity_bindings/activity_monitoring/monitoring_person_put/monitoring_observe。保存完整顺序setup、clock、观测请求且不混入GT；基线/候选时间线和inventory必须相同。通用工具不执行worker，无法证明声明对应真实执行或标签真的独立，报告三个verified字段均false；不能靠这些声明证明未披露纠错不存在。

**本地实测：** [最终59项报告](evidence/2026-10-12-no-downgrade-reuse/identity-evaluation-05/summary.json)。先写独立合成GT及23条请求ledger，再用当前正式C++worker、全新本地SQLite执行11个事件，实际发送与ledger逐条一致；9 ACCEPTED/2 HOLD，含1 PROVISIONAL。覆盖登记/未知身份、跨source、同ID跨活动、匿名拒绝、缺失/重复/错scope、JSON异常、模型/512D/政策/图库/图/clock/输入变化、全拒绝、A改善掩B退步、人工纠错和操作时间线。只证明工具/事件合同，不是现场精度，报告明确SYNTHETIC_TOOL_CONTRACT/field_accuracy_claim=false。

完整输入字段、保存结果对照命令、ByteTrack/TrackEval固定commit及条件、P1 20项合同/23项质量/23项入口/GPU4项/维护8项证据见[单一实施记录](docs/open-source-reuse-audit-2026-10-12.md)；代码/模型/程序/文档SHA及旧快照见[本次清单](../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/final-manifest.json)。第27、28节证据清单保留历史快照，不覆盖为新HANDOFF摘要。

```powershell
# cwd=D:\人脸算法；output必须不存在，自动新建本地库，禁止传生产资料。
$env:PYTHONUTF8 = '1'
$identityPython = 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe'
$identityRun = 'face-trajectory/evidence/2026-10-12-no-downgrade-reuse/recheck-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $identityPython 'face-trajectory/evidence/2026-10-12-no-downgrade-reuse/verify_identity_evaluation.py' --worker 'face-trajectory/cpp/build-activity/Release/face_trajectory_worker.exe' --output $identityRun
```

**怎么做、踩坑与经验：** 先固定官方源码/本地模型和独立输入，再做两个可复现质量守卫，随后用真实模型数值对照和真实C++合成事件分别验证。动态输出描述符不能代替实际张量；P1裁边后的框会掩盖已有门槛；不同入口默认政策必须记录实际值；人工纠错成绩泄露由CORRECTED拒绝和顺序请求冻结处理。初期评估-01未记录全部策略被工具拒绝，-02/-03/-04是历史协议验证，当前接手使用-05；旧自动GID与模型框只能作为无标签样本，不生成预期真值。

**边界与下一步：** 评估范围仅P2_FEATURE_EVENTS，不能统计P1图片漏检/质量拒绝、视频丢帧或连续bbox的HOTA；ByteTrack/TrackEval需连续视频/独立frame-bbox标注、dt与重连适配后再接入。现有1,288条无独立真值，2026-10-12用户确认暂无标注、先完善离线工具；59项合同验证已完成，新增[人工GT填空模板](collection/templates/identity_truth.template.jsonl)及[填写说明](collection/README.md)，默认不完整资料会被拒绝。真实误认/错关联下降尚未测定，保持现有模型/阈值，后续取得标签再冻结验收。当前启动/活动同步/可信UTC/168小时清理、迁移和回退沿用第27、28节，本轮无需数据库迁移。四层状态：本地修复及隔离工具验证完成；真实联调待完成；生产未访问/部署；600路/3090容量未验收。原模型权重用途许可、完整拓扑及实际钟源证据仍需现场独立解决。原final-manifest保留上一轮快照，本次文档和模板见[后续SHA清单](../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/no-labels-followup-manifest.json)。

<a id="gtr-bytetrack-20261013"></a>

## 31. 2026-10-13 GTR、ByteTrack与YOLO/ArcFace时空优化核查

用户确认GTR指CVPR2022《Global Tracking Transformers》，并补充ByteTrack、ByteTrack+ArcFace以及添加YOLO检测的比较。本次先读取当前P1/P2源码和现有合同，再核对论文、官方固定提交、配置、关联源码与C++部署。完整来源、代码定位、取舍与实验见[优化核查报告](docs/gtr-bytetrack-optimization-2026-10-13.md)。只更新本报告、主交接和docs索引，没有安装依赖、下载权重、改业务源码/模型、运行推理、访问服务器或部署；原程序SHA与历史验证边界保持原记录，本次没有重新执行算法测试。

**判断与接手顺序：** 连续视频优先评估YOLO人体检测+ByteTrack局部轨迹，保留SCRFD五点人脸检测/对齐与ArcFace512，增加有歧义保护的脸—人体绑定后接现有P2。仅需要有脸经过记录时，可先评估SCRFD脸框+ByteTrack；只有离散抓拍的设备继续现有链路。该组合是工程优先候选，尚无现场最优或准确率提升结论。GTR官方是单视频窗口学习关联，MOT窗32/TAO窗16，实际默认关闭位置/时间编码，在线拼接有Hungarian；原测试入口预载整视频，复用还需流式和部署适配。它不能直接替代P2真实时间/拓扑/实名身份。

**可复用与真实缺口：** ByteTrack有MIT的官方TensorRT/ncnn C++跟踪实现，可抽取tracker而不整套替换检测模型。P1现有TrajectoryExtractor已检查质量并透传外部local_track_id+track_session_id，尚未接ByteTrack；P2已有精确全512D按人多模板检索、竞争margin、整链时空可行性、活动隔离和168小时保留，不能重复列为未实现。优化候选为单镜跟踪/优质选帧、脸—人体安全绑定、待定事件窗口联合评估及有版本/期限的计算缓存。

**踩坑、处理与经验：** 不把人体框直接送ArcFace；低分框仅可续轨，不能降低人脸身份门槛。现有1/2/6fps动态采样和丢帧不能直接套ByteTrack固定dt=1，frame_rate参数只缩放lost buffer；C++和Python阈值/低分分框也不同，先固定输入核对。人群交叉/重连/长缺帧要解除或暂缓局部身份绑定，不能凭track ID传播实名。GTR全局指窗口，ByteTrack公开MOT数字不等于跨镜身份精度；加YOLO的新增检测/解码成本必须与减少ArcFace调用一起测，不能预报600路容量。脸不可用的身体轨迹保留为候选，不能伪造新的确认人脸经过证据。

**评估与未完成：** 先用同批原始视频和独立标注比较A现有脸链、B加脸框ByteTrack、C加YOLO人体ByteTrack与脸—人体绑定，必要时D对照GTR；分别验收局部ID切换/HOTA、端到端漏检/错绑、P2错并/错分/登记误认、确认覆盖及p95/p99延迟和资源。现有evaluate_identity_events只支持相同P2事件/完整配置的冻结比较；新选帧、track字段、聚合策略改变输入时，需另立端到端评估协议，不能伪装成同输入通过。窗口自动确认也需明确新状态/幂等/修订合同，不能用CORRECTED成绩代表自动算法。正式实施前需可用连续视频、独立标签和选定权重；本次未盘点当前现场数据。调研与文档完成，算法候选未实施；真实评估/联调、生产和容量均未验收，运行与回退沿用第27–30节。

<a id="point-connections-20261013"></a>

## 32. 2026-10-13 普通摄像头点到点接口与本地查看交付

用户明确普通摄像头只需已确认经过点位的连接线；仅鹰眼单摄像头需要画面轨迹，鹰眼后置。本轮按用户确认先完成算法接口和本地查看，未改业务前端。完整字段、方法、启动/复跑和接手要求见[点位连接交付](docs/point-connections.md)。普通分支沿用SCRFD/ArcFace512、P2身份/时间/活动/保留合同，不以新增YOLO/ByteTrack/GTR作为点位连线前置。

已在CameraPointTrace新增drawing_mode与connections，正常monitoring入流后按point-trajectories/get查询即可取得两个端点；相邻同摄像头抓拍不画自连线，原观测保留，A→B→A保留返回。时钟重叠组保留虚线/方向待定，位置缺失/冲突/多个候选不取第一位置，不同快照会话保留断点；无需推断完整路径，physical_path_observed=false。新端点ID保留source命名空间，旧observation_order标识保持兼容，新增source_id明确观测查找。纠错导致当前结果409/REBUILD_REQUIRED、历史STALE_ASSOCIATION、重建移除误归属及跨活动同人隔离均实际验证。

只读本地查看在tools/point-viewer，loopback8793，浏览器读取保存的完整回复，默认空白并支持三种坐标投影。切换/错误加载先清空旧结果，没有后台API代理、凭证或数据库接入。合成按钮仅加载实际Go/C++的验证示例，不代表现场人员/路径；当前查询状态属于导出时快照。旧8769工具与历史资料未改。页面与本次截图见[浏览器验证](evidence/2026-10-13-point-connections/browser-verification.json)。

本地新产物：cpp/build-point-connections/Release/face_trajectory_worker.exe，SHA前缀2e2b33aa；该目录Go服务仍c7f7e2b5且源码未改。旧build-activity产物/运行进程及业务库未覆盖；无需新数据库字段迁移。采用新输出时按第27节签名/UTC启动合同使用新目录worker。回退使用匹配的旧程序与完整一致性库备份，本次所有测试只用新隔离库；未在实际服务器执行升级/回退。

验证：新增CTest23项边界、查看数据8项、真实签名Go→C++→新SQLite20项；现有活动核心40项和服务31项回归通过，Go build/vet/test通过，完整OpenAPI38操作/569引用和116个实际HTTP回复通过。证据为[HTTP20](evidence/2026-10-13-point-connections/http-final/summary.json)、[核心40](evidence/2026-10-13-point-connections/core-final/summary.json)、[服务31](evidence/2026-10-13-point-connections/service-final/summary.json)、[OpenAPI](evidence/2026-10-13-point-connections/openapi-final.json)和[新SHA清单](evidence/2026-10-13-point-connections/final-manifest.json)。当前CTest只新增point_connections，旧归档目标没有恢复。实际浏览器确认导入、清空、投影和未确定点位显示；算法测试与界面合同均为合成输入，不能宣称现场误认下降或容量已达标。

怎么做、踩坑与经验：先查旧接口和当前源码，再补可直接绘制的展示投影，不替换模型或绕过准入。Windows long double的int64毫秒精度问题改为整数差值/饱和区间。试验的跨目标混合输入被原核心正确拒绝，按实际合同拆成拒绝与待定排除检查；全库零写断言忽略了已有可信UTC水位，改为明确只允许该水位推进；重启时固定测试UTC回退被拒绝，改合成证明而未放松生产规则，失败保留http-01/http-02。没有完整拓扑的实际null主路线暴露旧OpenAPI nullable+allOf缺口，改独立完整NullablePathCandidate合同，通过实际响应校验。测试依赖复用ops与既有独立OpenAPIvenv，没有给P1推理环境增加crypto/验证库。

交接边界：普通点位连接的本地接口和查看已完成；权威实际device绑定/时间/坐标资料、现场身份标签、真实后端/业务前端接线、生产与600路容量未验收。鹰眼单镜连续轨迹尚未实施。普通设备范围沿用明确活动绑定，不通过猜设备名/IP来修改设备列表；本次未访问服务器或相机。旧报告/清单继续代表旧日期快照，新程序以本节及本次清单为准。

**用户复核后的显示修正（2026-10-13）：** 原图逐条描线把B—C与C—B完全盖在同一位置，3个SVG元素只有2条独立几何线，导致重复记录无法从图上辨认。原查看只核对端点/数量，没有检验视觉重叠，这一验收缺口已明确记录。现按相机对和完整3D端点合并几何描线，保留每条连接记录，图上标`#2 / #3`，列表/图上编号均可独立点选第2/3条；分别显示记录数/线段数。原相机坐标共线，不为展示往返移动点位。用户追加要求方向箭头后，顺序确定用实心箭头，顺序待定用虚线加空心箭头，空心只表示from/to记录方向、实际先后仍待核实；共享线两端显示各自方向，点选第3条仅突出C→B。新增4项重叠回归红→绿、4项方向回归及原8项检查共16项通过；实际浏览器无重复描边、覆盖3个记录编号和3个空心箭头，点选/编号轮换/恢复全部通过。方法、失败快照、截图及SHA见[显示修正报告](evidence/2026-10-13-point-connection-display-fix/REVIEW.md)和[新清单](evidence/2026-10-13-point-connection-display-fix/final-manifest.json)。本次只改只读查看工具及交接，没有改API、C++/Go、程序、样例坐标或业务数据；原第32节清单保留修正前快照，当前查看文件以新清单为准。经验是同时校验记录覆盖、几何重叠、方向语义、交互选中与截图，不能用SVG元素数量代替显示正确性。

**模型接入与绘制边界：** 本地合同已实现：确认同人抓拍、可信时间、当前活动/device绑定及相机坐标齐全时，可查询并绘制点到点连接。模型加载或ByteTrack局部ID本身不能保证跨镜同人，缺坐标仍不画线，时钟不确定仍保留空心箭头提示。本轮未验证真实相机→P1→P2→查看器完整链路；现场可用性需真实输入验收。直线不是中间实际行走通路，鹰眼单镜轨迹另行实施。

<a id="review-package-20261014"></a>

## 33. 2026-10-14 时空算法重新打包供他人审核

交付为工作区根目录的`时空算法-审核包-20261014.zip`及同名`.zip.sha256`，解压后从根目录`README-审核.md`开始。本包保留当前P2 C++/Go源码、已有测试、内置JSON/SQLite依赖、OpenAPI、配置与图工具、当前点位查看器、选择后的历史证据和本次重新构建Windows x64程序；P1仅附参考交接/文档/示例，未交付整套人脸模型或P1运行环境。

如何做：先核对当前交接及10-13修正，独立构建到`evidence/2026-10-14-review-package/build`，不覆盖旧运行程序。C++ Release、Go build/vet/test完成；用户要求加快打包后停止追加测试，未重跑C++/查看器/签名HTTP/OpenAPI。当前检查范围见[verification-summary.json](evidence/2026-10-14-review-package/verification-summary.json)。包内完整清单记录每个文件SHA256/大小、排除项及未随包交付的历史资料链接；对ZIP全部成员读回验证SHA256/大小/CRC，并再次核对源文件。原旧包保留，源码目录没有Git，使用本次文件快照确定版本。

坑与处理：旧打包器会排除HANDOFF和全部evidence/tests，只识别build而漏掉build-activity等目录；改为保留当前审核入口及现有复跑材料，排除所有构建目录，显式加入此次三个exe。运行SQLite/WAL/SHM/锁全部排除，不把当前人员状态带进审核包；历史大模型/CAD、原始服务器快照、环境/缓存和旧二进制也排除。历史报告与SHA代表当时快照，保持原文件而不伪装本次重测；独立新清单确定本包版本。

交接：审核者按根README审核活动隔离、身份/时间准入、纠错/版本、点位方向与坐标不确定性、168小时清理/恢复、推送事务和模型版本来源。现有测试驱动及可选复跑脚本随包交付，由审核者按需执行；不要给脚本提供已有运行库。真实后端/相机全链、现场标签、生产部署/清理、目标Linux与600路/3090容量仍未验收，鹰眼单镜连续轨迹后置。本次没有远程访问或部署，没有修改算法业务源码。
