# 普通摄像头点到点连接交付

更新：2026-10-13，Asia/Singapore。用户明确普通摄像头只需点位连接，鹰眼单镜才需连续轨迹，且本轮只交付算法接口与本地查看。当前实现复用SCRFD/ArcFace512、P2确认身份/可信时间、活动隔离与168小时保留；未增加YOLO、ByteTrack或GTR。本地验证完成，真实联调/生产/容量仍未验收。

## 查询与绘制合同

正常观测继续经`POST /v1/monitoring/observe`自动关联和重建；随后以`POST /v1/point-trajectories/get`查询，携带后端签名query授权、`activity_id`及当前活动三版本。不使用已拒绝的raw/rebuild接口绕开准入，不需要先画出NavMesh完整路线。场景登记仍需明确相机位置/坐标系；点位图可以无已核定边并保持`topology_verified=false/topology_is_complete=false`。

```json
{
  "activity_id": "实际活动ID",
  "activity_num": "实际活动编号",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "target_id": "活动内目标ID"
}
```

三版本必须取实际配置，示例1不是固定生产值。省略`version`查询当前快照；旧归属发生变化时返回409/REBUILD_REQUIRED。显式历史版本仍带freshness，过期数据不会因查询复活。活动/版本/时间/鉴权沿用[0.13服务合同](activity-service.md)。

`point_trace`新增`drawing_mode=CAMERA_POINT_CONNECTIONS`和`connections`，既有observations/observation_order继续保留。每条connection包含：

| 字段 | 使用方式 |
| --- | --- |
| from/to_source_id、from/to_observation_id | 来源和原事件ID一起查对应观测，不能只按observation_id跨来源合并 |
| from/to_camera_id | 两个摄像头端点，不包含插入的推断中间相机 |
| drawing_status=DRAWABLE | coordinates恰好两个local xyz点，按point_trace.coordinate_frame绘制直线 |
| drawing_status=UNRESOLVED_POSITION | 缺失位置、冲突或多个标记坐标，coordinates为空；保留端点记录/提示，不平均或取第一候选 |
| drawing_status=SESSION_BREAK | 核心快照将两点分在不同轨迹会话，coordinates为空，不跨会话硬连 |
| order_status=ORDERED | 展示实线和实心方向箭头 |
| order_status=UNCERTAIN_CLOCK_ORDER | 展示虚线和空心箭头；箭头仅表示from/to记录方向，按名义采集时间排列，实际先后待核实 |
| meaning= DIRECT_CAMERA_POINT_CONNECTION | 两个摄像头点位之间的展示连接；physical_path_observed始终false |

仅确认且时间合格的同目标观测参与。连续同镜事件不产生自连线，原始证据全部保留，跨镜使用该次转换的实际边界观测；A→B→A保留返回连接。第三个相机的时间区间跨越相邻记录时，整个跨镜重叠组保留顺序待定，不因一对名义时间互不重叠就宣称唯一邻接。int64时间比较用整数差值和饱和区间，保留毫秒精度。

连接不依赖路线候选存在；图不完整时仍能展示确认端点，不把直线当实际走过的通路。位置标记仍是相机安装点。JSON持久状态未新增连接副本，连接按查询快照推导，因此纠错、重放、活动隔离、版本新鲜度与到期修剪沿用原核心。查询不改变身份/轨迹或生成投递通知，但服务仍会推进既有可信UTC防回退水位。

## 本地查看与启动

只读工具在[tools/point-viewer](../tools/point-viewer/index.html)，默认空白；选择保存的完整JSON回复后显示点位、实线/虚线、未绘制原因和结果状态。可以切换X/Z、X/Y、Y/Z投影，不下载完整场地模型。读取文件仅在浏览器内，不发送到后台。切换文件先清空旧活动/目标，加载失败也清空，避免误显示前一人的连线。

2026-10-13用户复核后修正了重叠连接显示：同一对相机、相同原始端点的记录共享一条几何线，图上显示所有记录编号，例如`#2 / #3`；点击编号可轮换点选，每条记录在列表中也可独立高亮。连接记录数与图上线段数分别展示，不删记录、不移点位、不增加虚构路线。按用户追加要求补上方向箭头：顺序确定为实心，顺序待定为虚线加空心箭头；空心箭头只对应from/to记录方向，不能据此确定实际先后。双向记录在共享线段两端分别标箭头，点选第3条仅突出C→B；各方向的确定性独立判断。不同相机对或不同3D端点不会仅因投影重叠就被合并。投影重合会单列提示，可切换平面检查。

查看工具单文件上限16MiB、经过和连接各最多50000条；超出浏览器可精确显示的时间会明确拒绝。窄窗口下点位标签按实际画布大小绘制，避免随整张SVG缩小而无法阅读。这些是查看工具边界，不是服务器容量结论。

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' tools/point-viewer/serve_viewer.py --port 8793
```

访问[http://127.0.0.1:8793/](http://127.0.0.1:8793/)。服务只绑定loopback，只允许读取查看静态文件；没有API代理、上传、凭证或数据库读写。关闭该终端进程即可停止。旧8769查看页使用历史raw/rebuild方式，不适用当前签名监控入流，本次未改旧证据或占用进程。

“查看合成示例”读取真实本地Go/C++产生的[SYNTHETIC接口示例](../tools/point-viewer/sample.json)：8条确认经过、5条跨镜连接，其中3条可画且顺序待定，2条坐标未决；3条可画记录对应2条几何线，`B—C`与`C—B`共享端点。A/B/C原始坐标本就共线，因此图形仍是直线，不能为了显示往返而挪动相机位置。该示例没有实际身份/通路真值。离线文件的CURRENT只表示导出时的状态，不能证明随后未发生归属变化；真正查询当前结果仍需服务授权。最初验证见[原浏览器记录](../evidence/2026-10-13-point-connections/browser-verification.json)，当前显示见[重叠显示修正](../evidence/2026-10-13-point-connection-display-fix/REVIEW.md)。

## 构建与复跑

本次独立构建目录是`cpp/build-point-connections/Release`，当前worker SHA前缀`2e2b33aa`，Go服务保持原源码/相同SHA前缀`c7f7e2b5`。旧build-activity产物和已有运行库未覆盖。启用新输出时选用本目录worker及配套Go服务，沿用主交接第27节签名/可信UTC配置；本次没有自动切换现有服务或迁移数据库。

```powershell
Set-Location -LiteralPath 'D:\人脸算法\face-trajectory'
$pcCMake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
& $pcCMake -S cpp -B cpp/build-point-connections -G 'Visual Studio 17 2022' -A x64
& $pcCMake --build cpp/build-point-connections --config Release
& 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe' --test-dir cpp/build-point-connections -C Release --output-on-failure
node --test tools/point-viewer/trace.test.mjs
node --test tools/point-viewer/overlap.test.mjs
node --test tools/point-viewer/direction.test.mjs
$pcRun = 'evidence/2026-10-13-point-connections/recheck-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' tools/verify_point_connections.py --worker cpp/build-point-connections/Release/face_trajectory_worker.exe --service cpp/build-point-connections/Release/face-trajectoryd.exe --output $pcRun
```

复跑必须用不存在的output路径，自动启动临时loopback Go/C++并新建隔离库/合成签名及UTC证明，结束后关闭测试服务；不接生产资料。Go重新构建与vet/test沿用主交接构建命令。

## 验证、踩坑与交接

新增C++23项边界检查、8项查看数据检查、真实签名Go/C++/隔离SQLite20项联调完成；原活动核心40项、服务31项回归通过，Go build/vet/test通过。完整OpenAPI校验覆盖38个操作、569个引用，以及新旧联调116个HTTP响应，含错误状态。证据：[HTTP20项](../evidence/2026-10-13-point-connections/http-final/summary.json)、[核心40项](../evidence/2026-10-13-point-connections/core-final/summary.json)、[服务31项](../evidence/2026-10-13-point-connections/service-final/summary.json)、[OpenAPI](../evidence/2026-10-13-point-connections/openapi-final.json)。测试均为合成合同验证，不是现场身份精度或吞吐证明。

方法是先复核旧接口与查看工具，再在CameraPointTrace增加展示投影字段，保留完整观测与旧observation_order标识；对新connection单独还原原事件ID并保留source命名空间。位置来自当前scene绑定，拓扑未知不制造坐标。随后独立构建、实际带签名HTTP入流/纠错/重启和浏览器文件读取验证。

坑点及处理：旧查看页不能绕过0.13入流，改用只读结果查看；跨来源同名事件须保留source；Windows long double不足以精确表示极大int64毫秒，用整数比较；测试初稿把跨目标输入当可重建数据，被原核心正确拒绝，改为验证拒绝并单列待定观测排除；全库只读断言发现既有UTC水位推进，改为明确验证只有该水位可变；重启测试的固定UTC落后于水位，被正确拒绝，测试证明改取更大的合成UTC，不放松业务防回退。上述失败保留在http-01/http-02，未覆盖成功记录。

新增无路线案例揭示旧OpenAPI `primary` 的nullable+allOf不能接收实际null，改为保持完整字段约束的NullablePathCandidate，未放宽身份/时间/通路判断。校验环境复用历史独立venv；P1推理venv没有cryptography，不为运行时新增测试依赖。新源码和程序SHA以[本次清单](../evidence/2026-10-13-point-connections/final-manifest.json)为准；旧日期清单继续代表旧快照。

交接内容：后端仍提供权威activity/device绑定、身份资料、采集钟源及坐标版本；前端按新connections两个端点画直线，保留status/freshness提示。生产部署、真实标签评估、服务器容量和鹰眼单镜跟踪另行完成。普通摄像头连线不以部署YOLO/ByteTrack为前置条件，也未推断设备型号来自动修改活动绑定范围。

显示修正的4项重叠、4项方向回归与原8项查看检查全部通过，共16项。实际页面验证了无完全重叠的重复描边、3条记录编号覆盖、默认3个空心方向箭头、点选第3条仅突出C→B、图上编号轮换及恢复全部。原API/C++/Go、OpenAPI和sample.json未改，本次未重新运行它们的旧测试或声称恢复现场验收；当前查看源码SHA见[显示修正清单](../evidence/2026-10-13-point-connection-display-fix/final-manifest.json)。原清单继续代表修正前快照。

接入模型后的绘制前提：检测/特征结果先经现有确认身份与可信时间链形成monitoring观测，后端提供当前活动的明确device绑定和同坐标系相机安装点，再查询点位接口、导出给查看器。符合合同的可画端点已经通过本地签名Go/C++验证；仅加载模型或提供ByteTrack局部ID不足以生成确认跨镜路线。真实相机→P1→P2→查看器的完整链路本轮未验证。普通输出是经过点位连接，不能据直线宣称两点之间实际通路；鹰眼画面轨迹仍后置。
