# 0.7 发布场景空间上下文

本接口复用既有模型标注，帮助轨迹调用方按 camera_id 找到摄像头在模型中的位置和附近场地标记。摄像头安装/标注点不是被抓拍人员脚下坐标；本接口不建立身份、不改抓拍时间、不生成导航图。算法仍在 C++，Go 仅转发，没有实现 Java 业务或生产前端。

新增 C++ `SiteContextStore`；worker 操作为 `site_scene_put`、`site_camera_context`。可选 HTTP 接口为 `POST /v1/site-scenes` 和 `POST /v1/site-scenes/camera-context`，合同见 OpenAPI 0.7.0。场景使用现有 SQLite `site_context` 命名空间、事务及恢复机制；同版本必须保持内容一致，修改需新 scene_version。纯排序变化允许幂等重放。

## 导入现有数据

只读审计转换在本地进行，不连接现场服务器：

```powershell
& ..\.venv-ops\Scripts\python.exe collection/import_published_scene.py `
  --audit evidence/2026-10-07-existing-data/audit.jsonl `
  --baseline evidence/2026-10-07-server-readonly `
  --model-id bfb3608f_af33_4000_bceb_b75e9677834c `
  --scene-url /resources/venues/previous-9099-v22.json `
  --calibration-profile home-v22@a13305f9ee298a4525d3 `
  --output evidence/site-import-new-run
```

转换器按 model_id / scene_url / calibration_profile 和 published、空 activityId 的基线发布精确选择，不选 draft，不按标注名称猜设备。矩阵不一致的发布方案不能自动合并。`shape.offsets` 是相对 anchor 的偏移，转换为绝对 local xyz 后注册；与已检查的现有前端 `MAe` 规则一致。

2026-10-08：导入器只取最后一份表快照，拒绝明确不完整／截断的快照；旧数据缺完整性声明时保留未核定状态。可传 `--model-revision`；版本前缀不再固定 v22，输出不覆盖旧文件。更新步骤见[模型更新](model-updates.md)。

输出 `site-scene.json` 是算法场景输入；`site-index.json` 是绑定和冲突摘要；`capture-time-review.json` 保留抓拍两种时间原文、单次设备偏时诊断及 HOLD 原因，不产生 UTC 修正或身份归属。当前包有 103 个标注、94 个摄像头标记，63 个标记绑定到 62 个有效相机 ID，31 个标记尚无设备绑定。5 个安检门、2 个闸机、1 条隔离设施、1 条人行动线同时保留。

## 调用

将场景 JSON 包在 `{"site": …}` 中提交 `POST /v1/site-scenes`。直接调用 worker 时额外带 `"op":"site_scene_put"`。注册的是本地算法状态，不上传到生产 Java 或原模型。

查询示例（scene_version 按实际导入文件取得）：

```json
{
  "op": "site_camera_context",
  "scene_version": "published-v22-readonly-783be849074170d3",
  "camera_ref": "a8c2bde0_c281_4190_9946_c0910a253b18_0001",
  "id_namespace": "CAMERA_ID",
  "radius_m": 100
}
```

`id_namespace` 必填 CAMERA_ID 或 VCL_CHANNEL_ID，后者只依显式目录映射；父设备 UUID 和注册号不推断通道。返回的状态包括：

| 状态 | 含义 |
| --- | --- |
| UNKNOWN_CAMERA | 目录中无该命名空间标识 |
| AMBIGUOUS_DEVICE | 同一 VCL 标识映射多个设备，不自动选一个 |
| NO_PUBLISHED_CAMERA_MARKER | 相机存在，但无当前发布的可见摄像头标记 |
| BOUND_MARKER | 有明确相机绑定，标注位置在声明的重复容差内一致 |
| AMBIGUOUS_POSITION | 多个标记相距超过重复容差，保留每个候选及来源 |

每个候选含 annotation_id、source_scene_id、local_xyz、world_ecef_xyz 和附近非摄像头标记。距离按三维米计算；折线用线段投影而非仅找最近顶点，保留高度；多边形距离指边界，不等于区域归属。不会依据邻近关系添加相机通路或自动确认观察区域。`person_position_observed`、`navigation_verified` 始终为 false。

输入矩阵为列主序 local metres → ECEF_WGS84 的刚性仿射变换，拒绝缩放/斜切以免局部米距离失真。当前发布矩阵给出模型的渲染世界坐标，不能据此声称现场 GIS 或人员定位精度。floor 原值只是标注属性；不能用名称中的“二楼”或全部为0的 floor 字段自动建立楼层约束。

## 本地结果和验证

只在本地运行 `evidence/2026-10-07-existing-data/verify_existing_data.py`、`replay_trajectory_local.py`。前者验证真实标注注册、设备映射、三维距离、冲突、持久化和 Go HTTP；后者验证真实原始抓拍不能生成假锚点，以及明确模拟的身份/UTC/通路条件下能构建轨迹、绑定既有折线、正确查询时间和恢复。

本地查看页为同目录 `preview/index.html`，全部嵌入结果，无外部脚本/地图/请求，也可以直接打开 HTML。需要 HTTP 时只启动 `127.0.0.1` 的静态服务。界面显示预先取得的 C++ 返回，不运行识别，不假装实时跟踪；中间时刻显示整个可能经过路段，不按时间匀速插一个人物点。该页面只是算法验收查看工具，最终生产显示仍由前端负责。

用户已确认南门两份标注暂不确定：相距108.35米的两个候选持续保留冲突。所有测试、状态文件和查看页都在本地，不允许为测试操作生产服务器。
