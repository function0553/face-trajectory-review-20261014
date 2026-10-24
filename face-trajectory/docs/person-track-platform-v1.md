# 人员轨迹三方协议 v1.0：算法侧接入

2026-10-22。按2026-10-21《人员轨迹三方接口对接协议_v1.0》改造。当前接入由平台手动导入固定名单，算法主动推送已确认单脸抓拍，驾驶舱只查询平台。原有 `/v1/monitoring/timeline/query` 保留用于联调核对，不再作为平台保存轨迹的轮询来源。

## 活动与服务配置

活动 `configuration.output_mode` 设为 `PERSON_TRACK_EVENTS`。此模式复用已验证的时间及人脸准入，跳过空间路线计算；只推送已登记且 `category=PRIORITY` 的已确认记录。匿名、NORMAL及HOLD不推送；已有推送归属纠正为名单外人员时发送RETRACT。原有TRAJECTORY和UE_CAMERA_EVENTS模式仍按其原推送合同运行，必须为本次对接明确选择新模式。

启动服务时保留现有鉴权、公钥、可信UTC证明和SQLite配置，增加：

```powershell
$env:FACE_TRAJECTORY_PUSH_AUTHORIZATION = 'Bearer <平台专用push-token>'
$env:FACE_TRAJECTORY_SQLITE_SYNCHRONOUS = 'FULL'
# 在现有 face-trajectoryd 启动命令追加以下参数：
# -push-url 'https://<平台地址>/api-security/algorithm/person-track/events'
```

`-push-url`启用持久入队和投递；URL未就绪可先用`-push-results`仅入队。上线必须同时配置URL与专用凭证。凭证放请求头，不放JSON。URL按完整路径配置，生产使用HTTPS。此模式应使用自己的服务实例/状态库；一个发送器的URL服务于该实例所有活动，避免把旧格式活动混入新平台接收地址。活动模式调整不转换已经存在的旧队列。

SQLite使用现有可配置的FULL同步模式，强化每笔提交的落盘保证。断网时须保持状态库、WAL及磁盘完整，不能只复制主库或换空库后启动。

平台活动ID直接作为`activity_id`；`activity_num`为已有活动映射编号，`venue_id`为场馆映射，二者建立后不可静默改变。`person_id`使用平台keyPersonId；摄像头平台设备ID写入绑定的`device_id`和抓拍的`camera_id`。`source_id`使用稳定的采集来源配置，不能使用进程ID或启动时间。单画面多张脸共用frame_id，各自使用不同observation_id。

## 可执行的首次登记样例

先准备已部署P1使用的真实模型配置、单人照片与平台可访问的档案照URL。登记与实时抓拍必须使用同一模型、对齐和预处理契约。下面的工具复用P1实际检测、质量校验与512维特征提取，生成四个完整JSON文件，没有空向量占位，不访问远程服务。

从`face-recognition-engine`目录执行，照片路径、正式模型配置和实际平台ID按联调值替换；输出目录必须是新目录：

```powershell
$P1Python = '<已安装P1依赖的Python解释器绝对路径>'
& $P1Python ..\face-trajectory\tools\prepare_person_track_import.py `
  --model-config configs/default.yaml --photo 'D:\登记照片\PERSON-001.jpg' `
  --activity-id ACT-001 --activity-num PLATFORM-ACT-001 --venue-id VENUE-001 `
  --source-id source-01 --camera-id CAM-001 `
  --person-id PERSON-001 --person-name '测试人员' `
  --profile-photo-url 'https://photos.example.test/profiles/PERSON-001.jpg' `
  --output 'D:\联调\person-track-first-import'
```

只有恰好检测到一张脸且质量合格才生成模板。`04-person.json`为可直接POST的完整登记请求，包含活动三版本、姓名、档案照URL、category=PRIORITY、**revision=1、expected_revision=0**、evidence_id和完整templates。工具输出`registered=false`表示仅生成请求，尚未导入。

平台具有`config/monitor`权限的现有算法签名Bearer放入环境变量`FACE_TRAJECTORY_AUTHORIZATION`（与平台push-token不同），按顺序发送：

```powershell
$AlgorithmBase = 'https://<算法服务地址>'
$ImportDir = 'D:\联调\person-track-first-import'
$Headers = @{ Authorization = $env:FACE_TRAJECTORY_AUTHORIZATION }
$Steps = @(
  @('01-activity.json', '/v1/activities/sync'),
  @('02-bindings.json', '/v1/activities/bindings'),
  @('03-monitoring.json', '/v1/activities/monitoring'),
  @('04-person.json', '/v1/monitoring/persons')
)
foreach ($Step in $Steps) {
  $Reply = Invoke-RestMethod -Method Post -Uri ($AlgorithmBase + $Step[1]) `
    -Headers $Headers -ContentType 'application/json; charset=utf-8' -InFile (Join-Path $ImportDir $Step[0])
  if (!$Reply.ok) { throw ($Reply | ConvertTo-Json -Depth 20) }
  $Reply | ConvertTo-Json -Depth 20
}
if (!$Reply.recognition_ready -or $Reply.submitted_revision -ne 1) { throw '人员模板尚未就绪' }
```

这是新活动首次初始化样例。已有活动直接沿用当前三个版本，仅逐人调用`/v1/monitoring/persons`，不要重新用expected=0初始化活动。新增一个人员仍用1/0；失败项原样重试，同一活动同一person_id不会新建重复人员。同一revision不同内容返回409；需更新资料/模板时使用新revision及当前expected_revision。仅保存照片URL的新人员会400，整笔回滚。`HTTP200 + ok=true + recognition_ready=true + submitted_revision匹配`表示模板与人员已经同步提交生效，无需异步状态查询；模板被删除后重试历史登记不会谎报就绪，应以新修订重新登记。

已有P1批量导入也可沿用`monitoring_service --enroll ...`及`--flush-only`，但排队不等于导入成功，必须检查ACKNOWLEDGED中的上述P2业务回执。不要与正在运行的P1实例并发启动第二个消费者。

## 主动推送与修订

HTTP正文严格为十二个必填字段，nullable字段也显式发送null：

```json
{
  "event_id": "pt:<稳定事件摘要>",
  "event_type": "UPSERT",
  "revision": 1,
  "activity_id": "ACT-001",
  "source_id": "source-01",
  "observation_id": "capture-001-face-0",
  "person_id": "PERSON-001",
  "camera_id": "CAM-001",
  "camera_name": "看台A1下入口1",
  "captured_at_unix_ms": 1792578600000,
  "clock_uncertainty_ms": 0,
  "capture_photo_url": "https://photos.example.test/captures/capture-001.jpg"
}
```

平台按`activity_id+source_id+observation_id`定位已上报抓拍。ID区分大小写，接收端仍按事件ID及修订处理幂等。算法新增上报过滤：同一活动、同一已确认person_id，在已保留抓拍的采集时间前后5000毫秒内（含边界）只保留一条；跨摄像头、跨来源及同摄像头均适用，不依赖横杆分组或名称。以先处理并持久入队的有效抓拍为代表，保留其实际摄像头ID、设备名称、时间及照片；被去重的抓拍不延长窗口，超过5000毫秒可产生新记录。不同人员或不同活动不合并。首发revision=1，event_id按该复合键及revision生成并随不可变请求体一起落盘。

这项5秒过滤是本次明确追加的业务规则；原始识别证据仍独立保存，调试时间线可能包含被过滤的抓拍。接收端不应再按姓名、时间自行合并已收到的事件。去重索引和候选抓拍与识别、outbox同事务持久保存，跨重启保留，并随168小时历史清理；仍被有效候选引用的代表保留至候选过期。

身份纠正沿用`POST /v1/associations/correct`与`identity`活动权限：

```json
{
  "activity_id": "ACT-001",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "source_id": "source-01",
  "observation_id": "capture-001-face-0",
  "target_id": "PERSON-002",
  "evidence_id": "independent-verified-correction-001"
}
```

新的名单内归属产生完整UPSERT，修订递增，event_id变化。撤回使用同一接口，`target_id:null`及独立撤回证据，产生完整RETRACT、person_id=null。相同归属的重复纠正不重复发消息；撤回后再次确认继续递增。原抓拍标识、摄像头、采集时间、误差、照片及已推送的点位名保持不变。尚未确认的抓拍只有在具备合格原始时间和人脸证据并得到独立确认后才会首发。

纠正结果若已被同一人员的5秒代表覆盖，原已推送记录改为RETRACT；从未推送的重复抓拍不生成多余撤回。代表被纠正或撤回时，仍然有效的其他摄像头抓拍可成为新代表，以该抓拍自己的ID和元数据发送UPSERT。一次纠正可能产生多条事件，全部同事务入队，容量不足全部回滚。

平台只在TDengine和必要归属状态全部保存后返回HTTP200，正文`{"code":200,"message":"保存成功","event_id":"<原event_id>"}`。算法同时检查HTTP200、整数code=200和匹配event_id；202/204、空正文、错误code或错ID都保留消息。旧修订被平台忽略但满足上述确认条件也算完成。

网络错误、超时、429/5xx以及未确认响应按1、2、5、10、30秒退避，之后保持30秒，同活动顺序投递、不同活动独立退避。400/422需修复参数/人员/设备映射，401/403需恢复凭证/活动权限，409必须双方排查；均不丢弃或篡改原事件，保留并记录错误后继续重试。更换启动环境中的凭证需重启服务。单次总超时最多10秒，同时受现有可信时钟租约约束。

识别变化、抓拍修订和SQLite outbox同事务提交。未确认事件跨服务重启及168小时历史清理持续保留，清理不改写载荷；只有业务确认后移除。队列上限每活动256、全局10000，满时返回503并回滚本次新增/纠正，由上游保留原请求重试，已有消息不丢弃。需监控受权`GET /v1/push/status`的pending、磁盘容量与投递错误。

## 照片、查询与联调边界

档案照和抓拍照分别由平台/采集方上传到双方可访问的存储，算法仅传URL，不上传二进制、不用档案照替代抓拍照。observe的`observation.capture_photo_url`须为完整HTTP(S)地址，缺失为null；URL有效期及续签由图片存储方负责。PERSON_TRACK_EVENTS的名称只取P1设备查询快照`observation.camera_name`，设备未配置、启动尚未查询成功、查询失败或缓存过期均发送null，不回退到表格/活动绑定名称。历史已入队事件不改写。

## 设备通道名称查询配置

当前设备查询适配海康ISAPI：Digest鉴权GET设备的`/ISAPI/Streaming/channels/<stream-id>`，读取匹配通道的`channelName`，校验响应id，不使用设备整机名称、其他通道名称或表格名称。接口参考[海康官方流通道示例](https://international-robot.hikvision.com/upload/web/1476067342641247/20220902/57811662081257893.pdf)。例如主/子码流101/102属于物理通道1，201/202属于通道2；配置必须与实际采集通道相符。

在P1的每条camera配置加入（完整样例见`face-recognition-engine/examples/monitoring-cameras.example.json`）：

```json
"device_name": {
  "protocol": "hikvision_isapi",
  "url_env": "MONITOR_CAMERA_NAME_URL",
  "username_env": "MONITOR_CAMERA_USER",
  "password_env": "MONITOR_CAMERA_PASSWORD",
  "refresh_seconds": 5,
  "timeout_seconds": 2
}
```

`MONITOR_CAMERA_NAME_URL`填写真实设备HTTP(S)地址、实际管理端口及上述通道路径；账号密码放对应环境变量。视频RTSP端口与HTTP管理端口不能混用，不能从平台摄像头ID猜通道号。若已配置并启用Hikvision alertStream、账号密码及expected_channel_id，且未设置device_name，自动复用该连接查询该物理通道的第一路流。单纯RTSP地址不包含可靠的管理接口地址，需补充device_name连接配置。其他厂商协议尚未适配，不能将普通ONVIF profile名当作点位名。

查询由4个后台任务执行，默认每次查询结束5秒后刷新，单次网络等待2秒；新抓拍使用最近成功的有效快照，不阻塞图像采集或识别。设备改名在下一次成功刷新后生效；查询失败立即清空名称，超过刷新间隔加两倍超时的快照也不使用。大量设备排队或网络故障时可能暂报null，而非承诺所有设备每5秒必定完成刷新。P1 `/health` 的device_names给出状态及查询年龄，不输出账号、密码或URL。厂商回调重试保留首次入库的名称快照，避免改名导致相同抓拍的内容冲突。

算法查询仍为原有左闭右开时间范围、游标分页及168小时窗口，仅供核对。平台自己的列表、page_num/page_size分页、TDengine保留期和驾驶舱定时刷新按三方文档开发，不使用算法查询窗口决定平台保留期。本次不增加重放、事件处理状态、自动补数或SSE接口。

本地验证使用隔离SQLite、合成人员/特征/时钟和模拟接收端；实际模型样例只复用既有P1提取流程，正式权重、人员照片和阈值以现场配置为准。正式地址、专用凭证、图片存储及TDengine接收端尚需三方联调，未修改生产服务。
