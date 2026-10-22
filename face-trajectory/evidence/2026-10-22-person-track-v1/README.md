# 2026-10-22 人员轨迹协议v1.0改造验证

实现与部署入口：[人员轨迹平台接入](../../docs/person-track-platform-v1.md)。接口为OpenAPI 0.14.0；新活动模式PERSON_TRACK_EVENTS。新Go服务`face-trajectoryd.exe`与`../../cpp/build-ue-camera-events/face_trajectory_worker.exe`配套使用。没有部署、切换或修改生产。

## 结果

| 验证 | 证据 | 结果 |
| --- | --- | --- |
| 新合同真实C++ worker、隔离SQLite | [green-final/summary.json](green-final/summary.json) | 26项通过 |
| 实际签名HTTP Go→C++→loopback平台 | [http-verified/summary.json](http-verified/summary.json)、[收到的原始正文](http-verified/received.json) | 通过：7次请求，含503和错误event_id后原样重试，5个唯一事件 |
| Go全量测试 | [go-test.txt](go-test.txt) | 6个包均通过；go vet ./...退出0 |
| C++ CTest | [ctest.txt](ctest.txt) | 2个目标通过 |
| 原摄像头事件、查询、照片、活动隔离、保留清理 | legacy-camera-final、legacy-timeline-final、legacy-photos-final、legacy-activity-final、legacy-retention-final下summary.json | 21+21+26+40+81=189项通过 |
| 原推送格式HTTP兼容 | [legacy-http-final/summary.json](legacy-http-final/summary.json) | 3条旧格式摄像头事件交付通过 |
| OpenAPI | openapi.yaml | YAML无重复键，603个内部引用全部解析成功 |
| 实际CPU demo提取器拒绝无脸登记 | [sample-extraction-negative.log](sample-extraction-negative.log) | 拒绝无脸图，不生成登记输出 |

新合同覆盖：完整十二字段及null、同步模板就绪、删除模板后历史重试不能误报、名单外与HOLD过滤、相同键不重复、同画面同毫秒不同单脸、UPSERT身份纠正、RETRACT及恢复确认、修订跨重启、满队列整笔回滚、超168小时清理不改写/删除待交付事件、历史压缩后存活抓拍首次确认。

红阶段保留在red、red-compaction、red-readiness。green-final是最后C++实现的绿色结果。独立代码审查发现的两项P2（回执压缩丢normalization、已删除模板的旧登记误报ready）均已复现、修复并由审查复核。早期http目录的模板重复失败来自测试未重放删除模板步骤，已修正测试；http-verified为最终结果。

## 复跑

所有输出目录必须为未存在的新目录。示例从face-trajectory目录执行：

```powershell
python tools/verify_person_track_push.py --worker cpp/build-ue-camera-events/face_trajectory_worker.exe --output evidence/new-person-track-run
python tools/verify_ue_camera_http.py --worker cpp/build-ue-camera-events/face_trajectory_worker.exe --service evidence/2026-10-22-person-track-v1/face-trajectoryd.exe --fixtures evidence/new-person-track-run/calls.json --output evidence/new-person-track-http --person-track
```

HTTP验证依赖Python cryptography，自动生成仅用于合成联调的签名密钥及短期时钟证明。推送的Bearer、人员、特征、时间及URL全为合成测试值；密钥不能用于生产。工具会关闭本次服务与接收端。

Go使用本工作区`outputs/ue-camera-events-build-tools/go/bin/go.exe`执行test/build/vet；C++使用同目录下python/cmake/data/bin/cmake.exe和w64devkit静态工具链构建。MinGW全量构建存在原文件NOMINMAX宏重复定义警告，不影响本轮编译及测试。

本机P1历史`.venv/Scripts/python.exe`保留了旧机器解释器路径，不能直接启动。本轮仅为验证提取样例，以当前Python3.11结合原P1 site-packages运行CPU demo权重，不修改旧环境。正式执行样例应使用已安装P1依赖和正式模型的有效Python解释器。

## 范围

真实本地程序、数据库事务及HTTP投递验证，不代表真实人员识别准确率、全场容量、照片存储可达性或平台TDengine入库已验收。正式联调仍需平台地址/专用凭证、活动及设备映射、有效照片URL、现场模型与阈值。平台存储/驾驶舱接口由对应责任方开发，算法原查询窗口继续为168小时。
