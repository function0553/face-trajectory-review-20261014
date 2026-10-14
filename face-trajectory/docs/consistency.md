# 重建版本与归属一致性

更新：2026-10-10；OpenAPI 0.13.0。活动内版本/归属保护继续适用；所有活动请求带activity_id及当前三版本与签名动作授权，见[多活动服务合同](activity-service.md)。旧point/raw完整记录重建公开入流已拒绝，正常monitoring准入后自动重建；association_rebuild从核心权威集合重建仍可使用。下面原核心替换算法语义不构成绕过准入的接口。

## 防止迟到的完整记录集覆盖新结果

`point_trajectory_rebuild`、`trajectory_rebuild`、`association_rebuild` 增加 `expected_version`，表示调用方准备该完整记录集时的轨迹版本，和 association.version 无关。

- 首次创建传 0。首次创建省略仍兼容；建议明确传 0。
- 修改已有轨迹时传当前 trajectory.version；省略或版本不一致返回 HTTP 409 / VERSION_CONFLICT，结果与 outbox 不改变。
- 输入、图、选项和归属修订与当前版本完全相同时，旧 expected_version 可作为回执丢失后的幂等重试；不产生新轨迹版本，但启用推送时仍可能产生新消息 ID。未来版本、负数或非整数不能用作重试条件。
- 合法撤回可以缩减或清空记录集，前提是基于最新版本明确提交。不要按记录条数判断更新是否过时。

例如已提交版本 1 的 `[A]`，以 expected_version=1 提交 `[A,C]` 生成版本 2。迟到的 expected_version=0/1、输入 `[A]` 会被拒绝。基于版本 2 核实撤回 C 后提交 `[A]` 则合法。

409 的 `error_code` 区分 VERSION_CONFLICT、SOURCE_CONFLICT、REBUILD_REQUIRED；`conflict` 提供 target_id、latest_version、current_association_revision。发现冲突后读取/核对最新权威资料，再准备新完整记录集；**不能只把 expected_version 换成服务器返回的新版本，然后重发旧记录集**。CAS 防止基于旧版本的替换，不验证外部资料本身是否完整或正确。

## 归属改变后的当前与历史结果

每个关联目标持久保存 `association_target_revision`；只有影响该目标的确认观测或归属修正推进其修订。无关人员不因此失效；同内容 observation/correction 幂等重试不推进修订。轨迹快照保存构建时的 association_revision。

observe/correct 的 `association.trajectory_effects` 列出每个受影响目标、当前关联修订、最新轨迹版本和 rebuild_required。修正移走观测时，旧主人与新主人都列入。已有轨迹不再以过期内容充当当前轨迹。

省略 version 或传 0 的 get、point get、timeline、predict、geometry 查询，当快照归属修订过期或关联目标尚无轨迹时返回 409 / REBUILD_REQUIRED。调用方按受影响目标，以返回的 latest_version 为版本依据调用 association_rebuild，再按需查询或预测；旧主人也要重建，允许形成空轨迹。服务不定时全场重算，也不在查询中隐式写入。

指定正整数version只可读取仍存活历史；168小时到期会重写存活版本并删除空旧版，不保证永久还原原历史。返回顶层及轨迹/时间轴/点位/预测/几何结果内的 freshness：

| 字段 | 含义 |
| --- | --- |
| snapshot_association_revision / current_association_revision | 快照绑定与当前目标归属修订 |
| latest_version | 当前轨迹头版本 |
| association_matches_current | 归属修订是否相同 |
| requested_scope | CURRENT 或显式 HISTORICAL |
| valid_for_current_use | 既是最新轨迹头，又符合当前归属修订；不表示现场精度或拓扑已验收 |
| status | CURRENT、HISTORICAL、STALE_ASSOCIATION |

指定历史版本不恢复当时服务器实际到达历史或已清理超窗点，不能以版本审计为由延期保留。归属过期的历史推断仅用于审计。版本比较要求新版本符合当前归属，旧依据变更时仍保留既有 UNRESOLVED 语义。

0.13所有外部point/raw/association_observe绕过入流均SOURCE_CONFLICT/409。活动内association_rebuild使用当前核心Sightings，客户端不能上传移走前快照或自报UTC绑回新归属修订；不能在同一目标混用两套归属权威。

## 恢复、推送与升级

归属修订、轨迹和 outbox 在同一 SQLite 事务中处理。入队失败回滚归属与修订，并重建热状态；重启后失效保护仍有效。回滚测试只在隔离 SQLite 中注入触发器。

推送的 freshness 是提交时状态。后端消费 observe/correct 时，按 trajectory_effects 的 current_association_revision 对受影响业务轨迹标记待重建；只接受与已知当前归属修订相符的轨迹。按目标维护归属修订和轨迹版本，两者分别单调更新，避免积压旧轨迹覆盖修正后的状态。仍须按activity_id/event_id去重并核验原retention_deadline；同人判定的 association.version 不能替代每目标 current_association_revision。接收接口目前未提供，这一合同尚未在真实 Java 端验收。

旧数据库轨迹没有 association_revision 时按 0 读取；旧关联目标没有独立修订时使用稳定迁移基线 1。旧历史可读，但关联目标当前视图需要以完整当前关联记录明确重建一次。无本地关联的外部轨迹仍绑定 0，不因该升级全部失效。不得手工给旧快照补当前修订冒充已重建。

版本保护防止后续旧请求替换，不能自动找回升级前已被错误替换的权威记录；已有目标仍须核对接入端完整资料，必要时基于最新轨迹版本重放。旧缺活动/期限的outbox不准发到任何当前活动；按清理合同到期删除而非无限审计保留，接收端须拒绝无作用域或超期消息。旧全局数据不猜活动，原capture期限有效、缺可靠原期限立即到期；按明确权威映射重新同步登记配置。回退恢复匹配程序与备份到隔离路径，先取得新可信证明并清理。未升级任何生产状态库。

C++ worker 默认绑定 `TrajectoryStore::SetAssociationRevisionReader`，包括失败恢复后。直接嵌入核心库时，宿主也须绑定目标修订读取器，并在同一写入序列/事务内提供完整关联 Sightings；未绑定读取器的纯外部 TrajectoryStore 只提供版本前置条件。SQLite 状态只允许一个权威写入 worker，多实例共享写入不在本次合同内。

## 验证与交接

当前成对产物cpp/build-activity/Release；当前活动/留存命令与证据见唯一交接。原一致性失败及修复见[此前报告](../evidence/2026-10-10-consistency-fix/REVIEW.md)。生产Java、数据库、相机和模型未改动，真实同人、时钟、拓扑、鉴权及600路容量仍须现场验收。
