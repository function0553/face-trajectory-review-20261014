# 运行状态、入流与三维路线

2026-10-10 传输补充：Go 支持配置接收 URL 后主动推送 JSON 算法结果，消息与算法状态同事务落盘；查询和预测仍按需执行。启用参数、鉴权、补发、状态与接收端去重见[结果推送](backend-push.md)。0.13多活动、签名授权、可信UTC和168小时清理见[服务合同](activity-service.md)；下文几何推断语义继续适用，原raw入流不再公开接受。

本次代码修复不依赖现场资料。算法仍需要实际标定才能判定现场路线和识别准确率，约600路相机的验收还需要实际抓拍频率和服务器规格。

算法方交付范围：C++ 时间/空间约束、关联与轨迹数据、P1 特征适配、版本恢复及离线验证。SQLite 是算法内部状态恢复机制。现有 Go 薄包装和 HTTP 检查仅用于调用契约和本地联调；本次不承担 Java 业务后端、业务数据库、人员资料系统、前端页面或三维绘制实现。对接方按输出契约集成，算法方不需要开发后端/前端。

当前复跑使用唯一交接中的verify_core/verify_service/verify_retention与边界脚本。C++内部JSON行协议只供本机受控Go管道和隔离测试；旧2026-10-05 raw脚本是历史证据，不是0.13公开入流方法。直接嵌入核心时宿主须提供相同活动视图、权威UTC、准入、事务和清理边界，不能只调用旧底层函数就宣称满足当前合同。

## 启动、作用域与状态

当前成对worker/daemon位于cpp/build-activity/Release，使用[服务合同](activity-service.md)的完整公钥、issuer、挑战证明与新隔离state-file启动命令。默认127.0.0.1。除了health，HTTP须后端签名Bearer；前端activity_id不构成权限。Go保留按活动轮转及单权威C++写入进程。SQLite固定随项目提供可离线编译；持久monitoring/推送/活动功能需要非空state-file，临时模式不提供这些合同。

State将活动命名空间编码为JSON[activity,id,ns]，独立图库、分类、关联、会话、轨迹与热缓存使用同一根SQLite事务；业务person_id不拼接活动伪造隔离。创建活动停止，明确CAS配置/绑定/启停独立于用户登录、选择及订阅。配置/绑定变更、停止后迟到输入HOLD，同原键全内容相同重试只回原交易；不同内容409。

数据库持有独占进程锁，单库禁止两个权威worker。观察、原始入流、自动模板、监控、轨迹及push同事务提交，SQLite WAL/FULL同步成功后回应，失败回滚并重建热索引。重启按活动恢复；用户内存订阅丢失需重新open，但后台enabled不变。队列满503、请求超时504，按同activity/source/event及原内容重试；已排队的授权在C++调度前再次核验期限。

图库总上限500000张、SQLite默认8GiB是本地保护值，另需WAL/备份空间，并非600路容量结论。登记模板排除清理；抓拍衍生模板、观察/历史版、所有refs-v1/v2共享对象、索引、缓存、结果流、推送及恢复来源按168小时清理。cutoff=可信now−604800000ms，capture等于cutoff保留，早于到期。新P2未知时间取首次可信隔离期限；缺原期限的旧/未归属/P1未知载荷立即到期，不能给迁移新窗口。原raw/association/point重建绕过接口拒绝，正常入流只能monitoring准入。

清理默认只统计；明确retention-enabled才删除，P1所有实际使用库由重复p1-retention-db启动参数列出。真正删除提交后清空活动热缓存，再secure_delete/VACUUM/截断WAL。P1维护握手暂停消费者、排空推理/发送、释放reader/缓存和SQLite连接，取得租约后清理，成功后重开恢复；不合作或物理维护失败保持INCOMPLETE/PENDING。dry_run使用内存PlanningCopy执行真实算法规划并回滚，原库/水位/墓碑不变，报告精确原行删除和共享对象GC计数。

历史版仅在窗口内可读，到期会修剪证据/派生路线、删除空旧版并标记清扫共享根；仍被有效窗口内数据引用的对象保留。最小运行checkpoint保持继续监控状态和跨cutoff已有歧义的不可逆依赖，不保留过期相机/时间/路线。不可逆幂等墓碑防止重传复活，但随不同事件累计，总存储不承诺恒定。实际量、可复用页、主库/WAL字节及多轮增长分别报告，见唯一交接证据。

本版继续精确余弦、连续内存、SSE2及OpenMP，不用近似TopK隐藏竞争身份，也不复制独立匿名建人的worker。正式3090/600路/2秒目标3秒可接受仍待容量验收。

## 时间和往返

身份约束求解整条抓拍链，所有观测共享同一组可选实际时刻，包含最短通行时间、等待、开放窗口和显式重叠区域。重叠时间簇枚举可行次序；预算默认 200,000 次、混合相机簇最多 12 条。无法完成证明返回 UNKNOWN；要求拓扑或声明完整拓扑时保持 PROVISIONAL。`max_travel_ms` 仍是软参考，不拿它排除慢行或等待。

`OBSERVED` 现在要求查询时刻等于直接抓拍的名义时间；返回的时钟误差仍有效，不表示零误差实际位置。`possible_observed_anchor_ids` 也只来自单条抓拍的误差区间，不把两次抓拍之间的整个 Visit 作为可能观测。Visit 内其他时刻不能据此声明已观测。该语义与 0.5 不同，调用方应更新显示逻辑。

`options.non_overlapping_cameras` 是经过独立核验的**观察区域不相交**相机对列表。A 两次抓拍的误差范围之间有 B 的确认抓拍、且 A/B 已知不相交时，自动拆开两次 A，并记录 B 的 `boundary_observation_id`；不补写伪造的 `reentry_evidence_id`。未知区域关系或时间误差使顺序不确定时仍保守合并，可以由独立离开/重入证据拆分。不能仅因 camera_id 不同就声明不相交。

## 三维路线

先 `POST /v1/geometry` 注册 `geometry`：

```json
{
  "geometry": {
    "geometry_version": "synthetic-demo-only",
    "graph_version": "synthetic-graph",
    "coordinate_frame": "synthetic-world",
    "unit": "m",
    "join_tolerance": 0,
    "edges": [{"edge_id":"AB","from_camera":"A","to_camera":"B",
      "points":[[0,0,0],[2,0,0],[2,3,3]]}]
  }
}
```

这只是格式示例，坐标不是场馆资料。真实边应绑定导航通道，摄像头代表已标定观察区域，不能直接把相机安装点当人员落地点。几何版本不可改内容，坐标单位和接缝容差必须明确。

再 `POST /v1/trajectories/geometry` 提交 `target_id`、可选 `version` 和 `geometry_version`。不使用 HTTP 时，对应 worker 操作为 `geometry_put` 和 `trajectory_geometry`，使用相同载荷并添加 `op`。输出每条主备路线的有序 `polyline`、通道折线及对应 `earliest_unix_ms/latest_unix_ms` 时间包络。`route_index=0` 为主路线。缺少绑定或接缝不连续时整条合成线为空，并报告 `MISSING_GEOMETRY` / `DISCONTINUOUS_GEOMETRY`，不画跨墙直线。图版本、边方向或端点不匹配会拒绝请求。

空间线是条件于某条候选路线的推断，可能性、完整搜索标记和主备路线都应保留。输出不包含匀速插值的“精确实时 xyz”或编造的转弯时刻；时间轴仍使用路段/节点的可行范围。

## P1 入流适配

P1 新增 `face_engine.trajectory_bridge`，复用现有 SCRFD、五点对齐和 ArcFace。它计算实际模型 SHA256 和运行检测配置，输出 512D 特征契约与来源元数据，使用 SQLite outbox 先落盘再 HTTP 投递，失败后可离线保留并重试。

在 P1 目录执行：

```powershell
.venv/Scripts/python.exe -m face_engine.trajectory_bridge --config configs/demo.yaml --events events.jsonl --policy policy.json --graph graph.json --outbox state/trajectory-outbox.sqlite --enqueue-only
.venv/Scripts/python.exe -m face_engine.trajectory_bridge --outbox state/trajectory-outbox.sqlite --flush-only --endpoint http://127.0.0.1:8081
```

demo 配置只用于本地工程验证。实际入流的 JSONL 每行包含 `image_path`、稳定的 `source_id/camera_id/frame_id`、以及采集端的 `captured_at_unix_ms/time_source/clock_domain/clock_uncertainty_ms`。帧内局部 ID/重入证据放在 `faces` 中，以排序后脸框索引为键；它们不是每帧所有脸共享的标记。帧图像、时间元数据和契约不可在同一 frame_id 下改写。

未知时间源默认 UNKNOWN，误差默认 -1，不取当前处理时间充当采集时刻。UTC 字段不完整会降低为 UNKNOWN；P2 返回 UNASSIGNED，不会产生确认时间轨迹。此类旧帧后续需要独立核实原采集元数据并以明确的数据修订流程处理，不能修改同一 immutable observation_id 重试。

outbox 默认最多 10,000 条待投递记录或 128 MiB 待投递正文，数据库数据页上限 512 MiB（`--max-outbox-mb`），超限拒绝整帧入队，采集侧应暂停或先投递。待投递记录按入队序号使用独立索引，避免扫描所有已确认回执。已确认正文清空，保留去重摘要和回执；回执/帧去重历史也需要部署保留策略。该适配器接收已解码图片/帧事件，不替代摄像机厂商 SDK 的硬件时间戳采集或 500 路视频解码系统。
