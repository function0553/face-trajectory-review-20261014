# 算法结果推送与按需查询（0.13.0）

C++17在同一SQLite事务中保存算法结果和可选持久出站消息；Go向运维配置的HTTP(S)接收URL POST JSON。后端收到算法结果JSON，不是JS或数据库文件。活动、鉴权、可信UTC及启动见[多活动合同](activity-service.md)，字段以[OpenAPI](../openapi.yaml)为准；本轮仅隔离本地验证，生产接收端与真实鉴权待联调。

## 消息与作用域

正常入流使用 `/v1/monitoring/observe` 或 `/v1/monitoring/gate-event`，每条输入都经活动设备route、三版本和ClockAdapter准入。登记类别、观察和闸机结果、明确归属修正和基于核心权威集合的关联重建可入队。原始association_observe/trajectory_rebuild/raw_trajectory/build_visits/point_trajectory_rebuild绕过路径已拒绝，不再作为业务接入方法。场景、图、时钟、查询、预测及比较不产生结果推送。

```json
{
  "schema_version": "1",
  "event_id": "persisted-stream-id:1",
  "sequence": 1,
  "operation": "monitoring_observe",
  "activity_id": "authoritative-activity-id",
  "activity_num": "authoritative-activity-num",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "created_at_unix_ms": 1791626400000,
  "retention_deadline_unix_ms": 1792231200000,
  "result": {
    "ok": true,
    "activity_id": "authoritative-activity-id",
    "activity_num": "authoritative-activity-num",
    "venue_id": "authoritative-venue-id",
    "config_version": 1,
    "binding_version": 1,
    "monitoring_version": 1,
    "status": "ACCEPTED"
  }
}
```

这是包络示意，真实result按具体操作schema返回。活动身份和三版本必须核验，不能只按target_id或sequence更新。HOLD、身份待决、误差重叠、图未核定及条件候选不得因传输变成确定结论。created_at是可信UTC入队时间；轨迹期限取原合格相机名义采集时间，新未知时间隔离取首次可信入库，重投、活动停止、用户退出均不重计。消息不附输入图片、脸向量或授权私钥。

用户订阅是独立的访问通道，通知含client_context、subscription_id和subscription_generation，见多活动合同。持久后台推送与订阅游标/关闭独立；用户切换不会ack或停止后台队列。后端转发给页面时须附当前用户选择上下文，前端逐项匹配活动、版本、选择与订阅上下文，丢弃旧消息。

## 接收、重试与到期

`Idempotency-Key`为SHA-256(JSON `[activity_id,event_id]`)。接收端须先在业务事务中持久接收，按同一逻辑键去重，成功后返回2xx；回执丢失可导致再次投递。监控入流的相同activity/source/event及相同内容重试不会产生新的业务副作用；同键异内容409。按target_id/算法版本更新仍应拒绝旧版本，归属修正须处理affected_target_ids及freshness，见[一致性合同](consistency.md)。

Go按活动轮转取各活动最老消息，同一活动保持提交顺序。失败活动独立退避1/2/4/8/16/30秒，再保持30秒；退避期间跳过该活动，其他活动可继续发送。全局worker/时钟不可用使用独立全局退避，未完成600路全场压测。HTTPS保留系统证书验证，3xx不跟随，4xx/5xx保留原消息直到它的期限。单次发送上限10秒，同时受剩余期限与可信UTC证明租约限制。取件/重启重放会删除到期载荷；超期或缺可信UTC不发包。接收端必须独立拒绝 `now > retention_deadline_unix_ms`，发送端无法召回已被对方接受的在途请求。相等期限仍保留。

168小时清理覆盖push载荷、依赖旧历史的回执和重放来源，不因持续4xx或离线而无限保留。存活回执/通知可压缩成detail_query_required，请再用授权的当前活动查询获取详情；过期历史不能由消息恢复。SQLite出站队列与算法结果原子提交，满队列或磁盘限制触发整笔回滚。

## 启动、观察与恢复

使用本轮成对 `cpp/build-activity/Release` worker/daemon及多活动合同中的公钥、issuer、挑战证明和新隔离state-file参数。`-push-results`只启用持久排队；`-push-url '<现场实际HTTP或HTTPS接收URL>'`启用排队及发送。目的地只能启动配置，HTTP输入不能更改。非空持久state-file是推送前提。Linux目标须重新构建，现有Windows exe不适用；单库只运行一个权威写入worker。

可选环境变量FACE_TRAJECTORY_PUSH_AUTHORIZATION按原值成为发送HTTP Authorization，由接收端签发；不要写入代码、命令日志或交接。它与用户请求的签名Bearer、UTC权威公钥三者独立。没有URL也没有push-results时不入队；无URL的合法排队数据只保留至原期限，旧数据库不会自动补生成全部历史消息。

有全局push_status授权后调用 `GET /v1/push/status`，观察enabled/durable/pending/max_pending/max_pending_per_activity/receiver_configured。receiver_configured只说明配置URL，不证明接收端可用。全局队列上限10000，每活动最多256，防止单活动占满出站空间，受SQLite配置页数限制；配额只在真实非重复入队时检查，同原请求重试不占新名额。503/504须按原键原内容重试。持续4xx、队列最老年龄、空间和可信UTC失效需运维告警。内部push_next/push_ack不公开HTTP，不手改数据库。

轨迹get、timeline query、geometry、点位get/predict/compare都需activity_id及当前三版本和query授权；历史version也受滚动修剪。必要身份资料、权威ID映射和前端显示仍由后端提供。升级停写并备份SQLite/WAL，在隔离副本验证；旧备份恢复必须先取得新证明和清理超窗历史，接收端始终按活动版本与期限去重。默认retention只统计，启用删除属于另行授权部署，见多活动合同。
