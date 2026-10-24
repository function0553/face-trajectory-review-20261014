# P2 文档目录

2026-10-23部署状态：[252 v4.1算法联调部署](../evidence/2026-10-23-server252-v41-deploy/README.md)。算法三项服务已运行并自启，后端未改；真实视频和后端推送仍待接入。

2026-10-23当前改造入口：[人员轨迹平台v4.1算法接入](person-track-platform-v4.1.md)、[v4.1 OpenAPI](../openapi-platform-v4.1.yaml)。本轮基于用户修正后的v4.1实施，v4.0不作为实施合同，下方v1资料保留为旧接口参考。

[当前交接](../HANDOFF.md) → [人员轨迹三方协议v1.0](person-track-platform-v1.md) → [监控与人脸接入](monitoring.md) → [OpenAPI 0.14.0](../openapi.yaml)。当前状态以主交接为准；带日期的报告保留当次证据及限制。

[v4.1动态采集路由与252实机缺口](platform-capture-routing-v41.md)：本地15项通过，后端未改，尚未实机接通。

## 当前开发与接入

| 用途 | 文档 |
| --- | --- |
| 当前三方对接：固定名单、单脸UPSERT/RETRACT、可靠重试、完整首次登记样例 | [人员轨迹平台推送v1.0](person-track-platform-v1.md) |
| 联调核对与既有后端按次查询：人员资料、抓拍照片和时间线 | [后端人员抓拍查询](backend-person-query.md) |
| 当前D-01～D-07修复、3D拓扑折线、验证与接手 | [2026-10-14修复与拓扑交接](repair-topology-2026-10-14.md) |
| 活动CAS/权限/订阅/新活动/清理/迁移与回退 | [0.13服务合同](activity-service.md)、[实施前统一设计及核对](activity-retention.md) |
| 三类人员、人脸比对、闸机监控与自动轨迹 | [监控入流合同](monitoring.md) |
| 经过记录、版本和新观测修正 | [点位轨迹接入](point-trajectories.md) |
| 普通摄像头直接点位连线、结果字段与只读本地查看 | [2026-10-13点位连接交付](point-connections.md)、[交接第32节](../HANDOFF.md#point-connections-20261013) |
| 迟到重建、归属修正、409 与历史有效性 | [一致性合同](consistency.md) |
| 时区、偏差、误差和校准有效期 | [时间适配](clock-adapter.md) |
| 末次观测后的候选点位与通道 | [前向可达范围](point-reachability.md) |
| 已发布摄像头与场地标记 | [场景上下文](published-site-context.md) |
| 模型连接导入、冲突和未完成查询 | [点位候选图](point-graph-data.md) |
| 主动推送 JSON、接收协议、离线重试与按需查询 | [后端结果推送](backend-push.md) |
| 场地三维模型变化、来源校验及重放 | [模型更新](model-updates.md) |
| 已有兼容特征的跨摄像头关联 | [关联算法与调用](association.md) |
| 公开成熟人脸/跟踪/多路视频项目、许可及复用边界 | [2026-10-12开源项目对照](open-source-security-comparison-2026-10-12.md) |
| 保持现有模型的源码复用、质量修复及离线错关联评估 | [2026-10-12实施与复现记录](open-source-reuse-audit-2026-10-12.md)、[当前交接第30节](../HANDOFF.md#no-downgrade-reuse-20261012) |
| GTR/ByteTrack论文、YOLO人体检测与ArcFace身份组合、优化位置及实验边界 | [2026-10-13优化核查](gtr-bytetrack-optimization-2026-10-13.md)、[当前交接第31节](../HANDOFF.md#gtr-bytetrack-20261013) |
| SQLite、恢复、重试、几何及既有入流 | [运行说明](runtime-and-geometry.md) |
| 现场需要读取或核实的资料 | [采集清单和模板](../collection/README.md)，座位/鹰眼后置 |

## 当前验证与来源证据

| 范围 | 入口 |
| --- | --- |
| 2026-10-12 SCRFD合同/裁边质量、GPU对照与离线评估工具 | [实施记录](open-source-reuse-audit-2026-10-12.md)、[本次SHA清单](../../face-recognition-engine/evidence/2026-10-12-no-downgrade-reuse/final-manifest.json) |
| 2026-10-10 摄像头名义时间及60秒准入 | [报告](../evidence/2026-10-10-clock-policy/REVIEW.md)、[时间合同](clock-adapter.md) |
| 2026-10-10 版本与归属一致性修复 | [报告](../evidence/2026-10-10-consistency-fix/REVIEW.md)、[supervision评估](../evidence/2026-10-10-consistency-fix/SUPERVISION.md) |
| 2026-10-08 模型版本保护及新核心复跑 | [报告](../evidence/2026-10-08-continuation/REVIEW.md)、[SHA 清单](../evidence/2026-10-08-continuation/manifest.json) |
| 2026-10-07 点位级实现和本地验证 | [报告](../evidence/2026-10-07-point-trajectory/REVIEW.md)、[SHA 清单](../evidence/2026-10-07-point-trajectory/manifest.json) |
| 2026-10-07 后台只读复核和通用模型查询 | [复核报告](../evidence/2026-10-07-reassessment/REVIEW.md)；其中“尚未实现”的状态已由后续点位实现更新 |
| 2026-10-07 发布场景绑定来源 | [场景报告](../evidence/2026-10-07-existing-data/REVIEW.md)，旧动线演示不作通行证明 |
| 2026-10-07 初始服务器数据调查 | [只读报告](../evidence/2026-10-07-server-readonly/REVIEW.md)，旧读时差不作历史校准 |
| 实际局部模型及撤回记录 | [模型报告](../evidence/2026-10-07-model-route/REVIEW.md)、[循环验证撤回判定](../evidence/2026-10-07-model-route/VALIDATION-ASSESSMENT.json) |
| 本次文档筛选及整理前副本 | [整理清单](../evidence/2026-10-08-handoff-cleanup/cleanup-report.json) |

## 按需追溯的历史

| 范围 | 入口和时效 |
| --- | --- |
| 2026-10-05 整链约束、持久化与性能修复 | [修复报告](../evidence/2026-10-05-repair/REVIEW.md)；[修复前复核](../evidence/2026-10-05-feasibility/REVIEW.md)的问题需对照修复报告，不照搬为当前缺陷 |
| 2026-10-04 关联合成验证 | [记录](../evidence/2026-10-04-association/validation.json) |
| 2026-09-30 原核心功能验证 | [可用性](../evidence/2026-09-30-usability/validation.json)、[时间轴位置](../evidence/2026-09-30-timeline-locations/validation.json)、[时空约束](../evidence/2026-09-30-time-space/validation.json)、[原审查](../evidence/2026-09-30-review/REVIEW.md) |

历史 CTest/Go 测试源码及旧清理内容在[工作区清理归档](../../workspace-docs/archives/project-cleanup-20261007.zip)。本次新增可复跑的`point_connections` CTest目标，旧归档目标未恢复；历史通过数量不能作为当前复跑结果或现场验收。原始证据保留原日期，不覆盖为成功记录。
