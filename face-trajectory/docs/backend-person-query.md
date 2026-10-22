# 后端按人查询人员信息、抓拍照片及时间线

2026-10-22：三方当前保存轨迹方式已改为[人员轨迹v1主动推送](person-track-platform-v1.md)。本文保留2026-10-21按次查询合同，用于联调核对和既有调用方；文内“不配置推送”的部署说明仅适用于旧查询模式。新模式不靠轮询保存轨迹。

## 查询接口

`POST /v1/monitoring/timeline/query`，`Content-Type: application/json`。请求头使用业务后端持有的 `Authorization: Bearer <query-token>`，需具有对应活动的 `query` 权限。当前局域网测试地址及有效凭证见本次测试目录的 `backend-query.http`；凭证不写入文档。

```json
{
  "activity_id": "ACTIVITY-ID",
  "config_version": 1,
  "binding_version": 1,
  "monitoring_version": 1,
  "person_id": "DAMAI-PERSON-ID",
  "from_unix_ms": 1792576800000,
  "to_unix_ms": 1792580400000,
  "limit": 500
}
```

用大麦上传的稳定业务人员 ID 查询；姓名用于显示，不作唯一键。三个版本使用后端当前活动配置值。时间区间为起点包含、终点不包含，示例为北京时间 2026-10-21 18:00–19:00。

主要业务字段示例（完整回复另含活动作用域、版本及授权上下文；以下示例照片域名不提供真实图片）：

```json
{
  "ok": true,
  "person_id": "DAMAI-PERSON-ID",
  "category": "PRIORITY",
  "is_priority": true,
  "person_revision": 2,
  "association_revision": 7,
  "person": {
    "person_id": "DAMAI-PERSON-ID",
    "person_name": "人员姓名",
    "profile_photo_url": "https://photos.example.test/profiles/person.jpg",
    "category": "PRIORITY",
    "is_priority": true,
    "person_revision": 2
  },
  "timeline": [
    {
      "source_id": "capture-source",
      "observation_id": "capture-001",
      "camera_id": "camera-001",
      "camera_name": "10136看台A1下入口1",
      "camera_name_source": "CAPTURE_EVENT",
      "captured_at_unix_ms": 1792578600000,
      "clock_uncertainty_ms": 0,
      "capture_photo_url": "https://photos.example.test/captures/capture-001.jpg"
    }
  ],
  "count": 1,
  "has_more": false,
  "next_after": null
}
```

`person` 是当前上传的人员资料；`profile_photo_url` 是档案照。每一条 `timeline[].capture_photo_url` 是该条摄像头抓拍对应的照片，可为全帧或该人脸裁剪照片，不用档案照代替抓拍照。缺姓名、缺照片或历史记录未提供照片时相应字段为 `null`。不返回特征向量、模板或整个抓拍输入载荷。

`camera_name` 是场地点位名称，不是设备型号。按抓拍 UTC 毫秒排序，保留时钟误差；相同摄像头的不同抓拍分别返回。身份纠正后，按当前人员归属查询，照片仍跟随原抓拍记录。仅已确认且时间合格的记录进入时间线，HOLD 不混入。

默认最多 500 条，`limit` 为 1–1000。超过限制时 `has_more: true`：后端下一次主动请求带 `after: <next_after>`、`expected_association_revision: <上一页 association_revision>`；其他筛选条件不变。算法不会自行发送下一页。迟到/纠正导致修订变化返回 409，重新从第一页请求。无记录返回 `timeline: []`。保留窗口为滚动 168 小时，截断历史窗口时带 `history_pruned` 和 `effective_from_unix_ms`。

## 人员资料与抓拍照片如何进入算法

人员仍通过 `POST /v1/monitoring/persons` 上传，在原来的活动、CAS、类别和模板字段之外增加：

```json
{
  "person_id": "DAMAI-PERSON-ID",
  "person_name": "人员姓名",
  "profile_photo_url": "https://photos.example.test/profiles/person.jpg",
  "category": "PRIORITY",
  "revision": 2,
  "expected_revision": 1,
  "evidence_id": "uploaded-person-record"
}
```

该片段须合并实际活动作用域；特征模板沿用既有 P1 提取与登记方式。资料更新省略 `person_name/profile_photo_url` 会保留已有值，显式 `null` 清空。同一修订重试不能换内容。重点标记只由上传的 `category` 决定。

抓拍仍通过 `POST /v1/monitoring/observe` 入流，在 `observation` 内加入 `capture_photo_url`，其余人脸特征、活动、摄像头和可信采集时间合同不变。同一活动/source/observation 的 URL 也是不可变内容，重试换 URL 返回 409，防止旧事件被悄悄换图。算法仅保存 URL，不下载图片。

P1 的采集元数据支持帧级 `capture_photo_url`，也支持 `faces["检测序号"].capture_photo_url`，单个人脸的 URL 优先（显式 null 也覆盖帧 URL）。人员登记清单支持 `person_name/profile_photo_url`。图片上传至存储并取得 URL 由采集方或业务后端完成，本次未连接对象存储或大麦生产系统。

URL 必须是完整 HTTP(S) 地址，主机不能为空，限 4096 字节，不能包含明文用户名密码、空白、控制字符或反斜杠。图片地址必须能被业务后端访问；临时签名 URL 的有效期需要覆盖实际查询使用时间，否则由图片存储方提供刷新机制。算法不会把本机文件路径变成可访问 URL，也不会续签 URL。姓名限 1024 UTF-8 字节，不允许控制字符。

## 部署与联调

保留已有活动模式值 `UE_CAMERA_EVENTS`，这里只是兼容的内部配置名，无需接入 UE。配置带位置名称的设备绑定并显式启用活动监控。启动时**不配置 `-push-url` 和 `-push-results`**；业务后端只调用上述查询接口，无需使用订阅端点。通用推送能力保留给其他既有部署，此接入方式不开启。

401 表示凭证无效/过期，403 表示缺少该活动查询权限，400 为参数或 URL 格式错误，409 为版本、人员修订或不可变抓拍冲突，503 为可信时间/运行状态不可用。后端按活动、source、observation 保存抓拍记录，按版本处理身份纠正；不能仅以姓名或摄像头名称去重。

验证：`tools/verify_backend_person_photos.py` 在真实 C++ worker 和隔离 SQLite 中覆盖人员资料、每次照片关联、空值、去重、非法 URL、身份纠正、无推送及保留清理。HTTP 与 Wi-Fi 模拟仅使用合成人员、特征和示意照片，不能作为真实识别准确率或生产接通的证明。
