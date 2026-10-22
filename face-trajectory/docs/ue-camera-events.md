# UE摄像头出现事件

2026-10-21 接入调整：当前只对业务后端提供按次查询，新增人员信息与抓拍照片 URL，**不持续推送、不处理 UE 前端**。请优先使用[后端人员与抓拍查询合同](backend-person-query.md)。下文 UE/主动推送内容保留为既有模式说明，不代表本次启用。

2026-10-21：UE根据摄像头名称和时间线绘制轨迹；算法只提供“这个人在什么时间经过哪台摄像头”的记录，并标记重点人员。设备名称是“南门检票大棚”“A1看台入口”这类场地点位名称，不是摄像头型号。此模式不建立空间路线，不需要坐标、场景或NavMesh。

## UE请求一次、返回一次：人员摄像头时间线

**接口：`POST /v1/monitoring/timeline/query`**，Content-Type为`application/json`，Authorization为后端签发的`Bearer <token>`，需要当前活动的`query`权限。服务地址与端口由实际部署配置提供，默认开发监听为127.0.0.1:8081。

每次请求返回一个JSON结果，不建立订阅、不自动重复发送，也不因查询触发后台推送。UE按`timeline`顺序取设备名称和时间，映射UE已有摄像头点位后自行生成显示轨迹。只需这种查询方式时，启动服务不配置`-push-url`和`-push-results`，前端不用调用订阅或路线接口。

请求示例（时间段为2026-10-21北京时间18:00–19:00；人员和活动为占位示例）：

```json
{
  "activity_id":"ACTIVITY-ID",
  "config_version":1,
  "binding_version":1,
  "monitoring_version":1,
  "person_id":"DAMAI-PERSON-ID",
  "from_unix_ms":1792576800000,
  "to_unix_ms":1792580400000,
  "limit":500
}
```

三个版本号必须换成后端当前活动配置值，不是固定填1。查询区间为`[from_unix_ms,to_unix_ms)`；时间是UTC毫秒，UE显示北京时间时转换UTC+8。

返回的业务字段示意（实际HTTP回复还带活动作用域、版本和当前用户上下文）：

```json
{
  "ok":true,
  "person_id":"DAMAI-PERSON-ID",
  "category":"PRIORITY",
  "is_priority":true,
  "association_revision":7,
  "timeline":[
    {"camera_id":"CAM-SOUTH","camera_name":"南门检票大棚","captured_at_unix_ms":1792578600000},
    {"camera_id":"CAM-A1","camera_name":"A1看台入口","captured_at_unix_ms":1792578720000},
    {"camera_id":"CAM-INNER","camera_name":"内场入口","captured_at_unix_ms":1792578900000}
  ],
  "count":3,
  "has_more":false,
  "next_after":null
}
```

上例对应18:30南门、18:32 A1入口、18:35内场。只表示这些摄像头拍到该人员，不补两点之间实际走过的通道。每条真实记录另含`source_id`、`observation_id`、`clock_uncertainty_ms`和`camera_name_source`。同名设备仍按`camera_id`区分；同一摄像头的不同抓拍分别保留，不把重复相机名当重复事件删除。

查询按当前权威人员归属返回：人工纠正身份后，记录从旧人员时间线移到新人员时间线；重点标记使用当前上传资料的分类。设备名称优先用该次出现时已保存的点位名称。切换模式前的旧记录缺名称快照时，可用当前权威设备绑定，标记`camera_name_source: CURRENT_ACTIVITY_BINDING`；无名称则返回null/UNAVAILABLE，不用型号、相机ID或猜测地点替代。

默认每次最多500条，`limit`范围1–1000。`has_more:false`表示该次查询区间内剩余记录已返回；无记录时`timeline:[]`。记录更多时，UE下一次主动请求携带`after: <next_after>`以及`expected_association_revision: <上一页association_revision>`，其余人员、时间范围和活动版本保持一致。迟到记录或归属纠正改变了时间线会返回409/VERSION_CONFLICT，此时重新从第一页查询，避免漏项或重复。服务不会自行追发后续页。

只返回滚动168小时内的合格抓拍，旧查询窗口被截断时带`history_pruned:true`与`effective_from_unix_ms`。按名义抓拍时间排序，保留时钟误差；误差区间重叠不构成精确物理先后。HOLD/身份待决不进入这条确认时间线。

常用错误：400为人员/时间/limit参数无效，401缺少有效签名，403没有活动查询权限，409为活动版本或分页修订变化。完整字段见`openapi.yaml`中的`UECameraTimelineRequest/Reply`。

## 大麦人员资料与摄像头名称

上传方沿用现有 `POST /v1/monitoring/persons` 登记接口：大麦业务人员ID原样作为 `person_id`；重点人员映射为 `category: PRIORITY`，普通人员为 `NORMAL`。照片经现有P1提取同一模型特征后登记。重点标记不降低识别阈值，所有兼容图库人员仍参与竞争比对。没有提供大麦原始字段合同，本次没有另造大麦接口或猜测其重点字段。

活动配置使用 `POST /v1/activities/sync`，在原有作用域、CAS字段之外设置：

```json
{"configuration":{"output_mode":"UE_CAMERA_EVENTS"}}
```

活动设备绑定的每条 `kind: CAMERA` 项增加 `camera_name`，名称应与UE中的设备名称一致。例如：

```json
{"source_id":"capture-source","device_id":"camera-id","kind":"CAMERA","route_rule_id":"current-route","camera_name":"10136看台A1下入口1"}
```

完整调用仍携带当前活动ID和对应版本。新活动先同步配置、再配置带名称的绑定、再明确启动监控。既有活动切换时，先在旧配置版本下更新带名称的绑定，再用新的配置版本启用模式；已有无名称绑定会阻止切换。最后刷新P1与后端使用的活动版本。模式省略时仍为 `TRAJECTORY`，不会自动改写运行中的活动。

名称取自已提交的活动设备绑定，不信任抓拍自行声明的 `camera_name`。同名设备仍以 `camera_id` 区分；名称缺失或全空白不能启用。改名需要新的绑定版本，不改写旧事件名称。

## 入流与上报

沿用 `POST /v1/monitoring/observe` 的人脸特征、policy、活动版本和 `time_record`。UE模式下可省略 `graph`，旧P1继续附带的graph不会用于路线求解。policy的 `require_topology` 和 `topology_is_complete` 必须为false；无需登记空间图。

时间和身份均通过后，HTTP回执增加 `output_mode` 和 `camera_event`；`trajectories` 为空。HOLD不生成UE出现事件。仅检测出一张脸不等于命中重点人员；低质量、身份歧义、时间不可用或未绑定摄像头不会被当成重点命中。原匿名规则保留，未登记目标明确标为 `UNREGISTERED`、`is_priority: false`。

配置既有daemon的 `-push-url` 后，通过原持久出站队列主动POST。`result`对人脸出现事件缩减为：

```json
{
  "ok":true,
  "status":"ACCEPTED",
  "output_mode":"UE_CAMERA_EVENTS",
  "camera_event":{
    "event_type":"PERSON_APPEARANCE",
    "person_id":"DAMAI-PERSON-ID",
    "category":"PRIORITY",
    "is_priority":true,
    "person_revision":1,
    "association_revision":7,
    "camera_id":"camera-id",
    "camera_name":"10136看台A1下入口1",
    "source_id":"capture-source",
    "observation_id":"original-face-event-id",
    "captured_at_unix_ms":1791626400000,
    "clock_uncertainty_ms":0
  }
}
```

外层原包络保留 `activity_id/activity_num`、三版本、`event_id/sequence`、入队时刻和原保留期限。上例为合成字段示意，不是实际大麦人员或现场时间。UE消费 `result.camera_event`；订阅用户也可从原 `/v1/subscriptions/poll` 的每条事件直接读取 `camera_event`，不再查询路线。登记/归属修正等控制推送保留原合同，不能作为新出现记录绘点；归属修正仍按原观察键及修订处理撤销或重归属。

`captured_at_unix_ms` 是相机名义抓拍的UTC毫秒，显示北京时间时由接收方转换UTC+8；它不是服务接收时间或推送时间。保留误差字段，时间区间重叠的事件不能据名义时间断言精确先后。重点标记和名称属于该次处理快照；名单更新后新的出现事件使用新人员修订。

普通、重点和未登记已确认出现均可上报；重点标记由人员资料决定。同相机新的抓拍照常报告；同一活动/source/observation重试不产生第二条事件。接收端按原 `(activity_id,event_id)` 幂等落库并校验版本/期限，再返回2xx，名称不是去重键。

## 验证与启用

本地源码和新程序需配套使用。回归脚本 `tools/verify_ue_camera_events.py` 使用新隔离SQLite、合成图库和时钟，验证无需场景/graph即可识别、名称可信、重点/普通标记、抓拍时间、去重、HOLD不上报、订阅与持久推送内容及不生成路线快照。

现有HTTP发送器和签名授权不变。实际部署需将新worker配给daemon、由后端同步上述活动配置/设备名称/大麦人员资料并配置接收URL。本次没有连接或修改生产服务，也没有在用户UE工程中添加代码。
