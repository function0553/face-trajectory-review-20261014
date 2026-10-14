# 给Gemini的审核任务

我是算法开发交付方，希望你审核这套安保人脸/时空算法当前源码。核心对象是P2 C++17/20+Go+SQLite，P1相关Python源码用于跨模块审核。请阅读实际文件，不要从名称、目录或一般经验直接判定已存在缺陷。

先确认你已读到源码正文。如果GitHub未被工具读取、内容截断或文件未加载，请列出缺失文件，不要编造行号或结论；可用仓库audit_bundle.txt作为纯文本附件补充。上传者也可直接把该TXT上传当前会话。

当前已按D-01～D-07任务书修复并新增拓扑参考折线；先读repair-topology-2026-10-14.md和validation-summary.json，再核对修复后源码。拓扑折点不是实测连续轨迹；相机原端点与时钟不确定性必须保留。

请按以下顺序审核：
1. P2完整HANDOFF、OpenAPI、activity-service、monitoring、clock-adapter、consistency、point-connections合同。
2. cpp/include/face_trajectory与cpp/src全部当前代码、go/cmd与go/internal，以及已有测试。
3. P1采集acquisition、monitoring_service、trajectory_bridge、quality、检测/对齐/特征、gallery索引、scheduling和monitoring_delivery；检查与P2的特征空间、person/source/camera/activity ID、采集时刻、三版本、幂等和质量字段是否一致。
4. C++状态/事务/活动隔离/保留、Go授权/UTC/推送与子进程生命周期，点位图/模型版本/纠错重放和查看器。

复核你上次提出的7类风险，逐条给出实际证据：
- 源码与构建产物一致性：此仓库仅文本源码，构建/内置依赖齐全程度与历史SHA的含义按实际清单判断。
- P1/P2端到端合同是否对齐；多个HANDOFF本身不证明接口冲突。
- Go daemon正常退出/强杀/worker崩溃、stdin EOF、Process.Kill/Wait及Windows Job Object相关生命周期，给出真实路径和复现条件；不要由exe命名推断GPU使用。
- 采集时间、PTS/钟源/时区/误差与队列延迟是否可能形成错误确认顺序。
- 登记图库动态更新、线程安全、person ID与历史修订一致性。
- 检测裁边/像素尺寸/模糊/姿态/五点质量与身份确认门槛；区分已有保护及其不足，不先假定缺失。
- 有界帧/结果队列、丢帧/背压、子进程、SQLite句柄/事务和长时间内存增长风险。

此外检查跨活动同人隔离、共享设备绑定、授权/上下文撤销、168小时采集时间期限、重试/恢复不得复活、HOLD准入、误差重叠顺序不确定、source命名空间、409与重建、推送去重和活动公平性。

输出格式：
1. 确认阅读的范围及缺失内容。
2. 已确认缺陷列表：严重性、原文件路径/行号、触发输入/时序、源码证据、实际后果、可复现步骤和最小修复建议。
3. 有待复现的风险与所需现场证据。
4. 已实现的保护及测试覆盖缺口。
5. 本地实现、真实联调、生产部署、准确率和容量分别评估。

现有合成测试和构建通过不代表现场准确率/600路容量。普通点位直线不是实际行走通路；鹰眼单镜轨迹、YOLO/ByteTrack/GTR候选不能被当作已部署。未持有独立真值时不得计算或宣称真实Accuracy/Precision/Recall。请不要要求降低现有身份/时间/质量门槛来通过测试。
