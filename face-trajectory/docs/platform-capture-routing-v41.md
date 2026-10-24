# v4.1 算法侧动态采集路由

2026-10-23。算法源码修复后，已按用户要求在252部署并常驻；未修改后端代码、数据库、配置、接口或服务。P1现已运行，但物理视频源目录仍为空，详见[252联调部署记录](../evidence/2026-10-23-server252-v41-deploy/README.md)。下方只读核查内容记录部署前状态。

## 修复内容

原P1启动时从cameras_file为每个摄像头固定一个activity_context。后端随后同步活动/摄像头只更新P2，不能更新P1；同摄像头无法服务多个活动。

新增`face_engine.platform_runtime.PlatformMonitoringRuntime`。P1定期以SQLite `mode=ro`和`query_only`读取**算法自己的P2状态库**内已经提交的v4活动绑定，不读取后端数据库。名称采用该活动绑定中的后台下发名称。每个物理UUID保留一个实时解码器；Hikvision按实际连接和通道共享订阅，向所有明确绑定且已启用、未结束、处于活动窗口内的活动分别生成输入。每条输入携带各自的活动ID和版本，由P2在该活动的图库中识别。

添加或停用一个活动不会重启仍供其他活动使用的相机。同一Hikvision物理连接使用采集目录中完整、明确的通道表；未绑定通道由算法丢弃，不入队、不识别，活动开关不重建其他通道的订阅。删除最后一个有效路由后停止对应算法读流/订阅；不向摄像头或后端发送配置命令。绑定新增后不会使用新增前缓存的旧帧；已撤销路由的排队图像拒绝识别。原采集source_id、time_record、抓拍时间和误差保留，到P2按已修复的来源校时规则验证；不能用接收时间冒充抓拍时间。

默认2秒轮询，运维可设置0.1～30秒；P2仍在处理观察时重新检查当前绑定和活动开关。快照或采集目录读取失败时撤销当前路由、尝试停止算法读流并报告`BINDING_SOURCE_UNAVAILABLE`；停止超时的reader保留引用、拒绝路由并在后续轮询重试清理，未停稳前不重建或释放维护租约；找不到UUID的视频来源时报告`MISSING_VIDEO_SOURCE`和缺失UUID。`BOUND`只表示路由与输入配置已装载，不代表视频、校时、识别或后端投递已验收。

多个活动分别执行检测/提特征和活动内识别；当前没有跨活动复用推理特征或600路容量验收。

## 算法运维配置

示例：`face-recognition-engine/examples/monitoring-platform-v41.example.json`、`monitoring-platform-cameras.example.json`。

在P1配置中增加：

```json
{
  "platform_bindings": {
    "state_file": "/home/haha/face-trajectory/state/face-trajectory.sqlite",
    "poll_seconds": 2
  },
  "cameras_file": "monitoring-platform-cameras.json"
}
```

`platform_bindings.state_file`为本机P2算法状态库；P1自己的state_file必须使用另一个文件。路径可相对运行配置解析。P1需有只读权限。cameras_file改为物理采集目录，以后台UUID为camera_id，包含原采集source_id、已有视频环境变量引用或Hikvision通道连接；**不包含activity_context**。无须人工为每个活动复制摄像头配置。连接口令只在运维环境中，不能放进后端同步请求、日志或本说明。新增的配置均属算法侧运维，后端的UUID/名称/人员请求字段不变。

示例中的UUID、来源和校时版本均为占位符，不能直接用于实机。GStreamer仅在对应缓冲区确实提供NTP参考时间且有独立有效校时档案时形成合格证据；普通FFmpeg接收时间仍是UNKNOWN，不会为了出结果补造可信UTC。

## 252部署前只读核查结果

通过已核对主机密钥的SSH连接确认主机为`haha-2288H-V6`，具有`192.168.1.252`。轨迹接口和worker正在运行，目录`/home/haha/face-trajectory`，状态库为上述路径。读取算法活动摘要显示目前为旧合同测试活动，尚无v4活动。

没有发现正在运行的本项目`face_engine.monitoring_service`/`monitoring_service.py`进程；默认`/home/haha/face-recognition-engine`不存在。在/home、/opt、/mnt/data的六层文件名搜索中也未发现本项目采集入口、相机目录及已知配置名称；搜索排除了后端服务目录和依赖/缓存，不能据此断言机器没有其他厂商采集系统。服务器上的其他4G、SDK媒体进程不能直接等同于本项目P1。

当前仍缺**可以按后端UUID定位实际视频/抓拍来源的现成采集目录或只读接口**。尚未核实真实模型安装、设备抓拍校时和实际帧。用户已明确不动后端，本轮没有通过修改后台服务、数据库或配置来补齐此缺口。代码已完成本地动态路由修复，第一项的252实机接通尚未完成。

## 验证

`tests/test_platform_runtime.py`共15项，包括真实C++worker生成绑定后由P1读取、同一物理摄像头服务两个活动、每个活动两人的人员库独立匹配、同帧四条对应上报的身份/112×112人脸裁剪/时间/UUID、停用隔离、Hikvision订阅复用、旧帧隔离、只读不改库、快照失败撤销、视频源缺失和结束时间边界。

真实核心测试中的检测器和向量为合成模型；未访问真实摄像头，不代表真实识别准确率或现场验收。既有来源校时19项和多人图像14项同时回归，完整证据见`evidence/2026-10-23-platform-capture-routing/README.md`。
