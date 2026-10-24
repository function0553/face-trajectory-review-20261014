# 人员轨迹v4.1：算法侧实现、验证与部署

2026-10-23。依据用户提供的《人员轨迹三方业务与接口设计方案_v4.1.md》，替代本次尚未完成的v4.0实现。用户已要求修改及部署算法；原文“仅交付设计”的范围说明不替代用户授权。新版已部署到252并常驻，按用户选择使用512维研究模型、CPU联调；未修改平台Java、管理后台或后端数据库。视频源和出站推送仍待接入，详见[252部署记录](../evidence/2026-10-23-server252-v41-deploy/README.md)。

## 已实现的对接

- `POST /v1/activities/sync`：接收camelCase活动字段，可同时带`cameras`、`keyPersons`，与单独接口共享摄像头绑定、照片建库逻辑。可先同步活动和摄像头，再单独导入人员。
- `POST /v1/activities/bindings`：Body为`{"activityId":"ACT-001","cameras":[{"deviceId":"后端UUID","deviceName":"入口1"}]}`。
- `POST /v1/monitoring/persons`：Body为`{"activityId":"ACT-001","keyPersons":[{"personId":"P1","personName":"人员姓名","personIdNum":"完整身份证号","personPhoto":"https://平台/人员.jpg","seat":null}]}`。
- 上述路径固定，不接受`?activityId=...`。平台不传旧CAS、特征模板、额外source_id或设备映射。UUID为不透明字符串，原样返回。
- 照片由算法下载、解码、执行同一P1检测/质量检查/512D特征提取，成功提交图库后才返回`recognitionReady=true`。无脸、多脸、质量不合格、URL不可访问分别失败，不阻断其他合格人员。
- 同一活动下所有已绑定且已接入的摄像头共用该活动重点人员图库。摄像头接口成功仅证明绑定完成，不等于实际视频接入成功。
- `POST /v1/activities/monitoring`的当前实现为`{"activityId":"ACT-001","enabled":true}`；初始关闭。禁用后停止新识别事件，已入队事件继续补发。

完整机器可读合同见`../openapi-platform-v4.1.yaml`。旧snake_case接口继续保留原合同，禁止在同一请求里混用两版字段。

## 上报合同

接收路径保持`POST /api-security/algorithm/person-track/events`，推送专用Bearer放请求头。

```json
{
  "event_id": "pt:<稳定事件摘要>",
  "event_type": "UPSERT",
  "revision": 1,
  "activity_id": "ACT-001",
  "device_id": "后端UUID",
  "observation_id": "本次抓拍单脸ID",
  "person_id": "P1",
  "person_name": "人员姓名",
  "person_id_num": "完整身份证号",
  "camera_id": "后端UUID",
  "camera_name": "入口1",
  "captured_at_unix_ms": 1792751400000,
  "clock_uncertainty_ms": 1,
  "capture_photo_url": "<实际抓拍JPEG的纯Base64>"
}
```

`person_name`和`person_id_num`是用户明确要求、但v4.1原始上报样例遗漏的两项扩展，后端DTO必须对应接收。`device_id`和`camera_id`均等于后端deviceId，名称采用后台下发值，不使用设备通道名称覆盖。没有source_id上报字段。

`capture_photo_url`保留文档旧字段名，但内容为纯Base64。按用户明确要求，从原始抓拍画面按每张脸的检测框分别裁剪并编码JPEG；同帧多脸使用各自的人脸图和观察ID，不共享整帧图，也不使用登记照片代替抓拍。用于裁剪的检测框与提取该条特征的检测结果相同，空或无效边界直接失败，不回退整帧。

`keyPersons`是一份多人名单，每个人的personId、personName、personIdNum、personPhoto作为一条完整档案入库，所有本活动已绑定并接入的摄像头共同比对这份名单。命中后按该personId取回原档案中的姓名和身份证号，附上本次该人的人脸抓拍、采集时间和摄像头UUID。两名重点人员同帧出现会分别上报两条，5秒合并按人员分别执行，不合并不同人员。

仅推送已确认的PRIORITY人员。保持同活动同人员跨摄像头5000ms内（含边界）合并；以先处理并入队的有效抓拍为代表，不改其实际UUID、照片或采集时间。原始识别记录与去重后的上报条数可以不同。

修订键为`activity_id + device_id + observation_id`，重试事件ID、修订号和完整正文不变。身份更正增加修订并更新姓名/身份证快照，撤回三个人员字段均为null。保持既有纠正后补发代表抓拍的规则。后端只有HTTP200、整数code=200且event_id一致才确认交付。

## 72小时与活动时间

用户指定算法侧重点人员保存3天。当前从逐人首次成功建库开始计72小时；相同请求重试、资料更新不会延长原截止时间。到期删除登记资料、档案照URL、登记修订和该人员识别模板，并清除非待发事件中的姓名/身份证快照。登记照片下载后仅在内存中处理，不另存磁盘照片。

未确认交付的outbox保留完整人员快照与抓拍Base64，直至后端成功回执；此数据独立于已到期的重点人员图库。同一已过期内容的原样重试返回失败，不悄悄续期。新增资料/照片可形成新的登记；具体业务续期方式如需调整，应同步修改协议。

每次处理请求前检查到期；服务另有一分钟周期检查，不依赖可选的168小时历史清理开关。逻辑清理使用可信UTC；可信时间不可用时不宣称清理或识别成功。

活动时间采用带时区ISO8601，状态字符串0/1/2，窗口为开始包含、结束不包含。当前时间和抓拍时间均须在窗口内，监控已启用且状态不为2才接纳新事件。既有待发事件不受活动停止、改名或人员到期影响。采集时间保留原值，时间误差沿用既有校准与最高60000ms准入，不伪造为0。

## 对设计建议采用的当前默认值

以下是本次可运行实现的默认行为，不代表平台已完成相同改造或三方已经确认P1-P12全部建议：

| 项目 | 当前实现 |
| --- | --- |
| 摄像头数组 | 完整集合替换；相同集合不同顺序不重复绑定；禁止用空数组清除已有非空集合 |
| 人员数组 | 按活动、人员幂等，省略某人及空数组不删除图库；姓名、身份证必填字符串；seat可空 |
| 人数与耗时 | 每次最多500人，一个照片处理批次最多5分钟；同一服务只并行处理一批，忙时503原请求重试 |
| 照片上限 | 登记图、抓拍图解码后不超过5MiB；JPEG/PNG；抓拍入口不超过1600万像素；HTTP正文小于8MiB并预留内部封装空间 |
| 响应 | code/message/activityId/data；逐人status、recognitionReady、message和成功时expiresAtUnixMs |
| 监控开关 | 初始关闭；enabled为人工开关，活动窗口同时生效；不停止守护进程 |
| 视频接入 | 算法侧支持从自身P2状态库动态装载绑定、物理UUID共享读流及逐活动分发；252真实UUID对应的视频源目录尚未找到，未实机验收 |

参数来源、Base64上下限、3天起算点和默认监控开关应随本说明交后端核对，不能将本地测试当作后端接受确认。抓拍图已按用户要求固定为对应人脸裁剪图。

## 算法部署准备

在原有算法服务、可信UTC、鉴权、采集配置基础上，配置算法侧环境变量：

```text
FACE_TRAJECTORY_P1_PYTHON=<已安装现有P1依赖的Python绝对路径>
FACE_TRAJECTORY_PHOTO_IMPORT_SCRIPT=<face-trajectory/tools/platform_photo_import.py绝对路径>
FACE_TRAJECTORY_P1_MODEL_CONFIG=<与实时抓拍相同模型的配置文件绝对路径>
FACE_TRAJECTORY_PUSH_AUTHORIZATION=Bearer <后端推送专用令牌>
```

上述为算法运维配置，后端仍只需算法地址与授权令牌，不要求后台增加模型或模板配置。Python进程通过内部stdin/stdout传递模板，后端看不到、也不负责生成embedding。真实模型或导入器未就绪返回503，不能用合成模板冒充正式登记成功。

更新时需同时使用本次Go服务、C++worker及P1 Python源码。既有状态库中的旧版待发消息不会被转换；切换到v4.1接收端之前应核对旧队列，安排明确的迁移/回退步骤。本次252部署在备份后保留原算法库，切换前后待发队列为空、活动数量一致；后端库未改。

## 本地验证范围

2026-10-23补充：修复原采集来源在UUID路由改写后重复校时被误拒绝的问题，入口完成原来源校验后，核心复用首次可信归一化；原时间记录、后端字段均不变。新增19项回归及原35/26/14项兼容检查通过，见`../evidence/2026-10-23-source-clock-fix/README.md`。随后补齐算法侧动态绑定/共享摄像头路由，15项验证通过；252尚缺实际视频源接入，见[动态采集路由与实机缺口](platform-capture-routing-v41.md)。两项现已随252联调版本部署，视频源仍待接入。

证据位于`../evidence/2026-10-23-platform-v4/`。使用本地真实C++worker、隔离SQLite、签名合成UTC、人造特征和loopback HTTP接收器验证接口与持久化逻辑；URL下载、图像解码、JPEG/Base64编码使用实际实现。该阶段未作252运行版本比对；后续已完成Linux构建、部署哈希及服务恢复检查，见本次部署记录。真实人员识别准确率、摄像头现场、平台TDengine入库和容量验收仍未完成。

补充`tools/verify_multi_person_faces.py`：两名重点人员分别通过loopback照片URL建库，同帧两张脸分别经过真实裁剪、对齐、实际C++匹配及入队，逐条核对人员ID、姓名、身份证号、人脸图、原时间和摄像头UUID。14项通过；检测器和特征提取器为明确标记的合成测试替身，不属于真实人员识别准确率验收。见`multi-person-face-crops/summary.json`。
