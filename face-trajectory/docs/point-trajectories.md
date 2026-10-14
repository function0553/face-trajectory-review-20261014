# 相机点位级轨迹接入

接口复用C++历史轨迹核心与SQLite，Go提供签名授权、活动作用域和[结果推送](backend-push.md)，数据为JSON。0.13正常抓拍经monitoring统一时间与身份准入，原point/raw重建公开入流已拒绝，详见[多活动服务合同](activity-service.md)。查询均带activity_id/当前三版本及query授权；全局场景/图/clock配置需global_config授权。模型安装点是相机节点，顺序连接不表示人物逐米路径。

## 接入顺序

2026-10-13普通摄像头展示合同已补齐：`point_trace.connections`直接给出相邻不同摄像头的两个点位端点；不需要绘制推断的完整通路。新字段、同镜去重、时间/位置歧义和本地查看见[点位连接交付](point-connections.md)。鹰眼单镜画面轨迹后置。

1. `/v1/site-scenes`登记已有发布场景。
2. `/v1/point-graphs`登记`point_graph`包：`graph_version`、`scene_version`、`source_evidence_id`、`coordinate_frame`、明确的`topology_verified/topology_is_complete`、`edges`。相同版本不得改内容。主边要求可解析且无冲突的绑定位置；冲突设备仍可形成经过记录并保留所有位置。真实包由`collection/build_point_graph.py`从保存的只读快照生成，详见[数据导入](point-graph-data.md)。未核定边用于条件候选，不可据此否定真实观测。
3. `/v1/clocks/profiles`登记相机校准版本。现场读时诊断以`verified:false`保存；可靠的校准需明确有效时段、偏差、误差和时间来源。详见[时间校正契约](clock-adapter.md)。
4. 先按CAS同步活动身份、完整设备绑定范围并明确启动监控。P1通过`/v1/monitoring/observe`逐条提交当前活动/device route、source/observation、提取器特征契约及time_record；不能上传猜测target_id。合格关联自动从核心完整权威集合重建，graph版本须与已登记场景/点位图合同一致，不能私改同版本内容。
5. 监控回应ACCEPTED/HOLD、normalization、association及trajectory引用；按引用target_id/version调用`/v1/point-trajectories/get`取得相机顺序、位置候选及顺序歧义。trajectory仍给主备条件连接，明确CONDITIONAL_ON_REGISTERED_POINT_GRAPH、SUPPLIED_GRAPH_ONLY及未观测到物理路径，不能把HOLD当作有效点。
6. `/v1/point-trajectories/predict`传`target_id/version?/prediction`，其中`prediction`含`as_of_unix_ms`、`at_unix_ms`及可选期限/资源配置。返回候选相机/通道集合，不输出人物精确点或概率。图的核验状态从登记包读取，请求无法覆盖。见[前向可达范围](point-reachability.md)。
7. 新合格monitoring事件自动重建；`/v1/point-trajectories/compare`传前后明确仍存活版本及新observation_id，比较旧预测支持程度，再从新版本预测。没有保留下来的历史依据不能用于比较。

正常入流幂等键为activity/source/event，同键同全部内容只回原交易，相异内容409，不能改时间/route/reconsider或换ID重放同一物理事件延长期限。活动内association_rebuild从核心当前完整Sightings重建；内部Rebuild全量替换算法仍存在，但不是公开raw入流入口。HOLD不进入确认轨迹，新P2未知时间最多首次可信隔离168小时，旧缺原期限立即到期。

## 结果语义

association_rebuild的expected_version首次0、之后当前轨迹头；版本冲突409不能只换号码重放旧资料。当前归属修订过期返回409/REBUILD_REQUIRED，显式存活历史带freshness；168小时清理会修剪历史证据及派生路线，不保证原版永久可还原。见[一致性合同](consistency.md)。当前成对产物cpp/build-activity/Release，完整启动须使用签名授权与可信UTC证明。

monitoring准入固定CAMERA_CAPTURE_60S，名义时间不改，误差为偏差绝对值＋测量/漂移＋量化误差，总界超过60000ms、未经核验/过期/歧义HOLD。默认max_clock_uncertainty_ms=60000只能收紧，不能传CORRECTED_UTC绕过；未来名义capture超过可信now+60000ms也HOLD。字段见[时间适配](clock-adapter.md)。身份/设备/通路独立核定，误差区间重叠保留顺序歧义。

- OBSERVED：可靠归属和合格时间支持的相机经过事件；模型位置是相机标记，不是人脚点。
- ORDERED/UNCERTAIN_CLOCK_ORDER：相邻经过记录在误差范围下是否能确定先后。
- CANDIDATE：在调用方已核定完整图与时间规则的条件下，可达集合计算完整；仍是可能性集合。
- CONDITIONAL_CANDIDATE：未核定/不完整拓扑或起点缺连接。候选集合之外也可能存在未覆盖位置。
- UNKNOWN：无合格观测、误差覆盖查询时刻、观测超过预测期限等，见reason。
- TRUNCATED：触及资源限制，集合不完整，不可用缺席判定不可能。
- CONSISTENT_WITH_PRIOR_CANDIDATES：新相机属于旧条件候选，不代表身份/路线已经额外验证。
- INCONSISTENT_WITH_PRIOR_MODEL：仅在旧依据未变化、核定完整图且计算完整时，时钟误差窗口上界仍无法到达；保留新观测供核查。
- UNRESOLVED：图不完整、预测失效、迟到、图变化或旧依据已修正/撤回。历史依据改变时reason为PRIOR_EVIDENCE_CHANGED_OR_RETRACTED，避免用已失效起点判定新事件矛盾。

as_of_unix_ms是选定快照内名义抓拍截止时刻，不是服务实际收件时间。历史version及截止时刻只是依据标识，到期证据仍要清理；过滤未来抓拍不还原过去未知归属修正或超窗历史。

预测允许在相机节点停留，默认也允许在通道中停留；有岔路保留多候选。开放窗约束出发时刻，不表示关闭时强制离开。默认预测期限5分钟，过期返回UNKNOWN。新观测会替换最后起点；相关误差重叠时保留多个可能起点。

## 现场数据边界

现有包有62个明确设备、63个绑定标记和31个未绑定标记。878条主候选连接来自局部模型的一般查询，不针对任何测试人的路线；64条涉及南门两个冲突位置的候选另存，2136个未完成模型查询保留。没有通过猜名绑定或取平均位置补齐数据。

真实时间数据/人员关联目前不足，`real-inputs.json`的30条历史抓拍仍未关联，6份时间配置均未核定。模拟时钟和模拟同人ID使用SIMULATED命名，仅用于本地逻辑测试；不能复用为生产校准。

## 本机构建与运行

本次在cpp/build-activity构建；正式接入前准备新的隔离状态目录，旧交付ZIP不冒充当前产物。

```powershell
cmake -S cpp -B cpp/build-activity -G "Visual Studio 17 2022" -A x64
cmake --build cpp/build-activity --config Release
cd go
go build -o ../cpp/build-activity/Release/face-trajectoryd.exe ./cmd/face-trajectoryd
go vet ./...
go test ./...
```

新活动服务完整启动见[服务合同](activity-service.md)，需要持久隔离state-file、受信公钥与外部UTC挑战证明。旧8769查看工具未接入当前签名活动选择合同，其历史演示不代表前端切换或登录联调完成；真实显示由现场前端接入后验收。
