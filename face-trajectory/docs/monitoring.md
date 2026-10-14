# 人脸识别、分类与闸机监控接入（0.13.0）

本地 C++ 统一管理每活动人脸库比对、监控状态、自动轨迹重建与168小时清理；Go 提供签名授权、查询、订阅、推送及周期调度。P1 负责照片/视频帧检测、质量处理、特征提取与采集调度。人员类别为 `PRIORITY / NORMAL / UNREGISTERED`，三类使用同样的身份确认条件；重点类别不能降低匹配阈值、掩盖普通人员竞争者或绕过时间条件。真实图库和场地模型可以后接，缺少图的阶段使用明确的未核定空图。

所有请求都要求受信后端签名 Bearer 和显式活动作用域，见[多活动服务合同](activity-service.md)。创建活动、用户选择、明确启动监控是三个独立操作；退出、关闭订阅与切换不会停止后台监控。身份库首版按活动登记，分类、匿名ID、会话、关联及轨迹版本均隔离；相同人员业务ID保持原值。

全部 monitoring 接口要求启动时设置持久 `state-file`。一次接收的原输入、归属、模板增删、监控证据、轨迹版本和可选推送出站消息由已有 SQLite 事务统一提交。容量或重建失败时一起回滚，恢复热索引。生产服务和相机没有因这些本地接口而升级。

## 人员登记

`POST /v1/monitoring/persons`：

```json
{
  "activity_id": "provided-activity-id",
  "activity_num": "provided-authoritative-activity-num",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "person_id": "provided-person-id",
  "category": "PRIORITY",
  "revision": 1,
  "expected_revision": 0,
  "evidence_id": "library-import:2026-10-10:record-1",
  "templates": [
    {"template_id": "library-photo-1", "feature_space": {}, "embedding": []}
  ]
}
```

示例中 `feature_space`、`embedding` 是结构占位，必须替换为实际提取器的完整模型契约和512维非零有限向量。登记照片与抓拍必须进入同一特征空间；不因两者都512维就允许混比。最多64张模板一次提交，模板ID不能属于其他人员，也不能占用 `assoc-template:` 保留前缀。

`revision` 必须大于 `expected_revision`；首发 expected 为0，之后按当前登记修订号更新。相同修订、相同内容可重复确认，内容改变返回409；旧 expected 返回409。省略 templates 只更新类别，不清空原图库；带 templates 按ID增补/替换，不隐式删除未列出的照片。人员类别和本次全部模板共同成功或共同回滚。返回 `person / submitted_revision / duplicate / gallery_version`。

未登记目标默认 `UNREGISTERED`。合格抓拍与已有全库人员都不相似时，沿用关联核心生成稳定匿名 `auto:` ID，立即形成轨迹；不会等待实名资料才记录。人工登记不能直接替换匿名保留模板。实名合并仍需独立核验后的现有归属修正流程。

## 抓拍/帧的人脸入流

`POST /v1/monitoring/observe`：

```json
{
  "activity_id": "provided-activity-id",
  "activity_num": "provided-authoritative-activity-num",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "route_rule_id": "provided-current-camera-route",
  "observation": {
    "observation_id": "source:frame:face",
    "source_id": "capture-source",
    "camera_id": "camera-id",
    "frame_id": "original-frame-id",
    "feature_space": {},
    "embedding": [],
    "detection_score": 0.99,
    "face_size_px": 64
  },
  "time_record": {"profile_version": "verified-camera-profile", "raw_time": "2026-10-10T10:00:00.000+08:00"},
  "policy": {},
  "graph": {"version": "unmapped-v1", "edges": []}
}
```

同样，空契约/向量/policy是示意占位。policy 必须提供版本化匹配、新人、模板更新和候选差距阈值；实际阈值待真实同人/不同人数据核验，不能直接套测试值。脸大小必须是原始图像上的脸框最短边，放大或对齐后的112像素不能充当原始清晰度。P1低清晰度门槛之外，P2仍检查检测分数及脸大小。

`observation` 不带调用者认定的 target_id/person_id，由 C++ 对所有兼容图库人员比对。相同帧多张脸共享 frame_id，observation_id 分开；局部跟踪ID必须连同跟踪会话ID提供，不能当跨镜身份。已有厂商抓拍照片可直接经 P1 同一提取器比对，厂商不透明特征不能直接混入当前图库。

时间适配固定 `CAMERA_CAPTURE_60S`：取相机名义抓拍时间，偏差绝对值、测量/漂移和量化误差总界不得大于60000ms。policy和options的 `max_clock_uncertainty_ms` 默认60000，只能收紧。即使 observation 中附带伪造的UTC/零误差字段，也会由 ClockAdapter 结果覆盖；显式请求旧校正模式被拒绝。time_record如带camera/source必须与观察一致。

缺失/null time_record、缺 profile/raw_time 都返回200且 `status=HOLD / reason_codes=[CAPTURE_TIME_UNAVAILABLE]`，保存原输入但不调用关联、建人或补模板。已知但未核验、过期、边界歧义或超60秒的时间也HOLD。RTSP接收时间和媒体PTS不能自动充当相机抓拍时间。

时间合格后执行既有全库匹配。`CREATED/MATCHED` 会在同事务中自动更新受影响人员的完整轨迹，使用当前头版本作CAS，无需 P1 另发 rebuild。关联核心证明权威集合仅新增一条时，可使用下述严格连续追加路径；其余情况取完整当前归属集重建。身份待定/冲突/低质量仍返回HOLD及关联理由，不强行生成轨迹。

返回关键字段：

- `observation_id / status / time_admitted / normalization / reason_codes`；`time_admitted=true` 不等于身份通过。
- `association`：既有候选、身份决定和证据；时间HOLD时为null。
- `monitoring`：类别、当前状态、入口状态、是否继续监控及来源修订的紧凑摘要；身份未确认时为null。
- `trajectories[]`：`target_id / version / association_revision / observation_count / anchor_count / graph_version / revision`。完整经过序列用 `/v1/trajectories/get` 按该 target_id 和 version 查询。

回执 `receipt_scope=ORIGINAL_TRANSACTION`。相同 observation_id 和相同内容重试返回原交易回执；它不会冒充后来闸机出场、名单变更或归属修正后的实时状态。需要当前监控状态时调用 get，消费端依照修订号处理旧消息。紧凑引用避免在每次回执和出站队列中重复保存整段历史。

事件幂等键是 `(activity_id,source_id,observation_id)`。相同键全部内容相同只返回原回执；改变 time_record、policy、graph、版本、route 或 reconsider 等任何原字段都返回409，不能用补证标记静默覆盖。当前没有可改写原事件的公开补证修订入口；需要补证时由后端先约定独立有版本的合同，不能换ID冒充新的物理抓拍或延长原隔离期限。已经确认的身份改变走 `/v1/associations/correct`，显式携带活动三版本及原观察 source_id，见[一致性合同](consistency.md)。

## 闸机入场、出场和漏入口

闸机只验票不提供身份。`POST /v1/monitoring/gate-event` 接收：

```json
{
  "activity_id": "provided-activity-id",
  "activity_num": "provided-authoritative-activity-num",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "route_rule_id": "provided-current-gate-route",
  "event_id": "stable-passage-event-id",
  "source_id": "gate-backend",
  "gate_id": "gate-1",
  "direction": "EXIT",
  "ticket_reference": "opaque-ticket-reference",
  "time_record": {"camera_id": "clock-owner-id", "profile_version": "verified-profile", "raw_time": "2026-10-10T10:10:00.000+08:00"},
  "binding": {"source_id": "capture-source", "observation_id": "confirmed-face-observation", "evidence_id": "independent-same-passage-link"}
}
```

`binding`可省略；原事件已保存后再改变或补上 binding 属于同键不同内容，返回409。它表示调用方有独立依据证明该闸机通行与这一张脸是同一次通行，必须引用当前活动内的 `source_id+observation_id`，不能因为时间接近、票号相同就填。API拒绝直接填写person_id/target_id。已建立的绑定不可静默替换；引用脸的归属经独立修正后，监控投影自然跟随新的已确认归属。

返回状态 `HOLD`（时间或活动绑定不合格）、`UNBOUND`（未绑脸）、`BINDING_PENDING`（明确引用的脸尚未到达或未确认）、`BOUND`，超窗返回 `EXPIRED`。闸机先到且已给出明确引用时，稍后脸观察确认会自动纳入重算。原事件方向、来源、票据、时间和绑定均不可改；改变原键内容返回409。A活动的EXIT和停止不影响B，迟到旧EXIT不能关闭明确更晚的新会话。

闸机自己的合格采集时间仍在窗口内时保留其合法效果；所绑已确认脸到期，清理抹除原脸ID及绑定证明详情，保留核心确认的target和不可逆摘要，避免有效EXIT被错误丢弃。它不使过期脸点重新出现，也不接受调用者自报target。

单独票据事件永远不会关闭某个人的监控。已绑定且时间合格的ENTRY/EXIT才参与该人的时序投影。绑定的脸和闸机时间区间相交时，该脸可由对应闸机事件代表，避免把同一EXIT脸又当出场后的内场出现而立即重开。时间区间分离时保留脸和闸机两个事件，不能因后补绑定抹掉更早的观察；明确位于EXIT之后的脸仍可启动未知入口的新会话。两个不同的有效事件可引用同一脸时仍分别保留，不预设它们先后。

任何合格的人脸出现都可以启动监控，漏入口时 `entry_status=UNKNOWN`。确认EXIT且其时间区间明确位于所有可能活动证据之后，才成为 `CLOSED / continue_monitoring=false`。后续合格内场出现可重新启动，入口仍可未知。迟到旧EXIT不会停止后来已发生的新一轮活动；重复EXIT不会单独新增入场。

误差区间重叠或仅接触的活动/出场事件组成歧义组，返回 `AMBIGUOUS / possible_statuses=[ACTIVE,CLOSED] / continue_monitoring=true`。不按名义时间硬排先后，不因长时间没拍到就判离场。当前没有自动超时关闭。时间区间采用保守分组，可能多保留歧义，不能将其解释为精确物理会话划分。

## 查询、恢复和交接

`POST /v1/monitoring/get` 请求 `{ "activity_id":"...", "config_version":1, "binding_version":1, "monitoring_version":1, "target_id":"..." }`，要求该活动 `query` 授权。返回窗口内sessions和event_groups，以及 `category / person_revision / association_revision / gate_revision / status / entry_status / continue_monitoring`，外层带活动身份、三版本和签名用户 `client_context`。无合格观察时 `NOT_STARTED`；UNKNOWN入口与UNKNOWN身份是不同问题。清理后必要运行checkpoint可以保留继续监控状态，但不还原超窗点。全场容量仍需真实负载验收。

监控投影从已提交的活动内当前人脸归属及不可变闸机证据重放，支持重启、迟到和归属修正；`session_id`是作用域内不可逆投影标识，迟到证据可改变投影边界，不承诺为不可变物理入场流水号。轨迹版本和关联修订受168小时清理；存活历史版可以被修剪过期证据，不能视为永久存档。三类写操作可进入独立主动推送队列，接收端按活动/版本去重并在原期限后拒收；持续4xx不能使载荷超过期限继续保留。

## 完整历史的共享存储与连续追加

新轨迹版本使用内部refs-v2：每版保存根引用和元数据，长数组采用不可变AVL共享子树。evidence以作用域内observation_id为键，随机UUID或更小ID只改变树路径；visit内观察/归属/来源按序号保存。证据内容独立驻留，完整重建基于旧根差分更新。原始监控输入存一次对象，当前指针及回执/兼容旧历史revision引用；当前同键内容禁止补证改写，所有对象仍受168小时标记清扫。

保留期内不会因共享存储而减少观察或采样，多个版本有各自根；到期清理会重写存活版本、删除空旧版及不可达共享对象，完整查询只物化仍在窗口内的历史。旧 `refs-v1` 状态仍可读并受清理，下一次更新可产生 `refs-v2`，两种历史可共存。旧全局数据不猜活动，禁止把它直接归入任意当前活动。旧二进制不能读取新格式：升级前须停写并做SQLite一致性备份，回退必须恢复匹配程序与清理前备份到隔离路径，不能在新库直接降级；恢复服务也须取得新可信证明并先清理超窗历史。

连续追加依赖关联核心产生的内部纯新增证明，调用者不能上传该证明。图、options、旧关联修订与轨迹头必须匹配；观察必须合格、无重入证据、同摄像头、名义时间严格递增且在visit间隔内。多visit情况下，完整核心还须证明最后visit的最早可能开始严格晚于所有其它anchor的最晚结束；新增观察不能改变该visit的first_earliest或first_latest。满足条件才增量更新最后visit/anchor/session和入段候选末节点时间。路径评分、边窗口及先前会话不受影响；真实改变的入段仍出现在affected_segment_ids中。迟到、跨镜新visit、误差改变首界、归属修正、图/options改变或缺少证书均回退完整核心。

监控摘要只有在旧关联修订、闸机水位和事件排序均可证明连续时才增量更新；待绑定的已合格闸机先到、新出现EXIT歧义、归属改变等触发完整投影。SQLite事务同时覆盖共享节点、索引、原输入、监控摘要、轨迹根及push；失败回滚会重建热状态。

本地合成负载证据见[性能报告](../evidence/2026-10-10-monitoring/p2/PERFORMANCE.md)。用户规模约600路，目标端到端P95约2秒、3秒可接受；单人连续流或400观察的跨镜实验不能证明该全链容量。多camera身份约束、跨镜/迟到完整回退及完整查询仍随相关历史增长，图库规模、每路有效人脸事件率、硬件与真实输入方式仍须纳入吞吐验收。

当前成对产物：`cpp/build-activity/Release`。活动、签名授权、订阅与168小时清理的启动、最小后端配合、生产启用前dry_run、配置和迁移回退见[多活动服务合同](activity-service.md)，实测结果见[唯一交接](../HANDOFF.md)。原监控算法验证保留在[此前P2报告](../evidence/2026-10-10-monitoring/p2/REVIEW.md)。本地合同验证不等于现场身份精度、设备时钟、真实登录切换、生产鉴权、真实闸机或3D通路验收，600路/3090/2–3秒仍是容量设计目标。
