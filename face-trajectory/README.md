# 摄像头点位时空算法

2026-10-22 当前接入：[人员轨迹三方协议v1.0](docs/person-track-platform-v1.md)。平台手动导入固定关注名单，活动模式设为`PERSON_TRACK_EVENTS`，算法主动推送单脸抓拍UPSERT/RETRACT，持久重试并校验平台业务确认。原有时间线查询保留用于联调核对。完整首次登记样例、模板提取及启动配置见该文档；生产服务未自动切换。

2026-10-21新增[UE摄像头出现事件模式](docs/ue-camera-events.md)：按大麦上传资料登记人员后，上报摄像头名称、抓拍时间、人员ID及重点标记。该模式不建立空间路线，不要求坐标/场景/NavMesh；通过活动配置显式启用，生产服务未自动切换。

**当前交接入口：[HANDOFF.md](HANDOFF.md)。** 项目负责已有同人抓拍的时间校正、摄像头经过序列、候选连接、后续可达范围和新观测修正。C++17 运行算法，Go 提供薄 HTTP 接口；业务资料接入和最终三维显示由 Java/前端负责。

2026-10-13已完成普通摄像头点到点输出：`point_trace.connections`给出可直接绘制的两个端点，同镜重复不画自连线；时间歧义保留虚线语义，位置冲突/缺失及跨会话保留断点。当前新本地产物在`cpp/build-point-connections/Release`，旧运行服务未自动切换。[接口和本地查看](docs/point-connections.md)、[交接第32节](HANDOFF.md#point-connections-20261013)。鹰眼单镜连续轨迹后置。

## 当前状态

- 时间校正、点位图登记、前向预测、版本比较及三类监控已在本地实现；接口为[OpenAPI 0.14.0](openapi.yaml)。多活动隔离、签名授权及168小时历史清理沿用[多活动服务合同](docs/activity-service.md)，新模式未交付事件独立持久保留至平台业务确认。正常入流统一monitoring准入；生产接收与鉴权待联调。新三方对接见[人员轨迹推送](docs/person-track-platform-v1.md)，旧通用格式见[直接推送接入](docs/backend-push.md)。
- 复用只读快照中的 62 台已绑定设备和 878 条模型候选边。图未核定且不完整，南门两个冲突位置保留；候选之外仍可能存在未覆盖位置。
- 真实 30 条抓拍缺少同人关联和可靠时间合同，仍保持 HOLD。模拟验证不代表真实人员轨迹、实际通路或全场容量通过验收。
- 生产服务器仅允许读取；开发、测试和查看在本机。新增 P1 自拉流/海康抓拍提特征接入 P2 三类人员实际比对与自动轨迹，见[监控接口](docs/monitoring.md)与[P1运行合同](../face-recognition-engine/docs/monitoring-runtime.md)。座位号和鹰眼设备包后置。
- P2保持C++17+Go；上述P1运行原型使用Python，正式C++/Go全链路迁移尚未实施。业务机已于2026-10-10只读核查，服务器资源及摄像头端口结果见交接第19节。

## 使用入口

| 需要做什么 | 阅读位置 |
| --- | --- |
| 接手、构建、运行、下一步 | [主交接](HANDOFF.md)、[构建与运行](HANDOFF.md#build) |
| 多活动、授权、新活动接入、168小时清理与迁移 | [0.13服务合同](docs/activity-service.md)、[统一设计](docs/activity-retention.md) |
| 接入经过记录及修正 | [点位轨迹接口](docs/point-trajectories.md) |
| 校对时间 / 推断候选范围 | [时间适配](docs/clock-adapter.md)、[前向可达](docs/point-reachability.md) |
| 场景、点位和图的来源 | [发布场景](docs/published-site-context.md)、[候选图导入](docs/point-graph-data.md) |
| 已有特征自动关联 / 状态与重试 | [关联模块](docs/association.md)、[运行说明](docs/runtime-and-geometry.md) |
| 检查验证证据 | [模型更新及当前复跑](evidence/2026-10-08-continuation/REVIEW.md)、[点位实现基线](evidence/2026-10-07-point-trajectory/REVIEW.md)、[完整文档索引](docs/README.md) |

当前本地点位查看页：[http://127.0.0.1:8793/](http://127.0.0.1:8793/)。从空白开始读取保存的查询结果，再显示点位连接；合成示例需明确点击。启动命令见[点位连接交付](docs/point-connections.md)。旧8769页保留为历史工具，不作为当前0.13入流方式。

当前监控入流自动从核心完整权威集合重建；不能用旧raw/rebuild接口传最新一条绕过时间准入。`timeline/query`用于窗口内历史查询，末次观测之后使用`point-trajectories/predict`，均携带活动三版本及query授权。相机点位连线不代表已观测到两点之间的详细脚步路径。

0.10.0修复的版本/归属一致性继续有效；历史查询也受168小时修剪。当前0.13.0成对产物在cpp/build-activity/Release，使用后端签名授权及外部可信UTC挑战证明；相机准入保留名义时间且总误差不超过60秒。默认清理只统计，明确retention-enabled才允许删除；生产启用另行授权。见[多活动服务合同](docs/activity-service.md)、[一致性合同](docs/consistency.md)和[时间适配](docs/clock-adapter.md)。

历史预设路线循环验证结论已撤回，见[撤回判定](evidence/2026-10-07-model-route/VALIDATION-ASSESSMENT.json)。当前独立逻辑测试不恢复该结论。

JSON 依赖固定为 [nlohmann/json v3.12.0](https://github.com/nlohmann/json/tree/v3.12.0)，许可见 [LICENSE.MIT](cpp/vendor/LICENSE.MIT)。
