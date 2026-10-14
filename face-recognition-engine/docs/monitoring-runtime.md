# 人脸识别与重点监控运行接口

2026-10-10。本说明是运行合同；当前实施结果与剩余验收事项以[唯一交接](../HANDOFF.md)为准。P2 0.13 的[监控接口](../../face-trajectory/docs/monitoring.md)负责活动内身份关联、会话、轨迹事务及168小时清理；签名授权与完整接入见[多活动合同](../../face-trajectory/docs/activity-service.md)。

本文件描述的是已验证的Python P1原型运行入口；C++/Go正式交付仍需迁移P1运行层并验证等价性。P2本身始终为C++17核心+Go接口，具体边界及业务服务器实查见交接第10节；下述Python启动命令不代表已完成正式架构迁移。

本轮Python修改仅活动字段传递、幂等兼容与C++维护握手，不更换模型、不重写推理。用户登录/活动选择与P2监控启停独立；P1暂停输入或用户关闭页面不改变P2活动监控enabled。底层状态和图库均按活动隔离，登记身份库与活动类别分别同步；相同人员ID可以显式登记到多个有权活动。

## 已确认规则

- 人员库提供稳定 ID、重点/普通类别、照片；同一个人可登记多张合格照片。
- `PRIORITY` 重点、`NORMAL` 普通、`UNREGISTERED` 当前未匹配到库的匿名人员都可形成轨迹。第三类不代表已证明其不在尚未交齐的全量人脸库中。
- 闸机验票不等于确认身份。只有明确绑定已确认人脸观测的 ENTRY/EXIT 事件才能影响对应人员的进出状态；不根据时间接近自动猜人。
- 漏入口仍启动监控并标记 UNKNOWN 入口；明确出闸结束；以后再次出现可重新启动。误差区间重叠时保留歧义，继续监控。
- 三类使用同样的质量、相似度和不同人员竞争差距门槛。优先级只影响调度。

## 运行链路

Python `monitoring_service` 自行拉取视频、接收海康原生抓拍或后端推图，经统一质量检查、SCRFD 检测、五点对齐、ArcFace 特征进入持久投递队列。P2 使用完整特征契约和多模板图库做实际人脸比对，同事务保存关联、监控输入和自动重建的轨迹。Python 不再维护第二份用于此服务的 NumPy/FAISS 人员库。

图片原始脸尺寸、瞳距、清晰度、边界和姿态代理指标在对齐前测量。低清放大不增加有效脸像素，不使用生成式补脸提高相似度。图片质量拒绝原因保留；低清视频可按需请求主码流。新主码帧独立检测、比对和计时，不能继承旧子码脸的身份或采集时间。

2026-10-12同模型兼容修复：SCRFD运行时检查输入和每层输出的实际形状，9头每格2个anchor、15头每格1个anchor，错误资料明确拒绝；动态ONNX的旧输出元信息不作为实际行数。内部检测结果的 `bbox` 仍为显示裁边框，可选 `raw_bbox` 为裁边前原图坐标框，仅用于已有裁边比例门槛，并校验两框一致。裁边超限返回 `FACE_CLIPPED`，在ArcFace和身份搜索前停止；旧检测器无该字段保持兼容，但无法补回已丢失的边界信息。模型、512D、质量/身份阈值及活动接入合同不变，复现和限制见[交接第21节](../HANDOFF.md#no-downgrade-reuse-20261012)。

实时视频按相机保存最新一帧，队列过期帧会丢弃并计数。AdmissionPlanner为尚未入队的相机按类别及最久未成功接纳顺序分配机会，入队失败保留等待年龄；已在队内的帧可原子刷新，刷新不花掉新入队轮次。临时字节不足时保留能装进空队列的大帧机会，永久超大帧拒绝并计数。默认重点/普通/匿名的新入队及繁忙队列配额均为 6:3:1，队内同类别 FIFO；抓拍和投递队列也按三类轮转，普通与匿名保有处理机会。默认目标采样频率为 6/2/1 fps，**不是保证吞吐率**。尚未比对的画面按普通处理；确认有活动重点人员后短时提升对应相机的后续采样频率，画面内所有人脸仍参与比对。有效EXIT仅清除当前活动该人的提升提示，A的EXIT不清除B；用户退出登录不改变P2后台监控或其它工作人员。旧回执不能覆盖新版本EXIT状态。

默认实时帧最长排队 2 秒；过载时处理更新帧而不累计旧视频。抓拍事件有持久队列，不能像视频一样静默覆盖。队列权重、采样频率和过期时间均是可调工程初值，需要目标机实测。

2秒队列上限不等于2秒全链路：补主码还包含连接、等关键帧和重新检测，默认取流超时8秒、并发上限2，不能承诺每次补图在3秒内返回。需按实际主码GOP、网络、首帧耗时和高峰请求量分别测量；质量失败仍保留拒绝原因，不能沿用旧子码身份作为补图结果。

## 时间来源

摄像头名义时间及其核定误差沿用 P2 `CAMERA_CAPTURE_60S`，总误差不得超过 60000ms。只有相机时钟 profile 不能把没有采集时刻的图变成合格时间。

| 输入 | 时间依据 |
| --- | --- |
| 海康抓拍 | 同一 multipart 事件所引用图片对应的 `faceTime`；顶层报警触发 `dateTime` 不擅自替代采集时间 |
| 后端推图 | metadata.time_record 提供有来源的 raw_time 与 profile_version |
| 普通 FFmpeg 解码 | 没有可靠同帧绝对时间，保持 UNKNOWN；接收时刻只用于本地调度 |
| 可选 GStreamer RTSP | 1.22+ `rtspsrc` 携带同一解码 buffer 的唯一 NTP reference meta，经 1900 epoch 转换后交给 P2 核验；没有参考或参考有歧义仍 UNKNOWN |

可选 GStreamer 需 PyGObject、对应 GStreamer Python 运行时及解码插件。本机无 `gi`，原生管线与现场 RTCP 时间映射未验收。相关原理和前置检查见[采集说明](acquisition.md)。先确认相机支持、同帧映射、profile 来源和偏差，不把 UNKNOWN 改成服务器当前时间。

P2新HOLD按首次可信隔离时间最多保留168小时；缺可靠原期限的P1未知时间载荷采用立即到期。不能把本地received/created时间当作可信采集时间，重复与恢复不能重计时。P2同 `(activity_id,source_id,event_id)` 的所有原内容不可变，补时间、reconsider或后补binding均返回409；现有公开接口没有原键补证修订能力，不换ID重放同一物理事件。需补证时先由后端约定独立有版本且保留原期限的合同。

## 准备与启动

1. 准备许可适用于本项目的 SCRFD/ArcFace ONNX 权重，在 `configs/default.yaml` 指定路径。仓库的 demo 权重用于本地研究验证，不作为现场交付权重。人脸库照片晚些提供时按下述命令登记，模型 SHA、对齐和预处理变化后须重提图库特征。
2. 准备 P2 同一版本的 worker/daemon，单状态库单写入实例。新 Windows 成对产物为 `face-trajectory/cpp/build-activity/Release`；目标为 Linux 时需在目标平台重新构建，不能直接运行 Windows exe。
3. 复制并填写 `examples/monitoring.example.json`、`monitoring-cameras.example.json`。所有路径相对运行配置文件；摄像头 ID 要与 P2 使用的一致。RTSP 地址、海康账号密码通过指定环境变量提供，不写进公开示例。海康抓拍必须配置 expected_channel_id，避免 NVR 其它通道图片被绑定到当前 camera_id。
4. 后端先同步权威activity_id/activity_num/venue_id、配置与设备绑定范围，再明确启动监控。每相机activity_context须含activity_id/activity_num/config_version/binding_version/monitoring_version/route_rule_id，登记清单逐人含同一活动五字段和独立source_id/event_id。共享设备由权威后端选择明确活动route，不因共享场地自动扇出；旧缺作用域请求隔离，不发送到默认活动。绑定修订后刷新P1配置并重启/重载本原型，旧持久请求不可偷改版本。先向 P2 导入经核定的 clock profiles；场地模型未交付时使用显式未核定空图，只形成观测摄像头序列和未知连接。

在新的隔离本地目录准备后端授权公钥、UTC权威公钥及挑战证明文件。完整启动命令、参数及首次证明流程见[多活动服务合同](../../face-trajectory/docs/activity-service.md)。现有无鉴权0.12启动命令已不适用；下面给出本地命令结构，实际issuer由受信系统提供：

```powershell
& 'D:\人脸算法\face-trajectory\cpp\build-activity\Release\face-trajectoryd.exe' `
  -worker 'D:\人脸算法\face-trajectory\cpp\build-activity\Release\face_trajectory_worker.exe' `
  -state-file 'D:\人脸算法\face-trajectory\runtime\activity-local-demo\isolated.sqlite' -addr 127.0.0.1:8081 `
  -auth-public-key-file 'D:\人脸算法\face-trajectory\runtime\activity-local-demo\auth-public-keys.json' -auth-issuer '<受信后端issuer>' `
  -clock-public-key-file 'D:\人脸算法\face-trajectory\runtime\activity-local-demo\clock-public-keys.json' -clock-issuer '<受信UTC权威issuer>' `
  -clock-proof-file 'D:\人脸算法\face-trajectory\runtime\activity-local-demo\clock-proof.txt' `
  -p1-retention-db 'D:\人脸算法\face-recognition-engine\runtime\activity-local-demo\p1.sqlite' -retention-interval 1m
```

在 P1 项目目录运行：

```powershell
.\.venv\Scripts\python.exe -m face_engine.monitoring_service --config examples/monitoring.example.json --validate
.\.venv\Scripts\python.exe -m face_engine.monitoring_service --config examples/monitoring.example.json
```

配置里的示例相机/照片标记必须替换后才能采集；`--validate` 只检查运行与模型配置格式，不连接相机、不加载权重、不证明设备可用。

P1的 `p2_authorization_env` 默认 `FACE_TRAJECTORY_AUTHORIZATION`，值为完整受信签名Bearer，由后端有限期刷新；不能把P1 HTTP machine token当作P2用户授权。P2 UTC权威必须核验参考来源并快速签发当前 `/health` challenge，30秒挑战、120秒最大证明租约、总误差不超过1000ms；历史NTP记录或机器墙钟不能替代。P1库须先存在，P2 `-p1-retention-db` 不创建缺库；在新隔离配置路径执行P1 `--flush-only` 可初始化空库且不加载模型（有待投递数据时会尝试网络，禁止将它指向生产或旧库），`--validate` 本身不建库。上述删除默认关闭，仅每分钟统计；在隔离库明确启用删除需增加 `-retention-enabled`。

3090 使用 `device: cuda`。新运行服务若请求 CUDA 但任何模型实际退回 CPU，会明确停止启动。低压力允许 CPU 时，把所选模型配置明确设为 `device: cpu`，测量目标并发和 P95 延迟后使用。不能根据 GPU 型号或一次照片耗时推断全场容量。CUDA、cuDNN 和 ONNX Runtime 的兼容版本以[官方说明](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)核对；demo 权重范围见[模型官方说明](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md)。

## 人员登记

填写 `examples/monitoring-persons.example.json`。每张照片原始检测结果必须恰好一张脸且质量合格；禁止从多人图里挑一个合格脸冒充单人登记。

```powershell
.\.venv\Scripts\python.exe -m face_engine.monitoring_service --config examples/monitoring.example.json --enroll examples/monitoring-persons.example.json
.\.venv\Scripts\python.exe -m face_engine.monitoring_service --config examples/monitoring.example.json --flush-only
```

第一步只完成实际提特征并持久排队，第二步尝试投递；查看 ACKNOWLEDGED 后才是 P2 已确认。P2 人员类别和照片模板同事务登记，revision/expected_revision 防止覆盖新资料。同一状态文件只运行一个 P1 消费实例；不要在运行服务之外同时启动另一个 flush 发送器。

正式监控宜先登记当前活动计划使用的图库。若已经生成 `auto:` 匿名轨迹，后补图库不会自动把匿名ID改成业务人员ID；两者竞争时保持相同身份差距门槛。经独立核验后按P2[归属修正合同](../../face-trajectory/docs/association.md)带活动三版本及观察source_id提交correct，并重建所有affected_target_ids；历史版本受滚动期限，不能改表或只改显示名称冒充合并。

## 后端回调

默认监听 127.0.0.1:8092。非 loopback 监听必须提供配置指定的 bearer token；密钥不是模板字符串。

- `GET /health`：实际模型后端、工作线程、实时队列/过期/替换计数、admission各类成功接纳/刷新/容量推迟计数、live_camera_coverage相机处理间隔、持久待办与隔离数量、推理和排队 P50/P95、P2 回执耗时。
- `POST /v1/captures`：`{image_base64, metadata:{activity_id,activity_num,config_version,binding_version,monitoring_version,route_rule_id,source_id,camera_id,frame_id,time_source,time_record?...}}`。image_base64是JPEG/PNG文件编码，最大12MiB、1600万像素，HTTP正文最大17MiB。回包202仅说明图和元数据已落盘；缺活动作用域为隔离状态，身份/轨迹由P2后续返回。摄像头既有配置不会覆盖后端自报歧义归属。
- `GET /v1/captures/status?capture_id=...&activity_id=...`：两个值须URL编码，activity_id必须与持久记录一致，错活动403。PENDING表示等待提特征，PROCESSED表示已记录处理结果；report含质量拒绝原因及observation_ids，各observation的delivery_status和p2_status区分排队、隔离、ACKNOWLEDGED、HOLD。维护后历史report/receipt可能被清空，当前详情用P2活动查询；接口不返回原图或向量。
- `POST /v1/gate-events`：转发P2显式活动五字段、route、source/event及ENTRY/EXIT合同，可在首次请求给出包含source_id/observation_id/evidence_id的binding。不允许把票号当作人员ID；原事件已保存后补binding属于冲突409。

后端取得 observation_id 后，仍须依据独立的同次通行证据完成闸机绑定；查询到了附近人脸，或两条记录时间接近，都不构成自动绑定依据。无绑定的出闸事件保留，不能关闭某个猜测人员的监控。

抓拍frame_id和闸机event_id对同一原事件须稳定；作用域至少activity_id/source_id/event_id，同键同内容重试，变内容409。received_at_unix_ms不参与抓拍语义指纹，重传不因本地接收时刻不同而冲突。P2回执的活动五字段不匹配即隔离，不能更新另一个活动的调度提示。400类永久错误进入QUARANTINED，网络/繁忙按原键退避；二者都受168小时上限，不因重试继续保留。

清理墓碑命中的相同原capture/gate请求返回410/EXPIRED，改变原内容仍409；不能恢复原图、载荷或重新排队。P1回调token仅鉴别接入系统，不能代表业务用户权限；页面访问和活动选择必须使用P2签名授权查询。

队列有独立的抓拍与投递容量。单次抓拍扇出至多max_faces_per_capture（默认1024）个观测，允许一次有界扇出余量，避免抓拍池满时无法生成发送项而死锁。数据库默认1GiB，满时明确503；168小时清理由P2 C++/Go执行，不用Python生产清理模块。原图、metadata/result、delivery payload/receipt/error及旧bridge frames/outbox都须列入P2启动的全部P1库路径，不能继续无限归档保留。登记人员投递排除，算法不删除后端验票历史或录像。

外部清理发布 `<db>.maintenance.request` 后，匹配P1停止接纳完整操作、等待进行中的推理/发包结束、清空实时队列及提升提示、关闭SQLite连接并释放 `<db>.consumer.lock`；C++取得租约后才删除/VACUUM/截断WAL。P1 `/health.maintenance`显示RUNNING/QUIESCING/PAUSED/RESUME_FAILED等状态和active_operations。物理维护成功移除request后，P1重新取租约、重开库并恢复输入，不沿用旧图片或回执缓存。未匹配旧P1、尚有消费者或物理清理失败时保持BLOCKED/INCOMPLETE或PHYSICAL_CLEANUP_PENDING，不能宣称全链清理完成；下一轮在同一库安全续做。不要手删request解除未完成维护。

2026-10-12 Windows恢复修复：状态查询的文件元数据读取可短暂阻止request替换或删除。C++对Win32错误5/32/33以5ms间隔重试，每个标记操作最多约1000ms，保持原子替换与现有暂停屏障；成功报告新增 `maintenance_request_status=CLEARED`，必须在消费者和维护租约仍持有时完成物理清理、统计与标记清除。永久占用/权限错误仍返回INCOMPLETE并保留错误码、操作阶段及重试上限；已有恢复屏障及COMMIT开始后的失败不能自动解暂停。释放冲突句柄后由下一轮C++维护恢复，不能手动删除屏障来绕过。

request清除与消费者租约释放之间允许有短暂窗口。P1仅把实际锁请求的EACCES/EAGAIN作为 `ConsumerLeaseBusy`，恢复入口遇到该异常或新维护请求时保持PAUSED、累计 `maintenance_resume_waits`，50ms后再试，不无限阻塞维护线程。打开锁文件、读取marker、解锁、重开SQLite或重启采集的真实异常仍计入 `maintenance_errors`，不会被正常等待分支吞掉。

在途capture完成写入时遇MaintenancePaused，保持持久PENDING并在操作屏障内正常返回，先释放异常栈和局部图像引用，再允许暂停线程交出租约；下一轮入口执行维护等待。该安全重试不计为推理/容量错误，不能将携图像的异常栈带到屏障外睡眠。

P1未知/超限时间载荷没有可信首次隔离依据，C++采用立即到期，留下不可逆逻辑键/内容摘要墓碑阻止重放重新入库；有合格原采集时间的未归属旧记录仍按capture期限。窗口内capture图像/metadata保留，历史report/receipt清空后用P2查询当前状态。墓碑会随不同事件累计，1GiB及总文件增长仍须容量规划。默认只有dry_run，删除需另行授权部署和明确启动flag，首次在生产副本统计核对。

仍存活的P1待投递载荷若引用已到期脸/闸机证据，会清除过期引用并标记QUARANTINED，保留原不可变摘要与自己的采集期限，不能改写后继续发送或重置幂等键。当前Windows的离线dry_run在读取时对主库/WAL/SHM持有拒绝写入句柄；活库的既有WAL/SHM使用只读读取，当前非Windows的外部P1冻结dry_run明确fail closed，避免为了统计创建辅助文件；此平台限制不影响C++核心。Linux全链维护需补齐冻结支持并在目标平台重新构建验证，不能拿Windows证据代替。

P2 紧凑回执表示原事务的结果；重复旧请求可能返回历史 ACTIVE，而当前已经 CLOSED。调度提示比较人员/关联/闸机修订，不倒退；完整当前状态用 `/v1/monitoring/get`，完整轨迹用 `/v1/trajectories/get`，历史查询携带版本。

## 上线验收边界

本地实际照片和模型链路、合成身份/时钟、模拟视频、loopback 海康协议服务各自说明其验证范围。尚需实际人脸库同人/异人标签、海康目标型号事件样本、可靠相机时间及校准、3090服务器的摄像头数量与并发压测。正式场地模型与设备绑定交付后再核定通路；图纸电气连线和摄像头顺序连线不能当作人员详细路线。

用户已确认约600路、目标结果延迟2秒、3秒可接受。运行示例的 deployment_targets 记录此目标。600路若全部连续按普通2fps检测，需要1200张图/秒；全重点6fps则为3600张/秒，这只是配置算术，原生抓拍相机应按实际事件率另计。当前FFmpeg每流单独解码，单3090全600流及2–3秒全链路尚未证明。带原生抓拍的相机可只配置hikvision、不填sub_url_env；同NVR的多个通道已共享订阅，各通道仍必须唯一明确绑定。

填好实际输入组合后，先运行不联网的规划命令：

```powershell
.\.venv\Scripts\python.exe -m face_engine.monitoring_service --config examples/monitoring.example.json --capacity-report
```

报告分别列全场目标、实际已配置、占位/未配置、连续流、原生抓拍及双输入数量，不能把一个示例相机当成600路覆盖。默认assessment为UNMEASURED。可选capacity_measurements须填source_evidence_id、peak_native_capture_images_per_second、qualified_faces_per_image、sustained_images_per_second、sustained_p2_events_per_second、result_p95_seconds；这些是同负载的实际测量输入，规划器只做算术比较，不认证其真实性或外推到别的图库/设备。

`/health`另有live_camera_coverage：连续拉流相机从未处理数、最长未处理时长和按当前调度类别统计的同相机处理间隔。它与结果延迟/队列帧龄不同；帧持续替换可以保持很新，却不能证明每台相机都及时被处理。事件驱动的海康抓拍不计入持续流覆盖间隔，避免将没有报警误报为漏处理。

目标机容量验证先从少量实际相机开始，再提高到计划路数和峰值人员密度。预热后保存健康快照，例如在默认本机监听下执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8092/health | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 runtime/health-evidence.json
```

配置了 token 时请求需附带相同 Authorization。同时记录模型 SHA、图库人数/模板数、视频分辨率与码率、相机数量、实际 CUDA/CPU 后端、采样频率和持续时长。比较推理/P2 回执 P95、普通队列已处理计数、过期帧、待投递最老年龄和 HOLD/隔离数；只有吞吐足够且这些指标稳定，才能选用该负载的 CPU 或 GPU 配置。健康接口的回执耗时从本机收到/处理图片起算，不包含相机到本机的传输时间；相机时间允许 ±60 秒，不能直接拿两端时间戳之差证明一秒级延迟。

默认 1GiB 状态库是开发容量保护值，达到上限会拒绝新增数据。部署前按实测事件率、每条数据大小和保留时长设置容量及归档流程；长期保留与满库处理需一起验收，不能只测几张照片后持续无人值守运行。
