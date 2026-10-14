# 摄像头采集、海康抓拍与主码流补图

2026-10-10，本地实现位于 `src/face_engine/acquisition.py`。采集模块将原始尺寸 BGR 图和来源元数据交给监控调度器，再复用 SCRFD、五点对齐和 ArcFace 比对。本文是技术接口资料；项目总体交接仍维护在唯一 `HANDOFF.md`。

## 调用方式

```python
from face_engine.acquisition import CameraAcquisition, HikvisionEventReader

camera = CameraAcquisition(
    sub_url, main_url, source_id="capture-service-v1", camera_id="camera-001",
    ffmpeg=ffmpeg_executable, sample_fps=10,
    main_min_interval_s=5, main_timeout_s=8,
).start()

# 调度器按重点/普通/第三类配额决定取样，采集槽始终只保留一张最新帧。
frame = camera.latest(after_frame_id=previous_frame_id, max_age_s=2)
if frame:
    result = process_face_frame(frame.image_bgr, frame.metadata)
    previous_frame_id = frame.metadata["frame_id"]

# 质量不足时在独立线程请求；本调用有等待，不要占住主调度线程。
main_frame = camera.request_main("LOW_FACE_QUALITY", after_frame_id=previous_frame_id)
if main_frame:
    process_face_frame(main_frame.image_bgr, main_frame.metadata)

# 有海康原生抓拍时可直接接收图片。回调只做有界入队，不能同步跑推理。
events = HikvisionEventReader(
    alert_url, "hikvision-isapi-v1", "camera-001", bounded_queue_offer,
    username=username, password=password,
    expected_channel_id=1, clock_profile_version=None,
).start()

events.stop()
camera.stop()
```

`Frame` 包含 `image_bgr`、`metadata`、`received_monotonic`。图像数组只读，消费端如果要画框需自行复制。每次 `latest()` 返回独立的元数据副本；修改副本不会污染采集槽。`latest()` 不阻塞，返回 `None` 表示没有新帧、帧已过期或正在重连。

海康订阅 URL 从已授权配置读取；支持 `GET /ISAPI/Event/notification/alertStream` 所返回的 `multipart/mixed` / `multipart/form-data`。Reader 使用 Digest 认证、TLS 默认证书校验、禁止重定向、禁用环境代理、5 秒默认 socket 超时和重连退避。401/403 使用最大退避。不会调用相机配置写接口，也不会自动设置 `httpHosts`、抓拍规则或视频参数。

相机和 RTSP URL 可含凭据，运行配置使用环境变量引用，示例见 [monitoring-cameras.example.json](../examples/monitoring-cameras.example.json)。示例中的相机 ID、URL、设备通道和角色均须替换成实际清单；没有自动用 IP 猜 camera_id。FFmpeg 可在 PATH 或通过 `ffmpeg` 指定绝对路径。不要把含口令的 URL 输出到日志；本模块的诊断只含状态、计数、异常类型或 HTTP 状态码。FFmpeg 子进程命令行仍包含输入 URL，应使用部署账号的进程访问权限保护凭据。

## 子码流、主码流和时延

持续子码流用独立 FFmpeg 子进程解码，输出无损 PPM 后转 BGR，保留输入原始尺寸。没有为了“看清”先放大再宣称获得更多人脸像素。采集默认每秒输出 10 帧，业务调度器再决定实际推理频率。视频解码目前走 FFmpeg CPU；3090 的推理 provider 由识别模块选择，采集模块不自行抢占或回退 GPU。

一槽覆盖旧帧，防止算法变慢时出现长队列；默认只接受本机收到后 2 秒内的最新帧。该年龄是排队控制，不代表真实相机采集延迟。首帧超时默认 12 秒、帧停滞默认 5 秒，看门狗终止子进程再以 1～30 秒退避重连。FFmpeg 4.x 的 socket 参数为 `stimeout`；新版本是 `timeout`。启动时读取当前构建的 RTSP 帮助确认，避免把旧版 `timeout` 当成 socket 超时而意外切换成监听模式。

`request_main()` 每次创建一次性主码流读取器，默认每台摄像头两次尝试间隔至少 5 秒，同一相机最多一个补图请求。默认等待 8 秒后终止；退出还需要有限的子进程回收时间。失败和超时也计入限频，防止低质量画面反复开流压垮设备。首次探测 FFmpeg RTSP 参数最多另外等待 10 秒；可在服务就绪预检时调用 `_rtsp_timeout_option(ffmpeg)` 预热。`stop()` 若未在限定时间内停止会抛 `TimeoutError`，不会报告虚假成功。

主码补图使用新的 `frame_id` 和自身时间来源，只用 `fallback_of_frame_id` 记录请求关系。不能复制旧帧的人脸框、人员身份或采集时间；补图中人物可能已经移动或换人，必须重新检测、提特征、比对。

## 时间证据

所有记录都有 `source_id`、`camera_id`、`frame_id`、`stream_kind`。`stream_kind` 为 `sub` / `main` / `hikvision`。本机 `received_at_unix_ms` 与 `received_monotonic` 仅用于接收记录、队列年龄和退避。

普通 RTSP 解码拿不到已经核验的相机绝对采集时间，因此默认输出 `time_source=UNKNOWN`、`clock_domain=UNKNOWN`、`clock_uncertainty_ms=-1`，不生成伪造的 `captured_at_unix_ms`。可由 `time_provider(metadata)` 返回与当前帧明确关联的原始 `time_record`；没有该关联时必须返回 `None`，不能拿主机时间、随手读到的相机当前时间或上一次抓拍时间填充。

官方 `faceCapture` JSON 的 `targetAttrs.faceTime` / 抓拍条目的 `faceTime` 保留为 `time_record.raw_time`；有配置时加入 `profile_version`。顶层 `dateTime` 是报警触发时间，只放入 `vendor_event_time`，不自动视为图像采集时间。XML 机型事件未建立完整时间字段映射，因此只保留原始事件时间且不认证采集时刻。

`time_record` 仍须经过 P2 的校准和 `CAMERA_CAPTURE_60S` 校验后才能成为已认证时间。拥有时区后缀也不证明相机偏差小于 60 秒；未校准、超限或语义未知时继续保留 HOLD。这个模块没有连接现场进行校准。

### 可选 GStreamer 相机参考时钟

对于需要从视频流保留相机时钟的部署，`CameraAcquisition(..., backend="gstreamer", gst_python=...)` 使用 `GStreamerReferenceReader`，默认 `backend="ffmpeg"` 保持原路径。相机示例配置包含 `backend` 和 `gst_python_env`。先调用 `gstreamer_backend_status(python_path)` 进行不连接相机的依赖预检；指定 GStreamer 但缺依赖时明确报错，不默默切到没有时间来源的 FFmpeg。

该 Python 环境需要 numpy、PyGObject/`gi`、GStreamer **1.22 或以上**、Gst/GstVideo introspection，以及 `rtspsrc`、RTP/RTCP、对应 H.264/H.265 解码器、`videoconvert`、`appsink` 插件。Windows 可使用独立准备好的 Python/GStreamer 环境；不要求当前人脸推理 venv 强行安装系统库。此轮没有安装这些系统依赖。

实现启用 `rtspsrc.add-reference-timestamp-meta`，在解码后的 **同一个 appsink buffer** 同时读取像素和 `GstReferenceTimestampMeta`。参考时间需要相机发送 RTCP Sender Report / NTP-64 RTP 扩展，且中间解码、色彩转换元素保留 metadata。没有参考、多个 reference、不支持的 clock caps、无效时间都保持 UNKNOWN，不拿上一帧的时间、pipeline PTS 或主机接收时间补全。

仅接受 `timestamp/x-ntp`：它是从 **1900-01-01** 起计的整数纳秒，不是 Unix epoch，也不是 RTP 包里的 NTP 32.32 编码。转换后输出带 `Z` 的毫秒文本，保留完整 `reference_timestamp_ns`、不足 1ms 的截断量和 clock caps。负数、非整数、`CLOCK_TIME_NONE`、uint64 越界被拒绝；不会用当前电脑日期猜 NTP era。相机时钟 profile 还须覆盖 RTP 参考到实际采集时刻的语义、RTCP 插值误差和文本量化损失，才能认证为不超过 60 秒的采集时间。

GStreamer 运行在独立子进程，复用父进程的硬超时、重连和一槽策略。配置经 stdin 传给子进程；输出中每张图的 metadata 和像素按一个帧消息绑定。appsink 一槽丢旧帧；读取时处理真实行 stride 和 plane offset，避免把行填充当像素。主码首次抓帧会在请求时限内短等首个参考（最长 5 秒、且不超过首帧时限的 75%），到期没有参考时仍返回 UNKNOWN。高清图与子码图仍有各自 frame_id，参考时间接近也不用于强行绑定人物。

官方依据：[rtspsrc 属性](https://gstreamer.freedesktop.org/documentation/rtsp/rtspsrc.html)、[rtpjitterbuffer 参考时间说明](https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html)、[GstReferenceTimestampMeta](https://gstreamer.freedesktop.org/documentation/gstreamer/gstbuffer.html#GstReferenceTimestampMeta)、[GStreamer 维护者对 RTCP SR/NTP-64 的说明](https://discourse.gstreamer.org/t/extracting-ntp-timestamps-from-an-rtsp-stream/1622/2)、[参考时间默认 1900 epoch](https://gstreamer.freedesktop.org/documentation/pango/timeoverlay.html)。

## 抓拍解析边界

`HikvisionMultipartDecoder(content_type, source_id, camera_id, ...)` 可同时用于订阅流或后端/设备回调，调用 `feed(bytes_chunk)` 得到 `Frame` 列表。每次最多传入 64 KiB，有限回调最后调用 `finish()` 确認 MIME 未截断。HTTP 回调鉴权、请求总大小和有界入队由服务入口负责。

- JSON 按已公开的 `faceCapture[] → faces[] → contentID` 和背景图 `targetAttrs.contentID` 绑定图片，读取 MIME `Content-ID`；缺少该头时，只接受与引用完全相同的表单 `name`。不按到达顺序、文件名或“上一张脸”猜测归属。
- 每个 metadata part 开始新事件；断线重连新建 decoder。无引用的孤图丢弃，缺图片的旧事件计为 incomplete。支持序列化的 metadata→图片传输；同一连接不同事件交错传图、重复使用 contentID 又没有事件标识的协议无法可靠关联，需要设备协议适配，不宣称已支持。
- 单个事件中重复 contentID 引用或重复图片直接拒绝。多张脸图片有不同帧 ID，背景图完整保留供独立检测。`faceId`、`traceUuid`、设备名单匹配结果不写成人员 ID；多脸背景不能把设备的单人标签广播给所有脸。
- XML 只接受 `EventNotificationAlert`、明确的 `faceCapture` / `faceSnap` 类型和显式 contentID，图片角色标记 `unclassified`。不解释未知私有字段，不支持 HCNetSDK 报警二进制结构，不承诺所有海康型号/固件通用。
- 默认每 part 8 MiB、metadata 256 KiB、每事件 16 MiB / 32 个图引用、每图 3200 万像素。JPEG 先检查 SOF 尺寸再解码；拒绝超限、重复 JSON 键、NaN、过深 JSON/XML、XML DTD/实体、截断和冲突长度。
- 不自动下载事件里的图片 URL。需要 URL 传输的机型，应让已鉴权后端按标准图片接入合同推送；SDK-only 设备还需要对应 SDK 的真实样例。

相同事件重传的 `frame_id` 由事件 UID（没有时用 metadata 语义哈希）、contentID 和图像哈希稳定生成，接收时间可以不同。metadata 语义哈希忽略官方的传输计数 `activePostCount` 和重传标记 `isDataRetransmission`，保留抓拍时间、内容引用等证据字段；JSON 使用规范化序列化，XML 仅去除对应顶层传输字段。消费端应先按 `frame_id` 去重，避免把临时接收时间混入不可变观测的内容哈希。源事件 UID 只用于事件去重，不作为人员身份。

## 验证、踩坑和交接

本地验证脚本 [verify_acquisition.py](../evidence/2026-10-10-monitoring/acquisition/verify_acquisition.py)，结果 [results.json](../evidence/2026-10-10-monitoring/acquisition/results.json)。2026-10-10 共 65 项检查通过：真实 FFmpeg 解码本地 MJPEG 视频、BGR/原始尺寸、一槽/过期/停止、主码补图新事件与限频、阻塞管道看门狗与重连、碎片 MIME 与各项拒绝条件、真实 loopback HTTP Digest 认证/重连，另有重传计数稳定性、NTP epoch/越界/歧义、行填充与同帧传输契约检查。当前环境 `gi` 缺失，结果明确记录 `native_gstreamer_execution_verified=false`，**没有验证本机原生 GStreamer 管线或真实相机 RTCP 时钟**。全部图片和协议记录是明确的本地合成夹具，没有连接相机；不能据此声称真实人脸精度、现场抓拍兼容、3090 峰值容量或端到端低延迟已验收。

另完成 [stream_integration.py](../evidence/2026-10-10-monitoring/runtime/stream_integration.py) 的 21 项实际运行接线检查，[最终结果](../evidence/2026-10-10-monitoring/runtime/stream-integration-final/results.json)。它实际调用 `MonitoringRuntime.configure_cameras/start/stop`，由 FFmpeg 读取本地低质量视频，真实 CPU 模型拒绝模糊脸并触发主码补图，补图通过质量与 512D 提取后经 SQLite outbox 送入独立的 Go/C++ P2，因无相机时间得到 HOLD。另一条真实 loopback Digest 抓拍链路使用明确的合成时钟/人员资料，完成比对并以入口未知开启监控。共 4 次已认证回调（包含重传和错误通道）最终只有 1 条有效持久化抓拍；错误 NVR 通道被服务配置指定的 `expected_channel_id` 过滤。

这轮接线验证还暴露并修复了两处问题：订阅流必须绑定 NVR 通道，否则设备不同通道会共用错误的 camera_id；停机时取消尚未运行的主码任务，不能依赖任务函数的 `finally` 清理 pending 标记。服务改为停止提交、完成/取消回调清理、先等待推理线程退出后关闭线程池。最终检查确认推理/发送/取样、采集和主码线程均退出，pending 集合为空，未出现推理/容量内部错误。首次失败证据保留在 `runtime/stream-integration-first/failure.json`，不能当作最终通过报告。

本机初始没有 FFmpeg 命令；验证用 `pip download --no-deps imageio-ffmpeg` 下载 wheel，只提取其自带 FFmpeg 7.1 到 `.tools/acquisition-ffmpeg/`，未向运行 venv 安装新包。部署机需准备自己的经过核验的 FFmpeg 可执行文件；工具目录、wheel 和开发环境不应混进正式源码交付。旧部署中已有看门狗与 RTSP 参数探测，沿用同一成熟方法并补入一槽/主码请求/事件解析，而没有依赖线程中不可中断的 `VideoCapture.read()`。

关键坑：将收到时间当相机时间会绕过误差门槛；用同一张大图的 vendor 身份标记所有脸会污染轨迹；直接拼接“上一条事件＋下一张 JPEG”会在丢包重连后错绑；分辨率放大不能恢复缺失特征。当前分别通过未认证时间保留、逐脸重新比对、contentID 同事件绑定与状态重置、原尺寸质量评估解决。

官方依据（2026-10-10 阅读）：[FFmpeg RTSP 协议文档](https://ffmpeg.org/ffmpeg-protocols.html#rtsp)、[海康 faceCapture JSON 数据定义](https://open.hikvision.com/hardware/v2/JSON%E6%96%87%E4%BB%B6/EVENT_JSON_faceCapture.html)、[海康事件接收示例](https://www.hikvision.com/content/dam/hikvision/fr/guides-pratiques/ipc/Comment-r%C3%A9cup%C3%A9rer-une-alarme-en-temps-r%C3%A9el-en-mode-d%C3%A9coute-HTTP.pdf)、[海康 ISAPI 通用事件布防说明](https://enpinfo.hikvision.com/unzip/20210507165058_24612_doc/pdf.pdf)。实际使用前还须核对具体机型支持的事件格式和经授权的连接参数。
