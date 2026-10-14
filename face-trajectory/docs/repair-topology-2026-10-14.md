# D-01～D-07 修复与拓扑折线交接

日期：2026-10-14，Asia/Singapore。当前源码为准；原工作区没有 Git 历史，GitHub 使用现有 `function0553/face-trajectory-review-20261014` 审核仓库。用户要求先本地修复验证，再统一同步。

## 实现与证据范围

| 项目 | 当前处理 |
| --- | --- |
| D-01 | 共用 `PathToUtf8`，覆盖 State/P1 维护路径及路径 JSON；LP64 明确 int64；optional JSON 明确构造；Scan 回调明确 string 类型。CMake 接受 17/20，另修复 MSVC C++20 的 string/JSON 比较二义性。 |
| D-02 | 请求取消/20秒限制只用于排队和写入前准入；已开始的事务等待内部常规20秒/冷恢复5分钟计时器。Fair 外层同步遵守该边界，防止它再次提前丢弃回执。daemon HTTP 写超时改为6分钟，覆盖排队、5分钟恢复和回收余量。 |
| D-03 | stdin关闭、Kill、唯一一次Wait；3秒未回收则阻止替换，下一请求只能在旧进程已回收后恢复。Linux Pdeathsig=SIGKILL，锁住启动OS线程直到回收；Windows先挂起启动，加入 KILL_ON_JOB_CLOSE Job 后恢复，不能在绑定前打开状态库。 |
| D-04 | 采用0.13严格不可变事件方案。清除 monitoring 的 reconsider 重评估分支，monitoring Observe 不传 reconsider。相同HOLD重试返回原回执；改变 reconsider/evidence/观测/上下文等仍409。纠偏走 associations correct/rebuild。 |
| D-05 | 当前 TrajectoryExtractor/Outbox 已包含 activity_id、activity_num、三版本和 route_rule_id，未重做已有实现。bridge 对400/404/409/410/413/422隔离、保存原载荷和最多2048字节错误详情后继续FIFO；其他HTTP错误和网络错误保持可重试。DeliveryStore也补齐410。 |
| D-06 | push_outbox 持续写 ref=activity_id、track=event_id，启动兼容回填旧行。活动计数走Count覆盖索引；push_next仅每活动取一条头部；ack按event索引定位并核对该活动FIFO。State视图共用ScanAfter，结果游标按20位sequence主键查询并跳过过期行。 |
| D-07 | 实际生命周期操作为activity_sync/activity_bindings/activity_monitoring，源码没有activity_upsert。成功停用或版本改变后淘汰对应runtime，不删除持久图库/记录。当前DeliveryStore已从现有cursor循环寻找非空槽位，其辅助类别实际名为UNREGISTERED；用回归证明无饥饿。 |

没有改变模型、512D、身份相似度/差值门限、质量准入或相机时间不确定度门限。签名授权、活动/source/event幂等、严格小于cutoff才过期、410 EXPIRED与不可逆墓碑继续生效。已有UNKNOWN、HOLD、AMBIGUOUS及409 REBUILD_REQUIRED规则保持。

备份及测试证据在 [`../evidence/2026-10-14-repair-topology/`](../evidence/2026-10-14-repair-topology/)。`before/`是修复前源码快照，包含已添加、尚未修复的Go失败回归；不是运行状态库备份。`before-manifest.json`记录原字节摘要。旧构建目录保留，新Windows构建为 `cpp/build-repair17/Release` 与 `cpp/build-repair20/Release`，新daemon为证据目录内 `face-trajectoryd.exe`。未切换既有服务或修改远端。

## 3D 前端接入

现有点位接口实际字段为 `point_trace.connections`，不是任务书旧命名的CameraLink/PointConnection类；保留全部原字段，增加独立呈现用CameraLink和可选字段。监控按需请求可返回 `monitoring.point_connections`。

1. 用真实发布场景和同版本NavMesh、geometry报告运行 `collection/check_model_coverage.mjs`。复用已有Recast/Detour，直接保留完整 findStraightPath 的点序列；把 Detour `[x,z,-y]`转换回场景local XYZ。失败/部分路径不导出折点。
2. `collection/build_point_graph.py`原样保留有限、2～4096个场景折点，写入边的 `waypoints`，并携带几何类型/坐标系。旧报告没有点时不根据距离反造点。输出仍为既有 `graph-packet.json`，不引入第二份冲突格式。
3. 注册scene和新版point_graph，再经原monitoring入流取得轨迹；graph_version不能原位换几何。源模型变化仍需重新生成并显式重建受影响轨迹。
4. 点位查询仍是 `POST /v1/point-trajectories/get`，携带实际活动三版本和target_id。相机 `coordinates` 永远只含两个原安装点；`from_position/to_position`是它们的可选别名，`chord_distance_m`取原端点欧氏距离，`geometry_kind=CAMERA_CHORD`。
5. 图中可达最短路全部片段都有提供的几何且连接点一致时，另外输出 `route_waypoints:[{x,y,z},…]`、`route_edge_ids`、`route_geometry_kind=TOPOLOGY_INFERRED_PATH`与`route_status=AVAILABLE`。多片段按既有cost求最短参考通路，保留中间相机/门洞折点；缺片段返回GEOMETRY_UNAVAILABLE，断开的投影返回DISCONNECTED_GEOMETRY，受资源限制返回SEARCH_LIMIT。没有完整且已核定拓扑时缺路仍UNKNOWN_NOT_IMPOSSIBLE。不给缺失部分补直线，也不跳到更贵的路线冒充最短路。
6. 可选 `POST /v1/monitoring/get` 加 `include_point_trace:true`，返回 `monitoring.point_connections` 和 `point_geometry`（scene/graph/frame/拓扑资格）。默认请求不增加几何输出。当前归属已变化仍409，不能通过传历史version绕开。读取不触发主动推送；无确认观测时返回空连接及NO_CONFIRMED_OBSERVATIONS。
7. `tools/point-viewer`切换相机弦线/拓扑参考折线；无折线时该模式不回退成直线。确认方向实线单向箭头，区间重叠虚线双向标识；HOLD、UNREACHABLE、位置未解及会话断点不造线。同端点不同走廊的折线分开分组。保留旧弦线示例，并新增来自签名Go/C++回归的 `topology-sample.json`。

拓扑几何只表达模型条件下的参考路径：安装点不是行人脚点，模型导航表面端点可能与安装点不同，因此不互相覆盖。不把折点当做实测位置、实测中间经过或确认时间；`physical_path_observed=false`保持。`direction_status=ORDER_UNCERTAIN`、`order_confirmed=false`与原UNCERTAIN_CLOCK_ORDER一致。

## 验证

Windows MSVC 19.44、Linux x86_64 GCC 12.3 的 C++17/C++20均构建core、worker及测试，CTest两个目标通过；point_connections_test包含原断言及最短路、端点保留、重叠时间、缺失/断裂/坏几何回归。另先通过GCC11 C++20。Linux测试在本机WSL，未连接目标生产服务器。

| 本地验证 | 结果 |
| --- | --- |
| Go全包，api/push/retention/trust/worker与daemon HTTP | 通过；真子进程21秒冷恢复、写后取消、回收/重启、强杀父进程；真实HTTP31秒回复回归 |
| Go vet ./... | 通过 |
| Linux Go worker测试二进制 | 通过；包括Pdeathsig和稳定启动线程 |
| 原点位签名Go/C++回归 | 20项通过（扩展后原模式还增加HOLD重试检查） |
| 最终拓扑签名Go/C++回归 | Windows / Linux各30项通过，含监控可选字段、409 freshness与停启runtime恢复 |
| 活动C++/HTTP回归 | 40 / 31项通过 |
| 168小时留存主/边界回归 | 81 / 45项通过 |
| 后端推送公平/配额回归 | 12 / 5项通过 |
| Viewer三个原文件及新增topology.test.mjs | 22项通过；浏览器确认4点折线/2点弦线切换、双向虚线 |
| P1活动/投递 | 原活动12项、新增5个unittest方法通过，覆盖六类永久错误、可重试HTTP和辅助槽位公平 |
| P1 C++清理/维护兼容 | 10 / 8项通过，新Outbox.error列与现有维护协议兼容 |
| P1语法 | 31个Python源文件通过py_compile |
| 历史本地模型重新覆盖检查 | 3078对，942条完整路径；878条非冲突运营候选边保留折点，64条冲突候选仍排除，2136条未解仍保留。LEGACY_UNVERIFIED，未获得现场验收。 |
| SQLite EXPLAIN QUERY PLAN | 游标走PRIMARY KEY(ns,key)，活动头/计数走state_ref（计数为covering），ack走state_track；见sqlite-query-plans.json |

Windows额外尝试 `go test -race` 因当前未启用cgo无法运行，不把它写成通过；正常全包与跨进程回归通过。本轮未测5分钟真实大图库加载极限，也未测600路/3090容量。

PowerShell从P2目录运行（输出目录每次必须新建，避免覆盖历史证据）：

```powershell
$cmakeExe='C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$ctestExe='C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe'
& $cmakeExe -S cpp -B cpp/build-repair20 -DCMAKE_CXX_STANDARD=20
& $cmakeExe --build cpp/build-repair20 --config Release --parallel 4
& $ctestExe --test-dir cpp/build-repair20 -C Release --output-on-failure
Push-Location go
go test ./... -count=1
go vet ./...
go build -o ../evidence/2026-10-14-repair-topology/face-trajectoryd.exe ./cmd/face-trajectoryd
Pop-Location
node --test tools/point-viewer/direction.test.mjs tools/point-viewer/overlap.test.mjs tools/point-viewer/trace.test.mjs tools/point-viewer/topology.test.mjs
```

端到端脚本使用支持cryptography的Python；本机可用 `C:\Users\function\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`。P1使用 `D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe`（numpy/cv2）；不是把生产模型加载冒充回归。

```powershell
& '<支持cryptography的python.exe>' tools/verify_point_connections.py --worker cpp/build-repair20/Release/face_trajectory_worker.exe --service evidence/2026-10-14-repair-topology/face-trajectoryd.exe --output '<新的隔离输出目录>' --topology
node collection/check_model_coverage.mjs --scene evidence/2026-10-07-existing-data/site-scene.json --navmesh evidence/2026-10-07-model-route/navmesh.bin --geometry-report evidence/2026-10-07-model-route/geometry-report.json --output '<新的报告.json>' --recast-module evidence/2026-10-07-model-route/node_modules/@recast-navigation/core/dist/index.mjs
```

上面覆盖率输入是本机旧模型快照，不代表已核实当前现场；公开GitHub只同步源码、合成示例和摘要，不发布这些场地原始数据。换成真实新模型后需重新校验绑定、方向/门洞、导航连通性和版本。

Linux在P2目录用 `cmake -S cpp -B <新目录> -DCMAKE_CXX_COMPILER=g++-12 -DCMAKE_C_COMPILER=gcc-12 -DCMAKE_CXX_STANDARD=20`及build/ctest；17改为17。本机WSL原先缺工具，已安装GCC12/CMake，仅变更本机开发工具环境。Go内置vendor固定 `golang.org/x/sys v0.30.0`，不需要运行时下载；它用于成熟Windows Job API，许可证随vendor保留。[Go Windows接口](https://pkg.go.dev/golang.org/x/sys/windows)、[Microsoft Job生命周期说明](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

## 踩坑、处理与接手

- 任务书类名/行号与当前版本不同：逐个核对实际入口，已存在的活动上下文与环形调度用回归确认，不重复改写。
- 只修Client还不够：Fair层和HTTP WriteTimeout仍会截断；真实HTTP31秒回归先复现EOF，再修复外层边界，普通算法计时器继续20秒。
- Linux Pdeathsig绑定启动线程寿命：用LockOSThread守护启动线程到Wait，避免线程提前退休造成意外SIGKILL；父进程强杀的真实测试通过。
- Windows裸Start后绑定Job存在写库竞态：先CREATE_SUSPENDED，绑定后恢复；任一错误杀子进程并回收，不能悄悄降级成无Job启动。
- 旧push_outbox行没有ref/track：只改新写入会漏掉旧积压，因此在持久库打开时兼容回填，事件正文与sequence不变。索引查询计划已核实。
- NavMesh坐标轴与scene不同：在导出源头做逆变换，不在前端猜；相机安装点与导航投影分开保存，断裂几何不补造路段。
- 两条路的端点相同不等于几何相同：分组键含整条路径；不确定单条记录也双向显示，不能靠输入/名义排序制造确认方向。
- 自检命令原文不完整：point脚本需要worker/service/output；模型工具需要geometry-report/output和实际navmesh.bin。执行了仓库真实接口，而未创建虚假的published-scene.json/navmesh.json占位数据。
- P1清理测试的adapter是独立 `p1_retention_test`，不能拿worker协议替代；两个程序输入/输出协议不同。

接手顺序：核对GitHub最终提交与SOURCE-MANIFEST摘要 → 构建匹配版本 → 在全新隔离状态复跑 → 后端提供真实活动/绑定/授权/UTC和同版本场景/NavMesh → 独立真实抓拍与人工标签联调 → 才安排生产启动、备份/回退、现场拓扑和容量验收。当前完成的是本地实现与离线/本地集成验证；真实业务联调、生产部署、现场准确率和容量验收均未完成。
