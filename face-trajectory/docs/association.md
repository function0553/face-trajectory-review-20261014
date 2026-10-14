# 跨摄像头同人关联

输入一张摄像头抓拍的人脸特征及来源信息，C++返回活动内target_id、判定依据和确认观测。复用已有512维人脸特征和多模板余弦检索，不新增模型。0.13所有活动接口携带activity_id及当前config/binding/monitoring三版本和受信签名授权，见[多活动合同](activity-service.md)。person_id保持业务原值，登记图库按活动显式同步；匿名ID、类别、会话、底层索引与轨迹隔离，人员不因30秒没出现而过期，抓拍衍生模板与历史按168小时清理。

## 接入顺序

1. P1 的 `trajectory_bridge` 复用检测、对齐和 ArcFace，产出 512 维特征、实际模型契约、检测分数和原图人脸框最短边像素数，以 SQLite outbox 投递。保留稳定事件 ID、同帧 frame_id、相机、来源及采集时钟证据。P2 接口接收特征；适配器接收已解码帧，硬件时间源仍由采集端提供。见[运行说明](runtime-and-geometry.md)。
2. 对每张抓拍调用`POST /v1/monitoring/observe`，携带明确活动版本、device route和time_record，经ClockAdapter核验后才进入关联；旧`associations/observe`已拒绝为SOURCE_CONFLICT/409。无需先给姓名或target_id。登记人员通过活动内`monitoring/persons`类别/模板原子同步，或有identity授权的`templates`；其他人员产生活动内不可逆`auto:` ID。
3. 保存原始输入和关联回复。按需查询某人时，调用 `POST /v1/associations/rebuild`，算法直接取该目标完整确认观测生成版本轨迹，再调用现有 `/v1/trajectories/get` 和 `/v1/timeline/query`。
4. 姓名／身份证由 Java 资料映射到 target_id；人脸图片经同一提取程序和 `/v1/search` 获取候选，确认选定目标后查询。查询图片本身不能提交为摄像头观测。匿名候选的 `person_entity_id` 就是自动轨迹 target_id。

完整字段见[OpenAPI 0.13.0](../openapi.yaml)。监控入流自动重建，明确correct后的受影响目标继续由associations/rebuild取核心权威集合。启用[后端推送](backend-push.md)后结果带活动版本及原期限发送。401签名身份无效、403无活动动作权限、400非法请求、409版本或同键内容冲突、502 worker不可用、503队列/存储满或可信UTC不可用、504请求超时；正常身份待定/CONFLICT是200，须检查decision。

## 判定与模板

0.10.0 的 trajectory_effects 给出受影响目标的关联修订、轨迹版本和 rebuild_required。新确认观测或归属修正使旧当前快照失效；默认查询返回 409 / REBUILD_REQUIRED，显式历史版本仍可读并带 freshness。association_rebuild 携带 expected_version，首次为 0，之后为该目标当前轨迹版本。详细流程见[一致性合同](consistency.md)；association 的业务 decision=CONFLICT 与 HTTP 409 的版本/来源冲突是不同情况。

| decision | 意义 | 是否产生确认轨迹观测 |
| --- | --- | --- |
| CREATED | 全部同空间人员得分低于新目标阈值，生成匿名 ID | 是 |
| MATCHED | 可行候选通过匹配阈值、人员差距和已有约束 | 是 |
| PROVISIONAL | 得分或差距不足，或要求的拓扑证据缺失 | 否 |
| CONFLICT | 相似脸与已接受相机／跟踪证据冲突 | 否 |
| UNASSIGNED | 抓拍时间或检测分数／人脸大小不合格 | 否 |
| CORRECTED | 有独立核验依据的外部归属修正 | 是 |

匹配按“人”比较，取每人的最佳模板；计算候选差距时不把同一个人的不同模板算成竞争人员，也不靠固定 TopK 隐藏竞争者。回复保留视觉 Top8 加上必要的可行第一／第二候选，最多 10 项。精确图库分数的剩余上界已经足以证明匹配差距或待定结果时，停止读取更低分人员的历史并返回 `remaining_similarity_upper_bound`；这个上界不冒充实测第二候选。尚可能改变结论的第9名等候选仍必须检查。cosine 分数不是身份概率。

首次建人保留首个合格模板；后续只有达到 template_update_threshold 且低于 max_template_similarity 的确认匹配才允许补模板，避免反复存几乎相同的脸。自动模板默认最多 8 张，满时按检测分数 × 人脸大小保留较好的样本。此指标只用于有限模板保留，不代表已实现模糊、遮挡、姿态等完整人脸质量模型；人工登记模板另外管理。待定／冲突不更新图库。

关联复用严格特征空间契约：识别／检测模型、检测配置、对齐、预处理与输出版本必须一致；不同版本不能因都是 512 维就混比。模型升级后的人员对齐需要独立核验，当前不自动跨模型合并 ID。

## 时间、拓扑与局部跟踪

只接纳 `UTC_CAPTURE`、`clock_domain=UTC`、已知且不超策略上限的时钟误差进入确认链。误差未知可填负值，结果为 UNASSIGNED；不能估填 0，不能把服务器处理时间写成采集时间。同一 observation_id 的内容不可修改，重试返回原判定和原关联 version。

相机约束检查该目标已有的确认观测，包括迟到观测前后两侧。有向通路沿最短通行时间及开放窗口判断是否存在可能到达的路径，支持漏掉中间摄像头的观测；max_travel_ms 是软参考，不用于超时排除身份。身份排除用时钟外包络，只有即使考虑时间误差也不可能才排除，与轨迹保守选路的判定目的不同。

**现场未核实完整通路时保持 topology_is_complete=false。** 未知连接或相机未纳入图会返回 UNKNOWN 相机兼容性；require_topology=true 或 topology_is_complete=true 时，无法证明整链可行的视觉匹配保持 PROVISIONAL。require_topology=false 且 topology_is_complete=false 时允许人脸证据通过后关联，候选回复仍显示 UNKNOWN。可传 `{"version":"unmapped-v1","edges":[]}` 先验证特征链路。

只有核实方向、最短时间、开放窗口和重叠观察区域后，才把 topology_is_complete 设为 true。该值是调用方对资料完整性的声明，算法不能验证现场是否遗漏通路；错误声明可能排除正确身份。`overlapping_cameras` 显式列出观察区域重叠的无序相机对，允许同时拍到同一人。

`local_track_id` 与 `track_session_id` 必须同时提供或同时留空，按来源＋相机＋跟踪会话＋局部 ID 分区。重启／循环复用 ID 时更换会话。局部 ID 只约束单相机轨迹一致性，不能证明跨摄像头同人。相同来源和相机的同一帧中，不同局部目标的脸不能归为同一个人；没有局部 ID 时，两条不同脸观测也按互斥处理。

policy_id和graph.version均不可静默改内容；改规则/拓扑使用新版本。同activity/source/observation键的原内容完全不可变，reconsider、补时间、route或规则改变均409，当前没有原键补证修订入口；已经确认的归属只能显式correct，不能换ID重放同一物理事件延长期限。

## 修正与状态保存

`POST /v1/associations/correct`输入活动三版本、原观察source_id/observation_id、核实后的target_id与独立evidence_id，需要该活动identity授权。算法不读取外部核验材料，调用方保证依据已核实；修正不能制造合格时间或质量。移走原观察及自动模板，回复affected_target_ids，对两个目标分别associations/rebuild。旧目标可重建为空，仍存活历史版本保留版本号，但过期证据和派生路线必须修剪。

修正只作用于指定观测，**不会把此前受误认影响的其他观测自动批量改归属**；需核对其他证据并逐条修正。局部跟踪的一致性从当前确认记录读取，不保留过时的独立人员绑定。第一次归属修正产生的新人员模板不等于自动验证了其整个历史。

`auto:` 人员 ID 和 `assoc-template:` 模板 ID 为算法保留前缀，模板增删接口不能直接操作自动模板。自动目标的业务资料映射由 Java 管理。一次修正、自动补模板或观测判定的 association.version 与 trajectory.version、gallery_version 是三个不同序列。

SQLite事务按活动保存观测、关联、模板、修正及历史轨迹，提交后回复，重启按scope恢复；外部恢复也不能让超窗轨迹重现。登记模板常驻并排除，抓拍、自动模板、所有历史版本、索引、缓存、投递与重放来源按168小时清理。容量满拒绝新写入，删除默认关闭，显式retention-enabled后周期执行；只保留不可逆幂等摘要及无过期点的最小运行状态。Java独立权威历史不由算法删除；容量与真实精度尚待验收。

## 离线示例与现场验收

旧[HTTP脚本归档](../../workspace-docs/archives/project-cleanup-20261007.zip)保留历史证据，其原raw入流合同不适用0.13。当前复跑使用唯一交接的verify_core/verify_service/verify_retention及边界脚本，真正Go/C++与新隔离SQLite、合成签名身份与UTC验证；测试阈值不能作为现场参数。

现场优先取同人与不同人的跨相机脸图／特征、模型契约、原始帧和局部跟踪字段、可信时钟证据、实际通路及重叠区。独立人工标签用于计算错并、拆分和待定比例，不能拿算法自己的归组当真值。进一步资料见 [采集清单](../collection/README.md)。

当前实现是人脸特征驱动的同人关联。没有合格脸的背身／全遮挡观测不能靠本模块可靠补出身份；尚未新增人体 ReID 或视频跟踪模型。P1 入流适配和三维折线绑定已实现；真实阈值、跨相机准确率、实际服务器入流和全场规模仍待现场数据验证；真实坐标标定及鹰眼 ARM 座位定位继续后置。
