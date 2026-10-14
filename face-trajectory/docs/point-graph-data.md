# 相机点位连接候选的离线导入

本模块将已经保存到本机的发布场景和一般模型诊断转换为相机点位连接候选，供点位级时空算法使用。脚本没有网络调用，不读取人员、抓拍、测试路线或未来事件，也不修改原证据。用户要求的生产服务器只读边界因此保持不变。

2026-10-08：导入策略已升至 `camera-point-model-candidates@2`，增加模型绑定和来源缺口记录，输出目录禁止覆盖。下列数量为旧快照基线；新目录复跑及模型变化操作见[模型更新](model-updates.md)。

## 输入与可复跑方法

实现：`collection/build_point_graph.py`，仅用 Python 标准库。

```powershell
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' `
  'D:\人脸算法\face-trajectory\collection\build_point_graph.py' `
  --scene 'D:\人脸算法\face-trajectory\evidence\2026-10-07-existing-data\site-scene.json' `
  --coverage 'D:\人脸算法\face-trajectory\evidence\2026-10-07-reassessment\model-coverage.json' `
  --output 'D:\人脸算法\face-trajectory\evidence\point-graph-new-run'
```

`site-scene.json` 来自既有只读审计，保存发布的设备绑定、相机标记、坐标矩阵和场地标注。`model-coverage.json` 由既有实际模型局部 NavMesh 生成，检查全部合格有向标记对；它与任何测试人无关。导入器首先验证该诊断记载的场景 SHA 与当前输入完全一致，再核对每个标记的设备绑定和坐标，不允许不同版本混用。

导入器不重新运行 Recast、不取最近若干台相机、不按名称猜设备、不插入人员路线。它保留源诊断的完整与失败结果。场地的“人行动线”保持发布设计标注，不能当作人员轨迹或已核实连接直接加入图。

输出：

- `graph-packet.json`：完整候选图及证据。
- `graph-build-report.json`：来源 SHA、策略、统计和导入一致性检查。
- `graph-import-verification.json`：本次确定性、边界拒绝和源数据保留验证，属于离线导入验证。

版本由两个输入文件 SHA、NavMesh SHA 和速度/停留/冲突处理策略的规范 JSON 哈希生成。改变输入或策略会改变版本；同一输入重复运行得到相同图文件。

## 输出契约与本次结果

顶层包含 `graph_version`、`scene_version`、`source_evidence_id`、`source_hashes`、`coordinate_frame`、`to_world_matrix`、`policy`、`cameras`、`edges`、`position_conflicts`、`ambiguous_connection_candidates`、`unresolved_connection_candidates`、`venue_annotations` 和 `limitations`。

| 项目 | 本次数量 |
| --- | ---: |
| 服务器设备目录 | 853 |
| 发布相机标记 | 94 |
| 显式绑定标记 / 设备 | 63 / 62 |
| 未绑定相机标记 | 31 |
| 设备目录中没有发布相机标记的设备 | 791 |
| 源模型检查的有向标记对 | 3,078 |
| 源模型完整连接候选 | 942 |
| 主 `edges` 中未核实候选 | 878 |
| 涉及位置冲突而排除的完整候选 | 64 |
| 源模型未得到完整连接的标记对 | 2,136 |
| 有主候选边的设备 / 没有主候选边的已绑定设备 | 52 / 10 |
| 场地标注 | 9 |

所有 62 个已绑定设备仍可用于记录“在该摄像头被观测到”的事实。模型投影无邻近面或缺候选连接，不使摄像头失效。缺乏模型覆盖的设备保留 `coverage_reasons`，未绑定标记和无发布位置设备也完整保留。未关联抓拍仍由经过记录适配器保持未关联，本导入器不会产生确认人员记录。

每条主边包含：

```text
edge_id, from_camera, to_camera,
from_annotation_id, to_annotation_id,
model_path_length_m, min_travel_ms, max_travel_ms, cost,
evidence_status=MODEL_CANDIDATE_UNVERIFIED,
travel_time_bound_verified=false,
hard_rejection_allowed=false,
directionality_verified=false
```

图以摄像头为节点。边仅表达“模型诊断提供了这两个相机点位之间的连接候选”，没有具体人员的预制折线。模型连接可能经过其他摄像头附近，因此它也不是已经核实的直接邻接、必然抓拍顺序或完整通道枚举。`cost` 取模型长度，仅供候选排序；不能将最低代价自动提升为已发生的真实路线。

顶层始终输出 `topology_verified=false`、`topology_is_complete=false`。`unresolved_connection_candidates` 完整保存所有未完成路径、未到终点及查询失败的源诊断，它们的 `physical_connection_impossible=false`。缺边在这个图上表示未知，不能据此否定人员事件或宣称现场不通。

## 时间假设与安全使用范围

默认最大速度假设 `2.5 m/s`，慢速假设 `0.3 m/s`，停留额度 `900,000 ms`，均可通过参数修改。

- `min_travel_ms = floor(model_path_length_m / max_speed_mps × 1000)`。
- `max_travel_ms = ceil(model_path_length_m / slow_speed_mps × 1000) + dwell_allowance_ms`。

这些只是可复查的建模参数。模型点位偏差、未实测/缺失几何、通道与观察区域未知，使“模型最短距离”不能当作现场人员路程的严格下界。速度上界也未现场核定。因此这些边上的最短时间不得用于硬性排除实际观测或停止保留其他未知候选。最大时间含有限停留额度，不证明人员不能停更久。

消费者需要保留 `hard_rejection_allowed=false` 等证据状态。若旧接口只接收边的时间上下界并据此判定“不可能”，不能将这个未核实图直接用于硬性拒绝。点位预测可以报告这些假设下的候选及其限制，现场校准后再以新版本升级为经过验证的约束。

## 南门冲突与候选相关性

设备 `4aae7357_cf5b_43cb_9a2a_6632a97bb318_0001` 的两个发布标记相距约 `108.349 m`。`cameras[].position_candidates` 保留两份原始标记及模型诊断，`position_conflicts` 保留来源和距离，没有取平均或选一个位置。

现有以 camera_id 为节点的图无法证明多条边采用的是同一个位置候选：若只把两个位置的边混入同一节点，可能从第一个位置进入、从第二个位置离开，构造无依据的跨位置跳跃。因此本版从主 `edges` 排除该设备的 64 条完整连接候选，放入 `ambiguous_connection_candidates`。这些边没有丢弃，可供后续支持“设备位置候选在整条路径上保持一致”的算法处理。该设备的经过记录仍能作为观测节点保留，其空间接续和预测应显示冲突或未知。

## 验证、踩坑和后续交接

本次先固定通用图，再交由其他模块使用。验证包括原输入哈希匹配、全部合格有向对恰好分区一次、所有绑定/未绑定标记保留、冲突边完整隔离、确定性输出，以及场景 SHA/标记绑定错误和非法速度参数的拒绝。它验证导入与证据边界，不验证真实人员轨迹准确率。

注意三类容易导致错误结论的处理：

1. 把模型查询成功当作实际通行验收：保留 `MODEL_CANDIDATE_UNVERIFIED`，公开覆盖范围和源失败对。
2. 把缺模型面或安装高度偏差当作相机无效：摄像头节点独立保留，模型仅作为连接证据。
3. 将同设备冲突位置的边直接合并：先隔离并保留完整候选，避免在相机节点上隐式跨位置。

后续应核对出入口、通道开放/单向等实际关系，并在拥有可靠时间与同人归属证据后，以只包含已发生经过事件的独立回放测试点位序列、候选保留和修正。不要使用选定测试路线反过来裁剪这张通用图，也不要为测试通过更改未公开的速度/停留参数。
