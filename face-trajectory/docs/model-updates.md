# 场地三维模型更新与算法版本

模型更新使用新的场景、连接图和轨迹版本；已有抓拍、归属证据及原始时间保持原样。这里处理点位与连接依据的变化，不运行人脸模型、不重新抓拍，也不写生产服务器。

## 本次补齐

- 场景导入不再固定 v22 版本前缀；可用 `--model-revision` 记录明确的发布版本或清单标识。同一 URL/model_id 下替换了模型内容，也必须使用新的来源标识和新几何数据。该字段是来源声明，不证明文件或现场准确性。
- 一个审计文件中的多个 `studio_scene` 快照只选最后一个，不把撤下的旧标记再合并回来。显式 `complete/snapshot_complete/list_complete=false` 或 `truncated/partial=true` 会拒绝；无完整性字段的历史审计标为 `LEGACY_COMPLETENESS_UNVERIFIED`，不能据缺失记录证明实际拆除。
- 通用模型诊断显式传入 scene、NavMesh 和几何报告，继续复用既有 Recast/Detour。不再隐式读取旧日期的场景/网格。源文件 SHA、模型绑定及来源缺口随结果保留。
- C++ 登记模型候选包时核对变换矩阵、模型元信息、设备通道、全部位置候选和边的标记绑定。仅更换 `scene_version`、仍带旧位置或旧矩阵会拒绝。纯手工最小图仍兼容；无来源字段不代表已验证模型。
- `assess_point_graph_update.py` 给出点位、边、坐标系及证据差异。任何图版本变化，都需重放使用旧图的目标；不能只重算发生变化的边两端，变化可能传播到多跳连接。该工具不自动切换服务或查询生产人员。

## 新模型接入顺序

1. 只读取得当前发布选择、完整场景标注和相机目录，保留来源、读取范围及哈希。不能仅凭旧入口的 7 份索引没有变化，就认定整个新模型没变。新快照应显式声明读取完整性。
2. 用 `collection/import_published_scene.py --audit <完整审计.jsonl> --model-id <明确ID> --scene-url <明确场景URL> --calibration-profile <明确校准标识> --model-revision <发布或清单标识> --output <新目录>` 导入。输出包含 `site-scene.json`、`site-index.json`；禁止覆盖旧输出。
3. 从该版本真实模型生成新的局部几何和 NavMesh。已有 `evidence/2026-10-07-model-route` 脚本是历史复现资料，路径及 ROI 固定，不能仅改文件名冒充新模型重建。新几何生成流程应保留源资源/变换/参数及生成后网格 SHA；本轮没有实现任意服务器模型的自动下载及几何重建。
4. 新几何报告的 `model_revision` 应与 scene 一致；若声明 `source_scene_sha256/source_navmesh_sha256`，诊断工具会核对实际字节。不得为通过校验手工给旧网格贴新 revision。历史未记录完整生成链时保持 `LEGACY_UNVERIFIED`；声明匹配也只是 `REVISION_MATCHED_PROVENANCE_UNVERIFIED`，不是完整来源或现场验收。
5. 运行诊断、建图和差异检查，查看冲突、未完成查询及来源缺口。再按现有接口登记新 scene/graph，用完整权威原始记录集重建目标，保留 admissions 中的 HOLD。新版本单独预测；旧版本继续可审计。跨图比较返回 `UNRESOLVED/GRAPH_CHANGED_REBUILD_REQUIRED`，不删除新观测。

下面是本轮**旧快照**的可复跑命令；`$p2Run` 每次必须是新目录。这验证工具及契约，不代表新模型或现场轨迹通过。

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
$p2Run = Join-Path 'evidence' ('model-update-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $p2Run | Out-Null
node collection/check_model_coverage.mjs `
  --scene evidence/2026-10-07-existing-data/site-scene.json `
  --navmesh evidence/2026-10-07-model-route/navmesh.bin `
  --geometry-report evidence/2026-10-07-model-route/geometry-report.json `
  --recast-module evidence/2026-10-07-model-route/node_modules/@recast-navigation/core/dist/index.mjs `
  --output "$p2Run/model-coverage.json"
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' collection/build_point_graph.py `
  --scene evidence/2026-10-07-existing-data/site-scene.json `
  --coverage "$p2Run/model-coverage.json" --output "$p2Run/graph"
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' collection/assess_point_graph_update.py `
  --previous evidence/2026-10-07-point-trajectory/graph-packet.json `
  --candidate "$p2Run/graph/graph-packet.json" --output "$p2Run/update-assessment.json"
```

通用诊断当前上限为 1024 标记、100000 有向标记对；超过上限明确失败，不输出裁剪后冒充完整的报告。这不是全场容量验收。图导入策略已升至 `camera-point-model-candidates@2`，因此即使旧几何结果不变，新来源字段也会得到新图版本。

当前验证和服务器连接边界见[本轮报告](../evidence/2026-10-08-continuation/REVIEW.md)。服务器刷新脚本仅用于有界元数据复核，并非包含全部标注的直接导入快照。


0.10.0 重放已有目标时必须以当前轨迹版本作为 expected_version；归属改变后默认查询保护与旧状态迁移规则见[一致性合同](consistency.md)。更新图和准备完整权威记录须核对同一版本依据，409后不能仅刷新版本号重发旧资料。
