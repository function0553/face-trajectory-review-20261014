# P1 人脸识别引擎：唯一交接

**GitHub同步已核对：** [d0c4813](https://github.com/function0553/face-trajectory-review-20261014/commit/d0c48139b6ed5707f04326eb5df215813859d32e)，远端main与本地HEAD一致，231个文件的清单与远端内容核验通过。

**当前本地修复（2026-10-14）：** bridge对400/404/409/410/413/422隔离并继续FIFO，保存原载荷和错误详情；DeliveryStore补齐410。当前活动字段与环形调度已经存在，新增回归确认。P2 Worker进程/超时/索引及3D拓扑透出同步修复，详见[统一修复交接](../face-trajectory/docs/repair-topology-2026-10-14.md)。模型、512D、质量与身份门限未改，真实业务/现场/容量仍待验收。

**本轮优化（2026-10-12）：** 保持现有模型的SCRFD合同/裁边质量修复、GPU基线对照与离线错关联评估见[第21节](#no-downgrade-reuse-20261012)。真实误认率尚无独立标注验收；模型、512D和阈值保持。

**当前增量（2026-10-12）：** P1维护恢复竞态修复见第19节；P2 0.13活动作用域与C++168小时维护交付见第18节。第17节保留实施前只读快照，其中“未实现”由后续源码及实测更新。三类人员仍使用相同质量/身份标准和重点、普通公平配额；本地验证不能代替正式图库、真实海康/时钟、3090容量与现场验收。

**语言与交付状态纠正：** P2算法核心是C++17、接口是Go；P1现有人脸推理及第9节新增采集/调度/投递运行层是Python，不仅是测试工具。用户要求正式算法沿用C++/Go；本轮功能原型通过不等于整条C++/Go生产链路交付完成。待迁移范围和业务服务器实查见第10节。

2026-10-05：新增 `face_engine.trajectory_bridge`，复用检测/对齐/特征模块并以 SQLite outbox 向 P2 投递不可变抓拍事件。未知采集时间不补成 UTC，重试去重与容量保护已实现。见 [P2 接入说明](../face-trajectory/docs/runtime-and-geometry.md) 与 [本次真实图片验证](../face-trajectory/evidence/2026-10-05-repair/real_bridge.json)。

整理日期：2026-10-07，Asia/Singapore。主目录 `D:\人脸算法\face-recognition-engine`。这是 P1 的当前交接入口；原混合交接已移入工作区清理归档，关键经验整合到下文。本次整理未重新连接或验收现网。

## 1. 职责与项目边界

输入图片或已解码 BGR 帧，执行 SCRFD 人脸检测、五点对齐、ArcFace 512D 特征提取及候选检索。算法库可输出 TopK、相似度和 MATCH / REVIEW / UNKNOWN。人员标识使用外部 subject_id。

历史 Java 接入方案由 Java 管理业务人员主键、模板和报警。本次监控服务沿用外部人员 ID，由 P1 提特征，P2 保存算法模板并执行关联和轨迹事务；业务图库仍由后端提供，不能另造一套业务人员主键。历史部署接口见专项说明，当前运行合同见第9节。

本项目负责视频/抓拍接入、人脸质量与特征和调度；通过监控入口调用 P2 的身份关联与轨迹。轨迹、时间轴及未来场地模型连接属于 [P2](../face-trajectory/HANDOFF.md)。X 光属于 [P3](../xray-training/HANDOFF.md)。

## 2. 内容和文档

| 路径 | 内容 |
| --- | --- |
| [src/face_engine](src/face_engine/) | Python 算法核心及 CLI |
| [configs](configs/) / [models](models/README.md) | 配置、模型格式及使用范围 |
| [benchmarks](benchmarks/) / [examples](examples/) | 性能测量与使用示例；测试代码已归档移出 |
| [Java 接入](deployment/java_face_integration_20260930/README.md) | 人脸接口、帧输入和业务职责划分 |
| [边缘适配](deployment/edge_face_20260924/) | API 适配、部署/回退历史 |
| [FacePoC 本地补丁](deployment/facepoc_20260928/LOCAL_PATCH_RELEASE_20260930.md) | 本地修复与上线前条件 |
| [文档目录](docs/README.md) | 排错、模型、评估与历史资料索引 |

## 3. 已有能力与未完成项

- Python 核心、图片/视频帧处理、演示和检索实现已有；具体命令见 README。
- 图片 API 的历史记录包含 detect/extract/compare；analyze-frames 为本地补丁，不能据此声称已上线。
- FacePoC 2026-09-30 本地补丁包含单脸校验、三态决策、top1/top2 margin、认证与待人工确认报警；记录为 15 项本地测试通过，未连接服务器、未部署。本轮未重跑测试。
- 正式模型由Codex评估成熟预训练方案并验证，效果不足再考虑微调（2026-10-10用户确认）；已完成首批候选本地筛查，尚未批准正式权重，见第11节。临时演示模型及阈值不可直接当作正式验收结果。
- 现场身份标注、阈值标定、多人脸高峰容量与长期误报召回尚未完成验收。历史热状态、相机在线数与旧服务器地址不是实时状态。

## 4. 接手后顺序

1. 先看 Java 接入说明，明确这次是改核心、改 API，还是维护 FacePoC。
2. 核对权重授权、模型版本、预处理、输出维度以及特征库使用的 modelId；模型/对齐变化后重建特征，不能混用。
3. 以真实已核验同人/异人样本标定阈值，区分检测效果、身份识别效果和吞吐。
4. 若部署本地补丁，先核对目标机实际版本及回退方案；历史“已上线”不能覆盖后来“仅本地实现”的记录。
5. 向 P2 交付带来源、相机、时间和归属依据的目标观测；图片相似度不直接等价于已确认同人。

<a id="build"></a>
## 5. 安装与运行核查

工作目录为本项目根目录：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[runtime]"
.\.venv\Scripts\python.exe -B -m face_engine --help
```

本次移除了 pytest 配置、测试源码及 CLI 的模拟 selftest。实际图片、视频、建库和摄像头命令见 README；性能工具保留在 benchmarks。运行前核对模型契约和配置，命令行可用不能证明真实身份准确率。

<a id="face-audit"></a>
<a id="capture-evaluation"></a>
## 6. 数据和历史证据

[P1 文档目录](docs/README.md)列出模型快照、抓拍包和人工核查图片。抓拍中的自动 gid/人员命中不是身份真值；画有框的场景图不能直接当作干净的检测训练图。

<a id="deployment"></a>
<a id="server-history"></a>
## 7. 服务器、实施方法与关键经验

历史部署适配及回退实现见 deployment；旧服务器容量和上线操作保存在工作区归档。操作前重新核实目标服务器、服务路径、模型/代码 SHA 与回退文件，凭据不写入交接。

| 处理过的问题 | 实施与处理方法 | 接手经验和剩余边界 |
| --- | --- | --- |
| 多余逐脸推理、统计与旧帧积压 | 历史优化仅加载 detection/recognition，修正字典计时器，批量提特征、逐相机最新帧调度和预览节流 | 2026-09-28 的上线记录只作历史证据；每脸仍提特征，不据此承诺高峰容量 |
| SQLite 锁等待或失败后丢抓拍批次 | 本地 trace_writer 改为事务确认后再清批次，处理不确定提交、有界重试与正常退出；WAL/保留期在授权窗口处理 | 2026-09-29 本地补丁不等于已上线；恢复不能删除 face_trace.db、配置、队列或图片 |
| 取流卡死、认证失败重复重连和散热余量不足 | 历史只读核查区分认证、网络、解码看门狗和硬件问题；取流看门狗已有本地补丁 | 认证熔断/退避和现场温度仍须核查，旧相机在线数量不是当前状态 |
| 检索第一名被误当确认身份 | 本地 FacePoC 用 MATCH/REVIEW/UNKNOWN、top1-top2 margin 和 pending 报警；注册/查询限定单脸 | 参数仍待目标相机标定，REVIEW 不污染人员命中/报警，MATCH 报警仍人工确认 |
| 不同入口同模型但特征仍不一致 | 固定模型 SHA、五点对齐、预处理与 modelId；非整数缩放曾存在取整、坐标回映和裁框差异 | 512D 相同不等于空间兼容；变化后重建全库。历史整数缩放样本不能覆盖这些差异 |
| P1 到 P2 失败重试和未知时间 | trajectory_bridge 用稳定事件 ID、来源契约和 SQLite outbox 先落盘再投递 | 同 ID 同内容重试；未知时间不伪造 UTC，容量保护拒绝新写入而保留已提交记录 |

<a id="current-goal"></a>
<a id="implementation"></a>
<a id="next-work"></a>
<a id="contracts"></a>
<a id="later-identity"></a>
<a id="documentation"></a>
## 8. 交接维护与本次整理

当前状态、实施方法、问题处理、经验和待办只更新本 HANDOFF.md。P2 的时间轨迹/身份关联转到其独立交接，专项 API、模型合同、部署说明和原始报告继续作为技术资料。

2026-10-07 按要求清理自有测试和重复交接；原混合交接、移出的测试、缓存及修改前文件统一保存在 [工作区清理归档](../workspace-docs/archives/project-cleanup-20261007.zip)，原路径/原因/字节数/SHA 见 [清理清单](../workspace-docs/project-cleanup-20261007.json)。先逐文件核对归档字节后移除。源码/数据/权重/环境目录不迁移，实际设备和远程服务没有操作。测试结论保留原日期，今后需要回放可按清单取出对应历史源码。

踩坑：原文档有混合项目与跨文档“当前状态”，现改为唯一交接加技术资料导航；移除测试必须同时更新 CLI、安装依赖和文档命令。归档不是部署验收，下一步仍按第4节执行。

整理核查（2026-10-07）：人脸 CLI `--help` 成功，命令列表已不含 selftest；pytest 安装配置已移除。Python 源码解析和交接/README/技术资料入口链接检查通过。核查只验证本机入口和整理完整性，未重新验收真实模型、相机或服务。

交付打包（2026-10-07）：按用户选择，将三个项目合并到工作区根目录 `安保算法三项目-交付包-20261007.zip`。P3 包含 xray-training 和 xray-design；保留原目录关系、源码、模型、配置、三份唯一交接、历史报告、样本资料、必要共用资料及现有交付包。虚拟机/WSL 镜像、虚拟环境、pip 开发依赖、下载分块、缓存、生成工程与 SQLite WAL/SHM/锁文件不装入；P2 已核查的 Release worker/算法库保留。清理历史 ZIP 为单份压缩证据，不在项目内恢复散放的旧交接或测试。

方法与经验：用 workspace-docs/package_projects_20261007.py 写 ZIP64，直接按原路径流式归档而不复制整棵目录；SQLite 通过只读在线 backup 得到一致快照，不直接复制运行中的主库遗漏 WAL。包内 PACKAGE-MANIFEST.json 记录实际文件路径、字节数、SHA256、数据库快照和排除项；每个成员解压读回核对 SHA/字节/CRC 后，才将 partial 文件改为最终 ZIP。最终 ZIP 的大小、SHA 与验证结果在工作区 workspace-docs/project-package-20261007.json 查看，接手时先完整解压、校验清单，再按本项目运行说明准备环境和验收。本地虚拟环境不能直接迁移为另一台电脑的可运行环境；压缩和一致性核验不证明现场部署或算法准确率。

打包结果（2026-10-07）：最终 ZIP 已生成，9583 个载荷文件逐项 SHA256/字节/CRC 读回校验通过；ZIP 9023476958 字节（约 8.40 GiB），SHA256 `2547dbb01789c2b3c053ffa03f6fc1abe99eba94c984e1c02b616a68ba1d7c81`。压缩包内是此次打包时的文件快照，本行最终归档 SHA 在本机交接/工作区记录维护，避免把 ZIP 自身 SHA 写回 ZIP 造成循环变化。没有改变任何现场服务或原有交付 ZIP。

## 9. 2026-10-10 三类人员、质量与采集运行链路

**用户确认的需求：** 人员资料提供ID、重点/普通标记和照片；未入库人员允许用第三类匿名ID建轨迹。闸机只验票，附近摄像头或后端提供图片；不能把票据当作身份。明确出闸结束监控，漏入口仍启动并标记入口未知。算法自行拉流，低清允许补取主码，海康原生抓拍直接用图片比对。重点高频低等待，普通保底，身份门槛一致。目标3090、约600路摄像头，抓拍到结果期望2秒、3秒也可接受，越低越好；低负载允许CPU。工程配置记录2秒目标/3秒可接受边界，仍须在真实输入组合上验收，不能用本地照片成绩承诺全场容量。

**如何做的：** 复用既有SCRFD→五点对齐→ArcFace，不引入另一个识别模型。quality.py在对齐前检查原始脸尺寸、瞳距、清晰度、边界和姿态代理指标；拒绝后不提特征。image_pipeline统一不同人员top2差距；即使top_k=1也检查竞争者。正式CLI、示例和演示登记均要求原始检测恰好一人且质量合格。monitoring_service组合自拉流/海康/后端图片、三类调度、SQLite抓拍与投递队列；实际比对、实名/匿名关联、监控及轨迹由P2 0.12同事务执行，不维护第二套生产图库。

**调度与数据可靠性：** 视频每相机保留最新帧，默认2秒过期；三类默认6:3:1配额和6/2/1fps采样，均为工程初值。AdmissionPlanner对尚未入队的相机也按类别及最久未成功接纳顺序分配机会；失败不清除等待年龄，刷新已有帧不消耗新入队轮次，原子刷新避免消费者同时取走帧时绕过配额。尚未确认身份的画面按普通处理，活动重点人所在相机短时提高采样；普通/匿名仍可处理。抓拍与出站队列也按类别轮转，403/网络/503等独立退避，400等永久错误隔离保留。回执核对事件/人员ID和版本；旧ACTIVE回执不能盖过后来EXIT的调度水位。原图和多脸观测扇出同事务交接，抓拍池满时仍有有界扇出余量。

**采集与时间：** FFmpeg独立进程解码，最新帧一槽、阻塞看门狗、退避重连和停止回收；主码限频，新帧独立检测计时。海康GET Digest订阅按同事件contentID配图，校验NVR channel，不取vendor人员ID。普通FFmpeg没有可靠同帧绝对采集时刻时保持UNKNOWN；可选GStreamer 1.22+读取同buffer NTP reference meta，仍须P2真实clock profile核验总误差≤60秒。当前机器无gi，原生GStreamer和现场RTCP映射尚未验证。海康event dateTime不冒充faceTime，收到时间不冒充采集时间。

**本轮验证：** 质量/竞争候选边界27项、真实照片退化与同图检索3项、极端几何1项、正式/示例登记10项通过。30张历史裁剪图检出18脸：7合格、11拒绝；无标签，不是识别精度结论。调度/容量/回执/鉴权独立审计17项通过并在最终调度源码上回归通过，三类别100次繁忙投递与抓拍均为60/30/10。最终实际照片→HTTP→CPU模型→真实Go/C++→自动轨迹[27项通过](evidence/2026-10-10-monitoring/runtime/integration-complete-final/results.json)；真实本地模糊视频→主码补图→模型→P2与loopback海康Digest整链[21项通过](evidence/2026-10-10-monitoring/runtime/stream-integration-complete-final/results.json)，两次均使用最终worker 46725cf0…df4b及最终P1调度。采集65项通过。证据分别在evidence/2026-10-10-monitoring下quality、runtime、acquisition目录。真实照片登记/回放用合成身份和时钟，只证明接线；本机实际GPU为GTX1650Ti，不证明3090容量。

**踩坑及处理：** 低清resize不能恢复面部信息；质量检查移到提特征之前。同图多张脸中过滤后只剩一张也不能登记，检查原始检测数。received_at与海康重传计数不能进不可变事件指纹；只剔除已明确的传输字段，摄像头时间和图片内容仍参与去重。NVR不按channel过滤会把其它摄像头抓拍错绑，服务入口现要求expected_channel_id。共享抓拍/观察同一容量池可导致满池扇出死锁，改独立预算和有界余量。HOLD时association为null，回执校验不能直接.get；新整链测试已覆盖。停止期间取消主码任务会跳过函数finally，现用future完成回调清pending，停止后不再提交，先等待推理线程再回收线程池。

**运行与照片补验：** 新增跨进程consumer lease，防止同状态库同时启动runtime与flush重复消费；并行登记仍可入队。启动中途失败自动停止已启动部分并释放锁，线程未启动时不join，进程退出锁可恢复。配置权重同时作用于视频、抓拍和出站队列，拒绝布尔/非正FPS；真实子进程等32项通过。JPEG EXIF旋转/镜像由OpenCV保留，尺寸校验允许旋转造成的宽高互换，仍在解码前限制像素；8方向像素与实际HTTP共51项通过。新增抓拍状态查询供后端取得质量报告、observation_id和原事务回执，不返回图片/向量，也不代替闸机绑定证据。方法、失败反例和复跑见[运行与照片复核](evidence/2026-10-10-monitoring/runtime/consumer-capture-audit.md)。

**600路采集处理：** 同一实际URL/凭据的NVR只订阅一次，按明确channel分派各自source/camera/profile，未知或冲突通道拒绝，纯抓拍相机也校验全局ID唯一。600个逻辑通道配置只建1个reader且测试未连接设备，双通道Digest及容量CLI共27项通过，既有采集65项回归通过。历史62台能力审计仅确认其中22台facesnap规则开启，515条记录是端口候选，不是全场600路已核定人脸抓拍清单。当前每路持续视频仍独立FFmpeg解码，不能直接承诺单3090持续处理600流；[600路审计](evidence/2026-10-10-monitoring/runtime/600-camera-acquisition-review.md)说明来源、开销和待验项。`--capacity-report`无需模型、数据库或联网，按实际配置与有来源的容量测量核算，默认UNMEASURED。

**600路调度修复与经验：** 只轮转扫描相机、只给队内类别配额仍会让队外普通相机等待很久；因此新增公平接纳及health.live_camera_coverage，分别统计实际相机处理间隔与帧龄。7项边界检查和20组修前/修后模拟完成，另有吞吐不足反例。600台按重点/普通/匿名300/240/60分配、队列128、假定服务300帧/秒时，普通相机完成间隔P95由8.95秒降至2.667秒；假定200帧/秒仍为4秒，即使帧龄P95仅0.185秒。模拟使用真实调度代码及虚拟时钟，不含解码、模型、人脸数、HTTP或P2成本，不证明600路延迟通过。临时字节不足时为能放进空队列的大帧保留后续入队机会，永久超大帧不阻塞其它相机。详见[前后模拟](evidence/2026-10-10-monitoring/runtime/audit-600-camera-scheduler-before-after.json)和[不足容量反例](evidence/2026-10-10-monitoring/runtime/audit-scheduler-capacity-boundary.json)。

**最终核对：** [文件SHA清单](evidence/2026-10-10-monitoring/manifest.json)记录本轮P1源码、配置、当前交接和主要证据，另引用P2最终清单。正式权重尚未提供，本地demo小图320分支触发ONNX输出静态形状提示；解码按实际输出及网格数量校验，整链通过，提示保留在stderr，不修改权重或屏蔽日志来冒充正式模型验收。

**交接与剩余项：** [运行合同](docs/monitoring-runtime.md)包含配置、启停、登记、回调、鉴权环境变量、时钟合同和验收步骤；[采集说明](docs/acquisition.md)列底层依赖与实现边界。新服务请求CUDA但实际退CPU时明确停止，CPU需显式配置并测负载。正式图库、真实同人/异人样本、相机/闸机绑定材料、可靠clock profiles、600路中的抓拍/拉流实际比例及事件峰值、3090实机与场地模型待实际接入；不修改生产或既有ZIP。质量阈值、模板模型授权和检测场景泛化未完成现场验收。高密度长历史的轨迹成本和多日留存另见P2实测边界，不能把功能测试当600路长期容量通过。

清理记录：登记验证首轮np.load未关闭导致Windows临时文件锁，已改with并通过复验；一次已知测试临时目录删除被自动审批以blocked by policy拒绝，已停止该删除，临时目录保留，不影响结果。

## 10. 2026-10-10 业务服务器只读实查与C++/Go边界

**用户补充：** 业务服务器能访问摄像头，目标SSH为211.139.12.141:22；用户要求自行查看，并再次确认算法应为C++/Go。本次使用其授权凭据连接，凭据仅交互输入、未写入脚本/证据/交接；校验先前已观察的SSH主机指纹一致。该端口的业务机与历史2222入口区分，本次没有连接另一入口。

**服务器实际状态：** Ubuntu22.04.5、双路Xeon Silver4310、24物理核/48逻辑线程；系统可见62.35GiB内存，检查时MemAvailable32.04GiB。PCI只见华为iBMC VGA，未发现NVIDIA计算设备或nvidia-smi。5.006秒/proc/stat差分CPU忙碌约1.57%，仅代表该短窗口，ps累计百分比不用于判断瞬时余量。系统盘可用240G，业务数据分区可用9.2T；没有做磁盘/推理压力测试。现场已有Java、数据库和媒体等业务进程，不能把全部CPU/内存当作算法独占资源。

**摄像头连通性：** 复用用户指定聊天及本地两份设备/候选清单，去重515个已知地址，不重新扫描整个网段。服务器侧仅TCP连接80/554，首轮并发8、超时1.2秒；47个失败端口用并发4、3秒超时复核，无新增成功。最终484个地址80可连接、499个554可连接、500个至少其一可连接；15个两端口均未连通。此结论不包含摄像头登录、实际取流、抓拍输出或全部约600台身份验证；短时未连通也不证明设备离线。10/20/30网段没有登录或任何配置修改。

| 网段 | 清单地址数 | TCP80可连 | TCP554可连 | 两者均未连通 |
| --- | ---: | ---: | ---: | ---: |
| 192.168.1 | 39 | 37 | 37 | 2 |
| 192.168.2 | 17 | 15 | 15 | 2 |
| 192.168.3 | 8 | 8 | 8 | 0 |
| 192.168.10 | 180 | 154 | 169 | 10 |
| 192.168.20 | 105 | 105 | 105 | 0 |
| 192.168.30 | 166 | 165 | 165 | 1 |

**网络和运行依赖：** 相机内网eno1=192.168.1.252，协商1000Mbps/full；路由实查1/10/20/30网段均走eno1。公网eno2协商100Mbps/full；若GPU跨公网，须按实际路由和图片/视频量测量，不能把内网千兆当作GPU传输带宽。5秒内eno1接收约0.552Mbps、发送0.177Mbps，rx_dropped增加26；仅留作诊断计数，不能断言摄像头流丢帧。系统PATH未找到ffmpeg、Go、nvidia-smi，系统Python未发现所查ONNX/Torch包；不排除其它私有环境。已装GStreamer1.20.3，不满足当前Python可选同帧NTP采集路径的1.22+前提。无需因系统未装Go就认定编译好的Go程序不能运行；正式二进制需核验Linux/架构及C++动态依赖。本次未安装、升级或启动任何算法服务。

**部署判断：** 此机可作为抓拍接入、持久投递及C++时空关联服务的候选部署点，业务负载低时可评估少量CPU人脸推理；没有测过其真实模型吞吐，不能承诺600路解码/推理或2–3秒结果。优先使用相机原生抓拍或既有后端图片，把需要检测/提特征的工作交给合适算力；GPU服务器是否与相机/业务机处于可用高速路径还需核验。设备时间仍需各相机核验，服务器NTPSynchronized=yes不能代替60秒摄像头误差证据。

**C++/Go正式交付待办：** P2的gallery/association/time/trajectory/monitoring在cpp/src，Go仅承接HTTP/worker。P1的SCRFD/ArcFace/quality、acquisition、scheduling、monitoring_delivery、monitoring_service目前确实由Python运行；不能将它们称作已完成的C++/Go服务。后续按既定架构将检测/对齐/特征与质量放到C++，采集编排、调度、HTTP和持久投递放到Go，保持现有模型/特征、时间、事件去重及事务合同；先做同图特征和全链路等价验证，再替换运行入口。Python可继续用于离线检查和证据脚本。此条是迁移范围记录，本次没有开展重写、部署或把未实现部分列为完成。

**如何核查及经验：** 先查可用MCP，无SSH专用连接器，复用本地.venv-ops的Paramiko；SSH仅执行内存中的只读Python标准库命令，未上传远端脚本。首次lscpu/df中文locale显示乱码，复核子进程设置LC_ALL=C并从/proc/cpuinfo核对24核/2插槽，原始证据保留。TCP拒绝、超时和实际摄像头业务可用性分开；MemFree很低不能代替MemAvailable判断。短样本低负载不能当作推理容量验收，语言交付边界也必须与功能测试边界分别记录。

核查数据、失败地址及范围见[本次摘要](evidence/2026-10-10-business-server-readonly/summary.json)、[原始硬件](evidence/2026-10-10-business-server-readonly/hardware.json)、[端口检查](evidence/2026-10-10-business-server-readonly/camera-connectivity.json)、[复核](evidence/2026-10-10-business-server-readonly/recheck.json)。源清单SHA、可复跑脚本及本次改文档前副本保存在同目录；[SHA清单](evidence/2026-10-10-business-server-readonly/manifest.json)记录当前证据。本次算法源码及正式模型未改、未跑模型推理或新增人脸抓拍、未写生产业务数据。

## 11. 2026-10-10 模型责任确认、性能实测及成熟候选

**用户决定与处理：** 正式人脸模型改由Codex评估成熟预训练方案并验证，效果不足再考虑微调；已同步模型合同和README，取消“用户必须训练并提供权重”的旧前提。ONNX是已训练网络的导出推理格式，C++也可运行；ID/照片登记只生成模板，不等于逐人训练。场地三维模型与人脸网络独立。

**实测结论：** 本机i7-10750H、CPU2线程，现有SCRFD10g+ArcFaceR50在一张质量合格历史小图上平均223.25ms，99.06%时间在ORT调用；本机GTX1650Ti实际CUDA约20.75ms。C++原生ORT对照与Python同包同版本/相同输入权重，40个输出张量逐项一致，最大差0；计时受波动与内部构建差异影响，不能宣称固定语言加速倍数。此结果说明本样本主要瓶颈在模型计算，不能承诺改语言后600路就达标。服务器CPU和3090仍未跑该测试。

**成熟模型筛查：** 从OpenCV官方Zoo固定提交下载YuNet2026may（MIT）和SFace2021dec（Apache-2.0），逐一核对Git LFS SHA/字节并保存原许可。30张历史对齐裁剪图，候选检测19脸/质量通过6，现有检测18/通过7；无人工身份标签，不能报准确率或召回率。选择双方通过一脸的同张图作ABBA CPU计时，候选平均36–38ms，现有234–238ms；不同模型/引擎/维度，不能据速度单独批准替换。候选128D与现有512D不兼容，保留EVALUATION_ONLY，不改默认配置、阈值或图库。

**踩坑、修正和经验：** OpenCV中文路径加载失败改用官方buffer重载；本机OpenCV不含ORT，明确采用原生OpenCV DNN CPU。首个候选计时图被质量拒绝，9ms未包含识别，废弃其性能可比性结论，保留原始证据并改用双方合格图/逐次一脸断言。原生对照原始scope曾误称同一ORT二进制，报告明确更正为同安装包/版本的独立pybind和DLL，不能过度归因语言。成熟项目代码许可不等于其权重许可；现有InsightFace演示权重非商业研究限制仍有效。质量通过数、同图相似度与识别准确率分别记录。

**后续交接：** 同事提供真实同人/异人及未入库测试图后，分开标定和冻结验收，覆盖跨相机/时段、低头、遮挡、逆光/低清并明确误认/漏认目标；不拿历史自动GID当真值。若实测不足，先定位输入/阈值/模型失败条件，再决定是否需要独立标注训练集微调。正式C++推理/Go采集调度投递迁移仍待实施；本轮C++程序仅是输入张量推理对照。图库维度/特征合同更换必须显式迁移并重建，不能给128D补零冒充兼容。完整方法、来源、原始结果和复跑注意见[性能与选型报告](evidence/2026-10-10-inference-performance/REPORT.md)、[SHA清单](evidence/2026-10-10-inference-performance/manifest.json)。未训练、未部署或修改生产/旧交付ZIP；模型筛查和600路2–3秒现场验收尚不能合并宣告完成。

## 12. 2026-10-10 准确性优先、128维复核与真实摄像头图片

**用户要求：** 自行选择合适模型，识别准确优先；已有业务服务器可访问摄像头，不应反复索要连接资料。已按此继续现场只读取图，并区分图像/模型特征与人工真值。

**方法与结果：** 固定SHA的SFace权重输出声明fc1=[1,128]，直接ORT和OpenCV两条实际推理也均为[1,128]。通过已授权SSH业务机GET旧清单192.168.1.5，型号匹配，faceRule开启；即时图片接口返回1920×1080 JPEG（199833字节），是像素而非特征向量。8.08秒事件订阅未收到face抓拍，仅收到videoloss类型XML，不能外推人脸事件格式/私有特征或实际视频丢失状态。真实图片本地研究模型检测0脸；一次冷调用178.32ms不作为容量或精度验收。未写远端文件、设备配置或业务数据，密码不落盘，摄像头采集绝对时间未知保持UNKNOWN。

**选型处理：** 保留SCRFD10g＋ArcFaceR50 512D作准确性研究基线，SFace仅轻量对照；维度大小本身不证明更准，不因候选更快就替换。512经PCA降128可减少向量存储/检索，但仍需原512网络推理，也不能与SFace128混搜；若换模型，从原照片重建图库并迁移明确特征合同。现阶段不降维、不改生产模型、不盲目微调，现有研究权重的使用范围限制继续保留。先约束误认再比较漏识/质量拒绝和延迟，重点/普通同一身份门槛，REVIEW/UNKNOWN和全库竞争必须保留。高质量抓拍和主码补图优先于放大低清图；未来多帧融合仍需单独验证，不把建议写成已实现。

**准备与经验：** 从1288条历史记录的30个相机组冻结111条[人工核验队列](evidence/2026-10-10-feature-dimension/human-review.csv)，身份留空、不导入自动GID作真值，供用户已安排的同事后续核验。连接可用可以自行采图，但不会自动产生可靠身份标签；没有真值不能宣布哪款模型最准确。抓拍规则开启与实际抓拍事件、JPEG像素与模型向量、HTTP连通与完整业务分别验证。完整决策、来源、剩余验收要求见[本次记录](evidence/2026-10-10-feature-dimension/DECISION.md)，[SHA清单](evidence/2026-10-10-feature-dimension/manifest.json)保留变更前文件与本轮证据。正式C++/Go P1迁移和3090现场容量仍未完成。

## 13. 2026-10-10 全量台账及服务器已有抓拍链路

**结果与交接：** 已只读查询850条视频设备、853条摄像头通道、168条抓拍设备及168条抓拍通道。TDengine ccic_media.vcl_face 有128个历史来源；2026-10-10 13:57（UTC+8）按utc_time复查，最近24小时8个来源1167条、最近5分钟3个来源5条，最新记录推进，确认仍在入库。每个历史来源检查一张脸图和背景图，256个当前服务器路径均HTTP200、JPEG头及尺寸可读；241张读完，其余仅前256KiB，不宣称全部完整解码。图片位于gat/gallery/face，由ccic-viis的9001服务提供。36个来源原URL仍是旧公网IP，但相同路径当前服务器可读；未请求旧公网主机或修改记录。完整结果、清单、复跑方法见[专项核查](evidence/2026-10-10-camera-server-survey/REPORT.md)及[全部摄像头CSV](evidence/2026-10-10-camera-server-survey/camera-inventory.csv)。

**发现的问题：** 128个来源仅97个精确匹配当前摄像头ID，31个未匹配；摄像头169条抓拍绑定中7条悬空，另6个抓拍通道未绑定。不能从756个通道未匹配历史记录推断无抓拍能力，462个数据库active标志也不等于实时在线。时间字段相差约8小时；即使按image_time为本地UTC+8墙钟的假设换算，近期8源的最新样本有7个仍差约193～240秒。全部时间语义保持UNVERIFIED，不擅自校时或通过≤60秒门槛。128张最新脸图中18张短边小于112，尺寸分桶不是质量验收。image_feature实际是JPEG URL，现用Java依赖ArcSoft SDK，库中2056字节BLOB也不能据此判定为128D/512D。

**方法、踩坑及经验：** 先读现有服务配置定位MySQL/TDengine/图片服务，凭据仅在进程内使用；只读事务、有限查询、4并发有界GET，不保存人脸像素或身份字段。不能因face_comparison空表或resources/face只有登记图就认定没有抓拍；TDengine时间聚合改FIRST/LAST，统计活跃源排除COUNT=0；大目录递归超时改浅层枚举和精确stat；旧主机URL与当前路径分开核验。后续可复用现有图片输入，无需先拉全部视频；摄像头映射与时间来源仍须明确。此次没有改动生产设备、Java、数据库、算法源码或部署采集器，也不构成识别精度、3090容量和C++/Go迁移完成。原始证据及文档变更前副本由本目录[SHA清单](evidence/2026-10-10-camera-server-survey/manifest.json)记录。

## 14. 2026-10-10 摄像头可读数据与原生人数计数

**实测与交接：** 从业务机直连58台已授权设备，设备信息/时间/码流配置均可读；27台当日进出报表有效，其中26台计数开启，192.168.1.60关闭且报表为0。27台均配置每日00:00:00+08:00重置，20台有非零计数，例如1.58返回进入282/离开178、1.55为135/158。数值为当日日报读取快照，不是去重人数或全场总量。完整名单、648个小时桶、44条出入口相机绑定及853条平台注册通道的数据可读覆盖见[人数计数报告](evidence/2026-10-10-people-counting/REPORT.md)与[逐机计数](evidence/2026-10-10-people-counting/people-counts-today.csv)。本轮未拉视频验证解码，码流配置可读与视频可用分开。

**服务器与时间问题：** 真正的客流表为ccic_media_master.flow_camera，32个标签来源中30个有数据，各仅1条；最近24小时6源有记录，不能当完整历史流水。14条出入口配置中13条未删除。直接GET设备时间后，58台中48台相对服务器的保守时间差区间在±60秒外，10台仅本次快照在范围内；服务器NTPSynchronized=yes，不因设备配置timeMode=NTP就认为已对时。21台完整序列号匹配平台、1台唯一access_id后缀匹配，其余保留IP/序列号待核对，未猜平台ID。1.201状态接口说counting，但规则/报表能力返回notSupport，未将其计为报表可读。

**方法、踩坑与处理：** 查现有Mapper定位真实业务库，仅读聚合人数/设备/时间字段，GET配置加只读统计POST，未调用清零或写配置。首轮误将旧CSV“登录说明”列当作密码，导致401；已保留失败证据并排除，改为从原对话确认凭据经getpass只在内存中使用，58台重新验证全部成功。相机实际返回整日24个小时桶，包括未来零桶；重复查询须覆盖同一统计桶，不能重复累加。每日清零、重复经过、重叠摄像头、方向及起始占用量均影响解读；原生聚合客流不依赖人脸身份模型。中文名称乱码复核为终端编码显示问题，上轮报告已纠正。方法、可读边界、失败处理及原始证据均纳入[本轮SHA清单](evidence/2026-10-10-people-counting/manifest.json)。未部署常驻采集、未改Java/数据库/相机、未验证事件推送或全场容量；C++/Go算法运行源码保持原样。

## 15. 2026-10-10 逐台能力清单与设备计数来源复核

**交付与范围：** 新增[摄像头能力清单 Excel](../outputs/01a1239f-6341-75f1-9b1e-4cb7656edcf5/摄像头能力清单-20261010.xlsx)，含509个已知IP、27台设备原生计数、853条平台注册通道、1436条接口请求证据。2026-10-10 14:41:17～14:41:28（服务器UTC+8）实查：58台设备信息可读；422个地址401待有效凭据、27个未连通、1个404、1个301。未知均保留待核验，不能宣传509台全部能力已确认。853条平台通道与509个IP不属同一口径，仅22条通道经完整序列号或唯一access_id后缀匹配，不相加、不猜映射。

**计数确定来自设备：** 全部27个统计请求均为业务机直连 `http://摄像头IP/ISAPI/System/Video/inputs/channels/1/counting/search` 的只读POST；请求URL、响应URL、socket实际连接IP逐个相符，均返回HTTP200及CountingStatisticsResult。表中记录进入/离开人次、24个小时桶、查询起止、返回桶范围、实读时间和响应SHA256。26台规则开启；192.168.1.60关闭且0。新快照1.58为291/183、1.55为136/158；前节数字为更早快照，不覆盖原始证据。服务器flow_camera历史上报结果在平台注册通道表单列标识，未冒充直读。

**能力核验结果：** 24台声明独立人脸抓拍能力，22台普通faceRule开启；另外2台的规则开关未由该接口确认，保留待核验。3台声明混合目标检测能力、2台当前开启。逐台分列计数、人脸、混合目标、人体/车辆/非机动车声明，越线/区域入侵/进入区域/离开区域的通道开关，码流配置，输入通道及设备全部正向能力标志。23台计数配置含detectionMode=double_single，其中6台另有algorithmMode=double；保留设备原值，镜头物理结构未靠相近型号资料强行确认。1.201各通道计数status虽返回counting，但配置不支持且未取得报表，不能算可读计数设备。能力依赖当前固件和智能资源模式，不等于所有功能同时可用、事件持续推送或身份识别已验证。

**时间与方法：** 按每次时间请求前后服务器时刻，加1秒设备时间字段粒度计算偏差区间，48台明显超过±60秒，10台仅此快照满足范围；未改设备时间。使用当前明确凭据，仅在内存中输入；其他IP只做无凭据设备信息检查，尝试只读服务配置中的唯一精确IP凭据映射未取得新增凭据，不盲试密码。requests禁环境代理、禁重定向，8并发、连接2秒/读取4秒、响应限512KiB。读取全部实际视频输入通道，避免只查通道1遗漏多通道周界配置。没有拉取视频、人脸图或身份模板，没有写相机、生产Java、数据库或部署服务。

**踩坑、处理与经验：** HTTP401代表未认证，不是设备不支持；403必须看notSupport等返回原因；配置enabled与能力标志分开，counting状态不能替代有效统计报表。接口能力是当前模式声明，不凭产品名推出双目或识别准确率。设备整日报表含未来零桶，采集时按设备/日期/桶覆盖更新，不能按轮询结果反复累加；统计人次不能代替去重人数。导出时发现ISO时间会被表格引擎自动规范化为UTC，已改用明确标注UTC+8的本地时间文本，并在导出后逐单元格核对，避免显示时差。表格以artifact-tool生成，逐表渲染核对，再解析导出XLSX逐单元格比对源数据、确认冻结窗格与无错误单元格；Python仅负责只读探测与数据整理，没有改变C++/Go运行服务。原始证据、工作簿、生成脚本、验证记录、旧文档备份见[本轮SHA清单](evidence/2026-10-10-camera-capabilities/manifest.json)。后续补齐有效凭据与平台IP映射后，复用本轮流程更新未知条目。

## 16. 2026-10-10 待核验设备补齐、校时与传输复核

**当前清单：** [补充核验 Excel](../outputs/01a1239f-6341-75f1-9b1e-4cb7656edcf5/摄像头能力清单-20261010-补充核验.xlsx) 为最新交付，旧文件保留为历史快照。用户补充剩余设备的当前凭据后，只对原451个待核验地址重查，本轮新增415台设备信息与能力可读，合并原58台校时后复核记录，共473/509台。36台仍未完成：7台HTTP401、27台连接失败/超时、1台404、1台301。表中保留原因，401没有继续试其他密码，连接失败不判定设备不支持。原58台未重复尝试新凭据。账号沿用台账admin；密码仅通过getpass进入进程内存，没有写入脚本、JSON或表格。

**能力数据：** 59台声明独立人脸抓拍，53台普通faceRule开关开启；3台声明混合目标能力，其中2台开启。27台设备原生计数报表可读、26台规则开启，本轮新增415台未新增有效计数报表。全部计数报表请求的实际socket对端与设备IP一致，来自设备接口，不是服务器转发数据。配置模式原文保留，不能据double字样直接确认物理双目。30.129及30.130设备信息接口有效但型号字段为空，型号保留待核验。共7741条合并请求证据，逐设备读取全部实际视频输入通道，配置开启、能力声明、有效报表分开。853条平台注册通道仍仅22条有可靠IP映射，未按名称猜配。服务器历史数据页明确保留前序查库日期，不冒充本轮实时上报。

**时间偏差与修复范围：** 在前一项明确校时授权下，仅修改1/2/3网段中50台原已认证设备的NTP配置：15台由手动改NTP，50台统一hik-time.ys7.com:123、间隔1分钟，8台原已合格NTP设备未改。先核对设备序列号、保存原始time/ntpServers XML，先5台试点再45台批次，写入后读回确认。没有写10/20/30网段、没有修改Java/数据库或重启业务服务。服务器时间与三个外部NTP源约20毫秒内一致；192.168.10.250的NTP响应快约253.81秒，是已验证的异常时间源，但不能据此断言它是所有相机回跳的唯一写入来源。

50台改完后，58台三次时间采样一度全部满足±60秒；后续观察到10台又快约4分钟，最后15:08:52～15:08:56复核58台三次均恢复范围内。因此不能宣称持续授时问题已经根治。回跳设备为1.45、1.48、1.49、1.50、1.51、1.59、1.75、1.78、1.193、3.73（前缀192.168.），表中时间门槛单独标记“曾回跳，待后端排查”。用户已安排后端查询其他平台重复授时，本任务不继续修改/排查该平台。新增415台全在10/20/30网段，实读时间均快252.3～254.4秒，全部超过60秒，保持只读，不自动扩展校时范围。门槛按请求前后服务器时间与设备字段1秒粒度形成偏差区间；校时后的合格快照不覆盖回跳历史。

**设备及服务器复核：** 校时前后839项设备信息/能力/规则/码流配置对比无差异，58台均可读，27台原生计数仍可读。1.196曾一次连接超时，单台复查恢复，最终比对已纳入复查证据。现有ISUP服务有39个已知相机IP在10秒窗口收到新字节，代表连接/心跳存续，不等于视频已解码。服务器第二次读取最近5分钟有5个抓拍来源、18条脸图记录，最新20对脸图/背景图共40个URL均HEAD200且文件修改时间晚于校时开始，5个来源的最新时间比第一次观察前进。抓拍服务进程保持运行，但活跃抓拍来源尚未与全部改时相机逐个映射；flow_camera未观察到新的校时后计数记录，不能称全部计数推送已验收。服务器无ffmpeg/ffprobe，未做视频解码验证。没有把心跳、可访问的图片或设备报表当成全部链路验收。

**实现、踩坑与经验：** 使用现有只读ISAPI探测，禁环境代理与跨地址重定向，8并发、2秒连接/4秒读取超时、响应上限512KiB；POST仅counting/search统计查询。旧凭据缺失造成的401与真实不支持必须区分；有凭据也不能把未知型号或缺失socket字段补成猜测结果。NTP配置读回成功和一次时间合格不足以证明持续稳定，必须保留多轮样本，外部授时冲突交后端排查。表格用artifact-tool导入原工作簿，在原4页结构内更新数据和表格范围，保留样式/冻结窗格；渲染复核并逐单元格比对导出数据。Python只用于运维探测和整理，不是替换C++/Go算法服务。原始请求、时间备份、修改响应与传输证据见[校时证据清单](evidence/2026-10-10-clock-remediation/manifest.json)，新增认证与最终工作簿证据见[补充核验清单](evidence/2026-10-10-camera-capabilities-expanded/manifest.json)。后续由后端定位冲突时间源；7台401需核对各自有效凭据，27台需查连通性，404/301需核对设备类型及服务入口。没有扩大密码尝试或修改受保护网段。

## 17. 2026-10-10 闸机后端只读查询、活动隔离及7天轨迹要求（实施前历史快照）

**用户最新明确要求：** 闸机、摄像头及人员轨迹必须对应指定活动，新活动由前端/后端创建后也要接入其独立活动上下文，不能对全活动混算。轨迹滚动保留最近7天，类似摄像头滚动存储。此轮授权为“服务器只查询”，所以只读检查了部署JAR、数据库结构/有限聚合、一个已核实查询接口和本地源码；没有部署、改变后端或算法运行源码、修改配置/数据，也没有执行验票、开闸、新增业务、消息发布或清理历史轨迹。下面的活动隔离与清理是确认的需求及实现待办，不能作为已交付功能。

**活动主键与实际链路：** 当前MySQL `ccic_media.security_activity` 有18个活动，主键为 `id`，唯一业务编号为 `activity_num`，场地键为 `venue_id`，另有start_time/end_time/type。全部18条venue_id能匹配 `security_place.place_id`。活动重点人员表 `security_activity_key_person.activity_id` 和重点视频表 `security_key_video.activity_id` 使用活动id；TDengine验票表使用activity_num，需经权威唯一映射转换，不能混用两种键。活动重点/普通规则应属于对应活动，人员身份模板可以有独立版本，但不能以全局重点状态替代活动人员规则。

场地设备通过 `security_place_device(place_id,device_id,device_type)` 关联，出入口通过 `flow_gate(place_id,gate_id)`，出入口摄像头通过 `flow_gate_camera(gate_id,camera_id)`。这些是场地共享配置，不是每场已启用的设备快照。当前14条出入口中13条未删除，44条摄像头绑定；13条绑定在当前 `device_camera.device_id` 中找不到对应。AT1012对应场地有5个未删除出入口，33条绑定全部集中在其中1个出入口，其余4个无绑定；33条中28条能在当前camera表找到。场地另有805个type=3设备，不能推导为本活动应全部采集。活动直接关联的重点视频24条涉及4个活动，AT1012为0；重点展示视频不能直接充当完整算法监控范围。无匹配数据必须保持待核验，不能猜绑定或默认为全场。

**跨活动歧义：** 现网 `FlowGateMapper.selectPageList` 按场地连接所有 `security_activity.type<>2` 的活动，并非显式activity_id过滤。快照里仅AT1012仍type=0，其余17条type=2；但AT1012计划结束时间为2026-10-06，而查询日期已是10月10日。不能把计划时间、状态字段或“最新一场”单独当权威启停依据。AT1001与AT1002存在同场地计划时间重叠，进一步证明不能仅按场地和时间猜归属。新活动应由后端提供明确选定活动、生命周期与版本化设备范围；一台相机可被不同活动复用，事件不能因此串场。未启用、未绑定或多个活动归属不明确的数据应隔离待核对。

**验票、入场与门禁数据：** `ccic_media_master.security_activity_check` 有31253条，分16个活动标签，其中15个非空；AT1012为2379条、AT1011为4000条、AT1010为6676条。这是按活动验票证据，不能用全表数字表示某一活动。标签 `1`(62条)、`123`(4条)、`003`(1条)在当前活动主表没有对应，需隔离。该表含utc_time、voucher_id(复合键)、check_time、entry_time、bind_card及人员字段、activity_num、image_addr；30033条有非空bind_card，31253条有票据ID，image_addr非空为0。未读取/导出姓名、证件/卡号值或人脸像素。最近24小时按utc_time无新记录，最新utc_time为2026-10-06T12:08:38.956Z。样例check_time/entry_time比utc_time约大8小时，原始时间语义未核定，不能自动当可信相机采集时间。

`security_activity_check2` 为DLH1000另6418条旧记录，不自动与主表合并。`security_activity_admission` 只有2条，结构是人数汇总，1条activity_num为空，不可当逐人入场。`security_activity_ticket` 12条是售票汇总，也非逐票通行。`ccic_media.gate_access_alarm` 有device_id、direction、alarm_time、utc_time、alarm_image_addr、user_id、open_type、open_door_sign等字段但记录为0；MySQL access_gate_info/device_gate_scene/person_device_gate也均为0。因此当前实查没有可验证的“本活动、某闸机、某人、出闸”链路，不能用验票、票据身份或客流计数直接关闭某个人监控。

**已找到的查询合同：** `GET http://127.0.0.1:10031/securityActivityCheck/list` 强制activityNum；可选provinceCode、startCheckTime、endCheckTime、isPage、pageNum、pageSize。用已存在活动AT1012、2099年1秒空窗口、pageSize=1，只读返回HTTP200、业务code200、data.total=0，data包含total/size/current/pages/records。首次遗漏必填activityNum返回400，已按部署字节码核对后补齐，不代表账号问题。部署网关配置把 `/api-security/**` 路由到media-security，因此候选网关路径为 `/api-security/securityActivityCheck/list`；正式网关鉴权及外部网络路径没有本轮验收，未冒用用户身份请求。另有POST `/securityActivityCheck/byBindCards` 查询与POST `/gateAlarm/list` 查询定义，未调用；写入add/update/delete、flowGate/test和设备控制接口均未调用。未确认已有专门接收本P2“按活动轨迹、版本修正、结果回执”的后端接口，不能拿验票add或门禁告警add替代。

**算法缺口已核实：** P2当前monitoring/persons、observe、gate-event、get合同无activity_id；monitoring.cpp按target_id归集证据，session_id形如monitor:target:event，没有活动隔离保证。P1/P2运行源码未找到按7天删除轨迹及其历史引用的完整保留期实现。现有短帧过期/优先级TTL不是轨迹保留期。这是实现缺项，不再仅写成“缺业务资料”，本轮未改代码。

**后续实施合同：** 新活动创建/更新后，应以 `activity_id + activity_num + venue_id + activity_revision` 建立可查询的活动上下文，明确启停及gate/camera绑定版本。全链抓拍归属、闸机事件、监控投影、轨迹查询/推送、幂等键、匿名ID及活动人员分类都必须带活动作用域；同人跨活动身份可以一致，但轨迹、进出场状态和会话按活动隔离，A活动的EXIT不能关闭B。单活动实例也须显式校验入流activity_id，不能仅前端加筛选。生命周期由后端明确启停/取消，不能单凭计划end_time截断可能延时的活动。后端新活动同步机制/鉴权、事件到具体闸机/脸图的关联仍待合同确认；本轮已取得的数据库字段不需用户重新提供。

轨迹7天按事件采集时间滚动保留168小时，不从活动结束重新计时。超窗历史迟到/补传不能重新生成过期轨迹。需要覆盖轨迹点、旧版本及refs-v2共享对象、查询索引/缓存、相关投递载荷和重放来源，避免只隐藏查询结果却仍持有全量轨迹，或删除共享对象损坏窗口内版本；当前有效监控状态与7天窗口裁剪必须保持一致。时钟未知/超限数据的隔离保留需单独有界，不能因无合格时间而永久保留。正式注册人脸库、活动主表、相机录像和后端验票历史不在本次“轨迹7天”删除授权范围，不能连带清理。实施后验证同人跨活动、共享相机、并行活动、迟到EXIT、活动版本改变、7天边界、到期不复活、重启/重放/索引和存储回收；这些尚未运行。

**方法、坑点和交接：** 没有匹配的SSH MCP，沿用固定主机指纹的Paramiko，密码交互输入仅留内存，远端脚本内存执行。MySQL使用START TRANSACTION READ ONLY及MAX_EXECUTION_TIME，查结构、关联和聚合，TDengine REST的POST只承载SHOW/DESCRIBE/SELECT。JAR只用zip读取与javap静态查看，不运行业务类。首轮jar枚举100项上限被中间件依赖占用，后续改为明确业务模块和access_layer目录，不宣称全盘穷尽；TDengine对TIMESTAMP MIN报类型错误，后续按活动使用FIRST/LAST(utc_time)成功，保留失败证据不伪造结果。部分字段注释/活动名编码异常，保留HEX，不修生产字符集、不凭乱码推断语义。网关YAML部分节为null导致本地远端探针异常，做空值处理后只读重跑。初步先看总表而漏了活动维度，用户指出后已按活动重新查询，并发现本地活动隔离和7天清理实际缺口。活动分区、孤立绑定、HTTP样本、字节码及SQL证据见[本次摘要](evidence/2026-10-10-gate-backend-readonly/summary.json)和[SHA清单](evidence/2026-10-10-gate-backend-readonly/manifest.json)。

<a id="activity-p1-maintenance"></a>

## 18. 2026-10-10 活动作用域及C++维护兼容交付

2026-10-12收尾核对：第18节证据是活动/保留交付的历史封存；之后P1恢复修复的当前程序、证据和命令见第19节。新增算法生产模块、现场联调和生产部署均未扩大。

**实际完成：** P2活动隔离、签名授权、可信UTC及168小时保留已由C++核心和Go实现，[P2第27节](../face-trajectory/HANDOFF.md#27-2026-10-10-多活动隔离及168小时滚动保留交付)是完整算法接入/构建/迁移入口。本项目Python只负责字段传递、scope键、投递幂等和C++维护握手兼容，没有新增Python期限计算模块、替换模型或重写推理。生产Java/数据库/相机/服务器未部署或写入，本轮删除仅用新隔离测试库。

**活动选择与监控：** 用户登录/退出/切换只改变签名访问context和订阅，不控制算法enabled；新活动创建、配置和明确monitoring启动独立。P1共同上下文传`activity_id/activity_num/config_version/binding_version/monitoring_version`，摄像头/闸机/观测入流另传`route_rule_id/source_id`；登记人员不要求route。不猜最新活动、不把场地全部设备作为活动范围。一台设备多个活动需显式版本化配置分别处理。person_id不拼活动前缀，分类、帧/投递键、优先提示、匿名关联与监控状态按scope隔离。缺scope旧资料隔离，不自动迁到当前活动；相同scope/source/event同内容安全重试、异内容冲突。

**新活动怎么接：** 后端先向P2同步权威security_activity.id↔activity_num↔venue_id，CAS更新config/bindings和显式enabled；按活动登记人员模板/分类，提供核验clock profile，再更新[摄像头示例](examples/monitoring-cameras.example.json)、[人员示例](examples/monitoring-persons.example.json)、[运行示例](examples/monitoring.example.json)中的同一活动版本。P1摄像头配置变更当前需重新加载服务。取有ingest/identity/monitor等必要动作的有限期签名授权，通过运行合同配置Authorization；公钥/claim样例不是实际凭据。后端创建新活动后算法核心无需改代码。共享设备route缺失、旧版本、停用后新迟到事件保持HOLD，原已提交幂等回执仍安全。

**C++如何清理全部本地存储：** P2启动重复指定`-p1-retention-db '<现存绝对本地SQLite路径>'`，包含monitoring captures/deliveries库及仍在用的旧bridge frames/outbox库。HTTP不允许指定路径；未知库/网络共享不自动创建。C++用同一个ClockAdapter验证time_record，已知采集按名义capture+168h、等于截止保留，UTC标签或created主机时间不能充当可信依据；旧无可信capture/原首次隔离证明立即过期，不给迁移新窗口。登记人脸库及`/v1/monitoring/persons`图库投递不连带删除。

清理范围包括原图/metadata/result、投递payload/receipt/error、旧frames/outbox、SQL索引列与历史空闲页/WAL、内存帧/原生结果缓存及跨P2/P1多库的binding/reentry依赖。仍有效自身事件保留capture期限与原摘要；过期引用抹除后未发送变更载荷QUARANTINED，不能用原ID发新内容。永久墓碑仅表名/键摘要/内容摘要，原摄像头/采集时间/事件原文清除，过期重新输入拒绝、异内容409，防恢复/重试重新生成旧数据。

**运行中维护：** C++维护request marker与consumer lease配合，P1全入口barrier暂停接收（HTTP503），排空已准入native/输入/发送操作，停reader、清空图片/结果/调度缓存及循环持有变量、关闭SQLite后释放租约。C++事务清理、secure_delete、VACUUM和WAL截断完成移除marker，P1同一HTTP服务重开SQLite并恢复；维护不改变后端活动启停状态。旧程序不配合时BLOCKED，物理清理中断保持暂停，下一C++owner恢复。正常关闭同样先排空再关闭DB、释放租约。

dry_run不暂停正在运行的P1、不建marker/墓碑、不改变main/WAL/SHM/控制文件：现存live辅助文件用SQLite私有只读索引，关闭库在Windows拒绝写共享句柄实际冻结后才以immutable打开，不具备安全条件时BLOCKED。非Windows外部P1冻结目前失败关闭，Linux目标全链维护需补齐支持及重新验收；核心C++活动库不受该外库冻结限制。

**怎么验证及证据：** 使用现有`.venv`和内置SQLite适配器驱动，真实HTTP服务/子进程、合成模型双桩/时间/人脸，不代表真实模型精度或摄像头/GPU容量。当前scope12项、retention10项、maintenance8项、跨库dependencies7项通过。维护测试真实kill清理子进程、验证原HTTP服务器恢复；dependencies包含真实配置P1路径的C++ worker与P2待过期散列计划，不只是Python或独立callback单元。命令从`D:\人脸算法`运行，所有测试自动新建隔离库，禁止传生产库：

```powershell
$activityCmake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$activityP2Evidence = 'face-trajectory/evidence/2026-10-10-activity-retention'
& $activityCmake -S "$activityP2Evidence/p1-retention-test" -B "$activityP2Evidence/p1-retention-test/build" -G 'Visual Studio 17 2022' -A x64
& $activityCmake --build "$activityP2Evidence/p1-retention-test/build" --config Release
$activityP1Python = 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe'
$activityP1Evidence = 'face-recognition-engine/evidence/2026-10-10-activity-retention'
$activityP1Adapter = "$activityP2Evidence/p1-retention-test/build/Release/p1_retention_test.exe"
& $activityP1Python "$activityP1Evidence/verify_p1_scope.py"
& $activityP1Python "$activityP1Evidence/verify_p1_retention.py" --adapter $activityP1Adapter
& $activityP1Python "$activityP1Evidence/verify_p1_maintenance.py" --adapter $activityP1Adapter
& $activityP1Python "$activityP1Evidence/verify_p1_dependencies.py" --adapter $activityP1Adapter --worker face-trajectory/cpp/build-activity/Release/face_trajectory_worker.exe
```

结果：[scope](evidence/2026-10-10-activity-retention/p1-scope-result.json)、[retention](evidence/2026-10-10-activity-retention/p1-retention-result.json)、[maintenance](evidence/2026-10-10-activity-retention/p1-maintenance-result.json)、[dependencies](evidence/2026-10-10-activity-retention/p1-dependency-result.json)。五轮清理保留6条有效业务行，约1.58MB页面可复用、VACUUM后主库49152–61440字节/WAL0；防复活散列19→67单独报告，不能宣称总库常量。完整Go build/vet/test、真实Go/C++上下文切换/授权/保留、存储五分项与最终SHA清单在P2证据目录。

2026-10-12复核中维护首轮恢复断言曾失败，旧断言未打印实际报告、临时库已删除；当时仅增强验证脚本保存实际失败状态和fixture，随后8次复跑通过，详见[验证历史](../face-trajectory/evidence/2026-10-10-activity-retention/validation-history.json)。用户要求继续查清后，同类恢复失败已在本地隔离库复现并修复，见第19节；缺失的原首次报告不能反推重建，本次证据不冒充那次完整错误记录或现场稳定性验收。

**启动及观察：** 按[当前运行合同](docs/monitoring-runtime.md)用新隔离state路径先`--validate`及`--capacity-report`核对配置，后者仍UNMEASURED；取得真实授权/UTC/profile后才启动采集。P2默认定期dry_run、不删除；观察`GET /v1/retention/status` attempts/successes/failures/last_result及每P1库status、表前后行数、reusable_bytes、主库/WAL和consumer健康/maintenance状态。授权客户端先`POST /v1/retention/run {"dry_run":true}`核对；另行授权生产部署后才显式加`-retention-enabled`、请求false。库清单遗漏、租约不配合、物理维护PENDING不能称全链完成。

**迁移与回退：** 停写后保存含WAL的一致性SQLite备份与匹配程序/配置/模型SHA，在隔离副本核对所有旧库路径；不把无活动记录猜迁入某场、不以created或迁移时间重置期限。回退必须成对恢复旧程序/匹配库到隔离路径，不能用旧程序读新墓碑/scope结构。恢复备份先重新取得可信UTC、清理超窗资料再恢复服务，不能重放过期缓存；备份自身留存由部署方单独管理。

**坑点、处理与经验：** 原仅帧TTL不能覆盖SQLite图片/receipt/旧bridge；只关DB不能证明native线程、HTTP在途或循环局部持有的图像已释放，必须排空和清缓存后交租约；只读打开WAL数据库也可能建辅助文件，需比对全部文件而不只比行；UTC_CAPTURE字符串可被伪造，必须复用C++核验profile；仅删除P2事件会留P1跨库引用，需查各活动源行/摘要并清理；改写待投递payload不能当原请求安全重试，必须隔离并保持原内容摘要。不能通过延长窗口、自动改event ID、扩大身份门槛或猜场地归属让测试通过。

**四层状态：** 本地实现/上述隔离验证完成；真实前后端登录切换、权限/UTC签发、结果接收、实际闸机/人脸/时钟现场联调待完成；生产部署及清理未执行；600路/3090/2–3秒容量验收未执行。前后端需配合权威活动和设备route版本、签名动作及context、不可变source/event、同次脸图关联、旧回包/推送丢弃、接收端到期拒绝及全库维护。待确认共享图库同步责任、route发布方、延时/取消启停状态、未知历史期限证明、墓碑/备份容量策略及目标平台多实例维护；保守默认沿用P2第27节。

<a id="p1-maintenance-recovery-20261012"></a>

## 19. 2026-10-12 P1维护恢复失败：隔离复现与修复

**结论和边界：** 已在新建本地SQLite复现 `INCOMPLETE / cannot replace P1 maintenance request`，定位Windows标记文件共享冲突并修复。最小同文件读取复现7个独立fixture均失败，不读标记、读取无关文件两项100轮对照通过；实际P1 Runtime/HTTP和合法PNG同样复现。诊断捕获Win32错误5、阶段PHYSICAL_CLEANUP_PENDING，说明SQL提交后发布标记也会失败。原第一次恢复失败的报告未保存，不能把本次报告冒充那次完整错误记录；本次证明的是同类故障的实际触发条件、原因和修复。没有连接或操作生产服务器、数据库、摄像头。

**如何做：** CPython3.11.9的Windows `Path.exists/os.stat` 短暂打开文件且不允许删除共享；P1状态查询与C++一次性的MoveFileExW原子替换会发生冲突。C++标记替换与最终删除仅对错误5/32/33按5ms重试，每个操作最多约1000ms，失败reason保留错误码/phase/retry_limit_ms。保留已有恢复屏障，并在COMMIT开始前设置保留标志；物理清理、统计和显式标记清除全部成功后才报告 `maintenance_request_status=CLEARED`。消费者及维护租约此时仍持有，数据库随后关闭再释放消费者租约，不以删除旧文件后新建来解决竞态。

旧实现还会忽略析构中的删除错误。受控真实Windows句柄测试已抓到旧adapter返回COMPLETE但标记仍存在；现在持续阻止最终删除约1秒后明确INCOMPLETE、标记保持，释放后下一次C++维护恢复。持续阻止初始发布时原数据不改变，旧屏障无缺口。已有SQL提交后的失败不能靠手删request恢复。

标记清除与消费者租约释放是两次操作。真实独立进程持有一字节租约时，旧P1在400ms内将正常等待记成7次maintenance_errors；释放后仍可恢复。Python只在实际锁请求的EACCES/EAGAIN抛 `ConsumerLeaseBusy`，恢复入口遇到它或新request时保持PAUSED、记 `maintenance_resume_waits`，50ms后再试。打开锁文件、标记查询、解锁、重开SQLite及原生采集错误仍走真实错误路径，不无限等待或泛吞异常。本次仅改两个既有Python文件的维护兼容，没有增加Python生产模块、修改模型或推理算法。

在途capture的finish_capture可能在request到达后触发 `MaintenancePaused`。真实合成extractor屏障捕获了该栈，旧代码把它记入inference_or_capacity_errors；现在在操作屏障内正常结束当前抓拍，先释放携图像的异常栈及局部引用，下一轮入口进入原维护等待。capture保持持久PENDING，恢复后重试，不作为推理/容量故障。不能把携图像的异常传播到屏障外再等待，否则active_operations降为0后仍可能留下热图片。控制实验验证类别，不把该单次栈当作旧压力报告所有计数的逐项溯源；旧300轮报告另外保留，当前300轮以最终源码重新运行。

**验证结果：** 使用最终源码、真实C++适配器/Windows句柄/loopback HTTP及合成时间和提特征双桩，所有库由脚本新建。没有真实摄像头、真实后端或模型精度验收。

| 验证 | 当前证据 |
| --- | --- |
| 原最小读取冲突：7/7红；修复后100/100完成且标记消失 | [最小回归](evidence/2026-10-12-p1-maintenance-recovery/marker-exists-after-01.json)、[复现摘要](evidence/2026-10-12-p1-maintenance-recovery/reproduction-summary.json) |
| 实际Runtime、合法PNG、8个HTTP查询线程：300轮恢复、17,358次health通过，维护及推理/容量错误均0；A过期删除、截止边界B保留 | [最终完整恢复](evidence/2026-10-12-p1-maintenance-recovery/pending-loop-http-final.json) |
| 短/持续发布占用，短/持续最终删除占用，保留屏障与释放后恢复，4项通过 | [Windows句柄](evidence/2026-10-12-p1-maintenance-recovery/controlled-handles-after-02.json) |
| 真正租约重叠时保持暂停、无维护错误，释放后自动RUNNING | [租约交接](evidence/2026-10-12-p1-maintenance-recovery/lease-overlap-after.json) |
| 权限/open、真实无效descriptor、marker查询、解锁错误不被吞掉；owner持锁时stop立即退出，5项通过 | [异常边界](evidence/2026-10-12-p1-maintenance-recovery/consumer-lease-error-boundaries-after.json)，open/stat/unlock为测试侧故障注入，租约/stop与CRT调用是真实操作 |
| 真C++请求打断在途capture：关闭SQLite和交出租约前decoded图像weakref均为空、在途0；pending安全重试、无误分类计数 | [图像所有权](evidence/2026-10-12-p1-maintenance-recovery/inflight-image-ownership-after.json)、[在途重试](evidence/2026-10-12-p1-maintenance-recovery/pending-loop-inflight-after.json) |
| 原维护8项，含真实kill子进程、HTTP503/410及同一HTTP服务恢复 | [维护](evidence/2026-10-12-p1-maintenance-recovery/p1-maintenance-after.json) |
| 原保留10项，含有效B、登记资料、墓碑、防复活及存储分项；原跨库依赖7项 | [保留](evidence/2026-10-12-p1-maintenance-recovery/p1-retention-after.json)、[依赖](evidence/2026-10-12-p1-maintenance-recovery/p1-dependencies-after.json) |
| 当前正式worker配原Go服务的隔离HTTP31项通过，两个Release C++构建通过 | [Go/C++](evidence/2026-10-12-p1-maintenance-recovery/service-after/summary.json)、[构建记录](evidence/2026-10-12-p1-maintenance-recovery/build-record.json) |

**可直接复跑：** 从 `D:\人脸算法` 执行；脚本没有生产库输入参数，只建立临时隔离库。每次选新输出目录，不覆盖封存。第一条旧adapter预期返回非零并保存失败fixture；随后换当前adapter验证绿。

```powershell
$env:PYTHONUTF8 = '1'
$maintenancePython = 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe'
$maintenanceEvidence = 'face-recognition-engine/evidence/2026-10-12-p1-maintenance-recovery'
$maintenanceRun = Join-Path $maintenanceEvidence ('recheck-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $maintenanceRun | Out-Null
& $maintenancePython "$maintenanceEvidence/marker_exists_repro.py" --adapter "$maintenanceEvidence/adapter-before.exe" --iterations 100 --poll-threads 1 --output "$maintenanceRun/before.json"
& $maintenancePython "$maintenanceEvidence/marker_exists_repro.py" --adapter "$maintenanceEvidence/adapter-after.exe" --iterations 100 --poll-threads 1 --output "$maintenanceRun/after.json"
& $maintenancePython "$maintenanceEvidence/pending_recovery_loop.py" --adapter "$maintenanceEvidence/adapter-after.exe" --iterations 300 --poll-threads 8 --poll-mode http --expect-inference-zero --output "$maintenanceRun/runtime.json"
& $maintenancePython "$maintenanceEvidence/verify_windows_marker_handles.py" --adapter "$maintenanceEvidence/adapter-after.exe" --output "$maintenanceRun/handles.json"
& $maintenancePython "$maintenanceEvidence/lease_overlap.py" --output "$maintenanceRun/lease.json"
& $maintenancePython 'face-recognition-engine/evidence/2026-10-10-activity-retention/verify_p1_maintenance.py' --adapter "$maintenanceEvidence/adapter-after.exe" --output "$maintenanceRun/maintenance.json"
```

当前源码构建沿用第18节CMake适配器命令及P2第27节worker命令；适配器已复制为 `adapter-after.exe`。当前worker SHA前缀535bf8b4、adapter为7c1ad7f3，原Go服务仍c7f7e2b5；完整源码/程序/证据SHA、before快照见[本次清单](evidence/2026-10-12-p1-maintenance-recovery/final-manifest.json)。旧2026-10-10封存保留历史，不能混配成当前程序的验证报告。维护仍按原启动路径、授权、UTC合同执行；没有迁移库结构或重新设定168小时期限。回退只能匹配源码/程序/一致性库备份到隔离路径，旧adapter保留仅用于故障复现，不能拿它替换当前运行程序。

**踩坑、处理和经验：** 单次复跑通过无法排除文件共享竞态，要用同路径读取/无读取/无关路径读取单变量对照；不能删除旧屏障绕过替换错误，也不能忽略最终删除错误；标记与租约交接需要显式正常等待状态。最初压力脚本编码错误、一次HTTP10053和最后清除测试未取得句柄均单独保留，不算本缺陷；后者通过真实保留32MiB数据延长VACUUM窗口，未加生产测试钩子。旧句柄测试是3项红、1项临时清除通过，不能把总FAIL写成4项全部红。一次加强Runtime测试只留下错误计数而缺异常栈，之后增加所有失败的完整报告/fixture和测试侧live traceback捕获，不降低断言。详见[诊断记录](evidence/2026-10-12-p1-maintenance-recovery/diagnosis.json)。

**交接与剩余边界：** 本地恢复修复与隔离验证已完成；真实后端联调待完成，生产未访问/未部署/未启用清理，600路/3090容量及目标Linux平台未验收。生产部署另行授权后需同步当前C++ worker和这两个维护兼容Python文件，按原合同先dry_run核对明确库清单和可信UTC；观察每库status/CLEARED、P1 PAUSED→RUNNING、maintenance_resume_waits与maintenance_errors，长期占用必须保留失败证据并由下一轮维护重试，不能手删屏障。没有新增业务确认项。

## 20. 2026-10-12 GitHub成熟算法与项目调研

按用户要求核查官方仓库、源码/模型许可和维护状态，完整结论与来源见[开源项目对照](../face-trajectory/docs/open-source-security-comparison-2026-10-12.md)。本次只更新文档，没有安装候选项目、下载权重、改业务算法或访问生产；第19节程序和测试证据不变，其封存HANDOFF摘要代表追加本调研之前的版本。

建议继续复用SCRFD/ArcFace方向，原生部署优先评估ONNX Runtime C++；OpenCV YuNet/SFace是CPU对照，SeetaFace6是较旧的C++ SDK备选。连续视频有需要时评估ByteTrack单镜关联；全身跨镜候选用FastReID/OpenVINO基线，有同步视频和投影标定再评估NVIDIA DeepStream。CompreFace适合参考图库/REST/角色管理。当前C++活动、时间、拓扑、留存与Go授权协议保持原职责。

方法与坑点：InsightFace代码MIT但官方随库权重非商业研究；模型目录授权、数据来源、代码许可需分别核对。OpenCV模型已迁往官方Hugging Face，DeepStream旧reference仓停止更新并迁入NVIDIA/DeepStream；ByteTrack/FastReID属于经典基线，不能把star数或新仓迁移当成成熟度证据。人体相似和track ID不是人脸身份确认，换模型要重建图库、重校阈值。InsightFace刚新增的PersonAnalysis只列观察项，不把单帧匹配当现成历史轨迹系统。

交接：候选复用尚未实现/实测；若继续做离线对比，固定commit/模型SHA/许可证，按真同人异人样本评估误关联、拒绝率和延迟，不直接替换生产。具体权重用途授权、视频/全身数据、同步标定及目标平台需在实施前补齐；缺失关键资料时先询问。本地调研完成，后端联调/生产部署/容量验收均未扩展。

<a id="no-downgrade-reuse-20261012"></a>

## 21. 2026-10-12 保持现有模型：减少误认与错关联的本地完善

**完成内容：** 用户指定减少误认、跨摄像头错关联并要求完善原算法。对照InsightFace官方固定提交后，业务源码仅局部修改 `src/face_engine/detector/scrfd.py`、`detector/base.py`、`quality.py`，修复同模型输入/输出和已有质量门槛的数据传递；没有重写P1推理链、增加Python生产模块、替换SCRFD/ArcFace、缩减512D或放宽质量/身份阈值。原C++活动/关联/时钟/168小时核心及Go不变；新Python工具仅离线评估。生产服务器、Java、数据库和摄像头本轮未访问或操作。

SCRFD旧代码根据任意可整除输出行数猜anchor，真实ORT错误640 heads接320输入被当作8 anchors并静默解框；当前9头固定2 anchors、15头固定1 anchor，校验输入dtype/rank/static尺寸以及实际运行各层行数/列数/batch/有限值。动态ONNX旧静态输出元信息不用于判断实际行数。另一个旧问题是检测框先clip后质量再算裁边面积，边界超限也得到0；当前新增可选 `raw_bbox` 随过滤/NMS索引传递，只恢复裁边比例测量，并验证显示框等于clip(raw_bbox)。同一边界例从错误接受改为 `FACE_CLIPPED`，提特征/搜索前拒绝；旧检测器无该字段保持原兼容，但无法找回已丢失的边界信息。

**验证与交接证据：** [完整实施记录、固定来源和复跑命令](../face-trajectory/docs/open-source-reuse-audit-2026-10-12.md)、[本次SHA清单](evidence/2026-10-12-no-downgrade-reuse/final-manifest.json)。冻结旧SCRFD8/20红、当前20/20绿（实际类加载路径和SHA已校验）；裁边质量23项、实际TrajectoryExtractor/图片入口23项、原维护8项通过。入口使用真实ORT检测和计数型512D recognizer，证明边界提特征/身份搜索为0，正常完整输出、112对齐像素与旧基线一致；GPU另用现有实际权重，两张照片320分支的512D向量差0，640两例均无脸仅为空输出一致，共4/4。这些是工程/数值验证，不是人脸准确率或600路容量验收。

P2新增 [离线评估器](../face-trajectory/tools/evaluate_identity_events.py)，现有真实C++worker和新SQLite的11个独立合成GT事件/完整23条请求，最终59项通过。比较完整作用域、误登记接受、匿名错并/错分、HOLD/PROVISIONAL和覆盖；冻结模型/政策/图库/图/时钟/每事件输入与完整重放时间线，全局及每活动不退步，防全拒绝或A改善掩盖B退步。人工 `CORRECTED`和纠错操作拒绝计入自动成绩。范围仅P2_FEATURE_EVENTS，不覆盖P1整图漏检/质量拒绝；执行和标签独立性声明仍需人工核验，详细输入格式和可运行合成范例见[P2第30节](../face-trajectory/HANDOFF.md#no-downgrade-reuse-20261012)。

**怎么接手：** 从 `D:\人脸算法` 按实施记录的fresh输出目录命令复跑，旧模块只显式加载到测试进程，红测试预期非零；当前代码正常运行入口与活动接入命令不变。本次无需迁移数据库或重提模型特征；质量改变可能使边界图得到更多拒绝，必须将此拒绝率单列，不猜成模型精度下降。若以后变更预处理/对齐/NMS或权重，需另冻基线、重提图库并用独立真实标注验收。回退只在隔离副本对照原模块/匹配资料，不让旧宽松路径替换当前生产，更不能回退到已修复的旧维护适配器。

**踩坑及经验：** 一次冻结包的__init__转到当前模块导致错误绿结果，已用显式模块路径/inspect和加载前后SHA修正，错误报告保留；动态模型元信息警告不能当实际错解证明。GPU测试初版把每分支必须检出脸当成功条件，修正为单列空输出、每张图至少一分支实际512D；入口测试对索引内部size次数过严，改为实际旧基线一致，业务未改。评估器曾缺实际默认政策/把CORRECTED算自动成绩，最终补齐实际入口政策、纠错拒绝和请求时间线。历史1,288条抓拍有自动未核验GID/模型框，没有独立身份真值，不拿它们或合成标签声称误认率下降。成熟方案复用先找当前真实缺口，并保留已有全库512D、多模板、不同人top2、时钟/拓扑深度。

另一个测试输出坑已修复：两个clipping脚本原来会写回固定模型fixture目录，当前报告及fixture都要求fresh路径，旧封存SHA保持。质量修复只作用于新输入，未自动重审/删除历史登记模板；排查旧低质量模板需原登记图与来源，不能由512D向量反推质量。

**四层状态及待补资料：** 本地上述修复与隔离验证完成；真实后端/摄像头联调待完成；生产部署未执行；600路/3090容量未验收。2026-10-12用户确认暂无人工标注，选择先完善离线验证工具；当前59项工具验证已完成，新增[标签填空模板](../face-trajectory/collection/templates/identity_truth.template.jsonl)及[填写说明](../face-trajectory/collection/README.md)。模板默认不完整，不能直接作GT。现场误认率下降、阈值标定、连续视频ByteTrack/TrackEval及更广泛NMS/对齐差分仍未验收；继续使用现有模型和阈值，取得数据后先冻结人工真值及验收集。本次无标注状态确认、模板和文档补充见[后续SHA清单](evidence/2026-10-12-no-downgrade-reuse/no-labels-followup-manifest.json)，原final-manifest保留上一轮快照。

## 2026-10-12：252网卡数量查询

用户询问252现在有几个网卡。先检查可用MCP，未发现SSH设备查询连接器；检索现有交接及2026-10-10-business-server-readonly/hardware.json，定位252为内网192.168.1.252对应的业务服务器（公网SSH入口211.139.12.141:22）。历史系统接口清单显示2个物理网络接口：eno1=192.168.1.252（UP，1000Mbps/full），eno2=211.139.12.141（UP，100Mbps/full）。另有lo、Docker网桥及veth，不能算作物理网卡；两个接口也不能据此判断是两张独立板卡还是一张双口板卡。

本轮未获得业务服务器当前SSH凭据，未连接远端，未修改服务器。结论仅为2026-10-10已核查快照，不代表2026-10-12实时状态。经验及交接：回答接口数量时分开物理接口和虚拟接口；后续实时复核需用户提供有效连接方式，仅读取ip -j addr及/sys/class/net下设备关联和链路状态，勿误连公网2222的GPU服务器。

## 2026-10-12 10:42（UTC+8）：252实时网口复核

用户补充SSH凭据后，连接211.139.12.141:22（haha），核验SSH主机SHA256指纹与既有PIN一致。复用本地Paramiko，只在远端内存执行python3 -B，读取ip -j addr和/sys/class/net的device、speed、duplex、carrier，未写远端文件或网络配置。凭据通过getpass临时输入，未写入脚本/证据/交接。

实时结果：4个物理网络接口，3个carrier=1，1个carrier=0。eno1=192.168.1.252/22，UP，100Mbps/full；eno2=211.139.12.141/29，UP，100Mbps/full；eno4np1=192.168.0.145/22，UP，10000Mbps/full；eno3np0无IPv4，DOWN，无链路。lo、Docker网桥和veth均为虚拟接口，不计入物理网口。

纠正上一节：10月10日只有2个物理接口的历史快照不可用于回答当前状态；本轮实际已有4个物理接口，且eno1当前协商100Mbps。经验：实时问题必须实时读取，以/sys/class/net/*/device关联区分硬件接口与Docker虚拟接口；接口数不等于独立板卡数。交接证据：evidence/2026-10-12-network-252/live-nics.json，复核脚本query_nics.py（需交互输入密码）。

## 2026-10-12 10:49（UTC+8）：252两网口地址迁移完成

### 授权和最终配置
用户明确要求eno1改为50.14.0.130、掩码255.255.255.128、网关50.14.0.129，并将eno1原192.168.1.252迁到eno4np1。通过211.139.12.141:22管理入口执行，账号haha；凭据只在getpass和进程内使用，不落盘，SSH指纹固定核验。

- eno1：50.14.0.130/25（255.255.255.128），网关50.14.0.129；UP、100Mbps/full。
- eno4np1：192.168.1.252/22（255.255.252.0），网关192.168.1.1；UP、10000Mbps/full。原DHCP地址192.168.0.145已去除。
- 原eno1的172.16.0.0/12、192.168.0.0/16两条静态路由（下一跳192.168.1.1）和DNS迁到eno4np1；eno1移除不再适用的内网静态路由。
- eno2地址、连接配置未修改；主默认出口仍为211.139.12.137，metric50。eno4np1配置metric101，eno1配置metric20101；实际eno1默认路由因NetworkManager连通性判断为metric40101，未抢占公网管理出口。

### 如何执行与验证
读取实时IP、路由、NetworkManager连接UUID及IPv4配置；确认eno4np1原链路能ping通192.168.1.1。备份两个目标原始连接文件至/root/codex-network-20261012-eno1-eno4（目录700），生成rollback.sh并先启动180秒systemd回滚定时器。先修改两个连接的持久设置，再激活eno1释放252，随后激活eno4np1接管252，避免两口同时持有同一IP。

实测：192.168.1.1从eno4np1 ping两次均成功；192.168.10.9的route get经192.168.1.1走eno4np1，源192.168.1.252，仅检查路由，未访问摄像头。管理客户端回程仍走eno2；另开新SSH会话成功，重新读取网口配置。两个UUID均已保存到/etc/NetworkManager/system-connections，完成后停止自动回滚定时器，确认inactive且回滚service从未启动。没有重启服务器或业务进程，未进行业务端到端/重启验收。

### 当前限制
新网关50.14.0.129 ping失败，ARP最终FAILED：只能确认eno1地址和网关配置生效，不能宣称新网络已连通。待现场确认eno1网线、交换机端口/VLAN和网关所在二层网络。未自行修改交换机、网关、主默认出口或其他接口。

### 坑点、处理及经验
初次备份假定两个连接都在/etc，eno4np1实际是/run下运行时DHCP连接，因此预检主动停止，尚未修改地址，也未启动回滚。修正为同时定位/etc和/run的目标UUID；nmcli modify后eno4连接持久化到/etc。回滚脚本会删除同UUID新生成的目标文件，再恢复原路径原文件，reload后仅重连两个目标接口，恢复eno4原DHCP方式。nmcli1.36.6不支持device checkpoint命令，本次使用systemd定时回滚与原文件备份；不把未执行的checkpoint算作保护。两个profile必须通过UUID定位，不能用本地中文连接名猜测。apply_network.py已加旧备份保护，禁止覆盖原配置后重复应用。

### 交接和回滚
本地证据目录：evidence/2026-10-12-network-252；change-preflight.json、profiles-before.json、connectivity-before.json、profile-location.json、change-apply.json（首次备份预检失败）、change-apply-retry.json（执行成功）、change-verify.json、change-commit.json、live-nics.json（10:49新SSH实时复查）。脚本query_nics.py、network_session.py、apply_network.py不含密码。

必要时经eno2管理入口手动恢复本轮前配置：

```bash
sudo bash /root/codex-network-20261012-eno1-eno4/rollback.sh
```

回滚仅适用于尚未另行变更这两个接口的当前交接状态，执行会使252回到eno1，并使eno4np1恢复DHCP。不要重复运行迁移脚本覆盖备份。

## 2026-10-12 10:57：50.14.0.129实时ping复核已通
用户要求再次ping新网关。通过固定指纹SSH连接252，执行ping -n -c 4 -W 2 -I eno1 50.14.0.129，源地址50.14.0.130，4发4收、0%丢包，平均2.299ms（1.637～3.937ms）。ARP为REACHABLE，网关MAC 44:aa:50:52:3f:c8。eno1仍为50.14.0.130/25且UP。证据：evidence/2026-10-12-network-252/gateway-ping-recheck.json。本次没有修改配置；此前10:49无ARP响应的状态已在本次复核中恢复，但未调查恢复原因，也不据网关ping外推外网或业务系统已验收。经验：网络接线或上游状态可能变化，重复实时检查后更新结论。

## 2026-10-12 10:59：20.255.254.6连通性及出口复核
用户要求ping20.255.254.6。首先默认ping，route get显示经211.139.12.137走eno2，源211.139.12.141，4发0收、100%丢包。随后按本轮新网口上下文显式执行ping -n -c 4 -W 2 -I eno1 20.255.254.6，route get oif eno1显示经50.14.0.129，源50.14.0.130，4发4收、0%丢包，平均1.575ms（1.468～1.658ms）。只读检查，未改路由或配置。证据：evidence/2026-10-12-network-252/ping-20-255-254-6.json和ping-20-255-254-6-eno1.json。经验及交接：目标通过新eno1可达，但普通业务请求目前默认仍走eno2；若需要该目标默认走eno1，需要明确增加目标路由，不能把绑定网口的ping成功当作默认路由已迁移。

## 2026-10-12 11:02：eno1/eno2硬件规格与当前速率
用户询问网口1、2是百兆还是千兆。实时/sys/class/net读取两口均100Mbps/full。服务器未安装ethtool命令，未安装软件，改用Python标准库ctypes/fcntl读取SIOCETHTOOL/ETHTOOL_GSET（只读），两口supported和advertising均包含1000baseT/Full，自动协商开启，当前speed均100Mbps。结论：两个都是支持千兆的网口，目前链路协商为百兆。未读取到对端advertising，不能确定降速原因；需进一步核对网线、交换机端口支持速率和端口配置。本轮未修改速率、协商或网络配置。证据：evidence/2026-10-12-network-252/eno1-eno2-link-capabilities.json（命令不可用及sysfs实时值）、eno1-eno2-ethtool-ioctl.json（硬件能力读取成功）。经验：硬件能力和当前协商速度分别报告，不能仅凭100Mbps读数判断网卡只有百兆规格。

## 2026-10-14：双卡 3090 P1 绑卡与多实例启动交接

**如何做的：** `src/face_engine/onnx_backend.py` 现在接受 `cpu`、`cuda` 和 `cuda:<id>`；`cuda` 未指定编号时读取 `FACE_ENGINE_CUDA_DEVICE_ID`。解析出编号后向 CUDAExecutionProvider 传 `device_id`，ORT 默认 `intra_op_num_threads=2`、`inter_op_num_threads=1`，日志会输出实际 `device_id` 和最终生效后端。`src/face_engine/config.py` 增加同一设备格式校验；`monitoring_service.py` 的 CUDA 失败保护同时覆盖 `cuda:<id>`。

新增 `scripts/run_dual_3090_workers.py`：四个实例固定为 `p1-gpu0-a:cuda:0:8091`、`p1-gpu0-b:cuda:0:8092`、`p1-gpu1-a:cuda:1:8093`、`p1-gpu1-b:cuda:1:8094`，各自使用独立 P1 SQLite outbox，全部将 `endpoint` 指向同一个 P2 地址。传入四项 cameras 模板时按索引分配并写回明确 source_id；没有 cameras 文件时仍可生成配置并启动无采集源的 worker。先用 `--dry-run` 检查配置，再去掉该参数启动；脚本通过 `FACE_ENGINE_CUDA_DEVICE_ID` 与模型 YAML 双重固定卡号。

**验证：** `face-recognition-engine/.venv/Scripts/python.exe -m pytest -q face-recognition-engine/tests` 为 5 passed、11 subtests；Python 编译检查通过；fake ORT 检查覆盖显式 `cuda:1`、环境变量 `cuda:0`、线程数和 provider 参数；dry-run 检查四端口、四 source_id、同一 P2 endpoint 生成正确。

**踩坑与经验：** 配置切片 `cuda:<id>` 的冒号索引必须从位置 5 解析，已修正并重新验证；仅 `get_available_providers()` 不能证明 CUDA DLL 可加载，仍以 session 实际 provider 为准。启动脚本不会下载权重、替换 P2 或宣称服务器容量；目标服务器需要另行提供已核验模型、相机模板、P2 地址和授权环境变量。

**交接边界：** 本节完成本地代码与启动编排；未在双卡 RTX 3090 主机启动四个真实实例，未做 P2 实时并发写入、端到端延迟、温度/显存、摄像头现场或 600 路容量验收。生产启动前保留各 P1 state_file，先 dry-run 和 `/health`，再用同一 activity/source/event 合同做有界并发验证。
