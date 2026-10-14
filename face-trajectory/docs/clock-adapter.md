# 抓拍时间适配器

`ClockAdapter`处理抓拍名义时间、校准来源及误差区间，注册/归一化在C++执行。0.13正常入流统一`monitoring/observe`及`monitoring/gate-event`，按当前活动/device route及`CAMERA_CAPTURE_60S`核验，总误差不超过60000ms；旧raw/point重建绕过入流已拒绝。校准/normalize是global_config授权，活动入流是ingest授权，全部操作依赖有限期外部可信UTC，详见[多活动服务合同](activity-service.md)。

## 注册不可变校准配置

```json
{
  "profile_version": "camera-clock-example-v1",
  "camera_id": "example-camera",
  "verified": true,
  "source_evidence_id": "local-test-fixture-not-production",
  "capture_semantics": "DEVICE_CAPTURE",
  "fixed_utc_offset_minutes": 480,
  "calibrations": [{
    "calibration_id": "example-window-1",
    "source_evidence_id": "local-test-calibration",
    "valid_from_utc_ms": 1791331200000,
    "valid_until_utc_ms": 1791417600000,
    "offset_ms": 48000,
    "uncertainty_ms": 100,
    "time_resolution_ms": 1000
  }]
}
```

以上数值仅解释接口，不能作为服务器校准。`verified` 必须明确给出；实际服务器的单次读时诊断、字段相差八小时等现象，应保存为 `verified: false`，原始时间语义不明时使用 `capture_semantics: "UNKNOWN"`。未校准配置允许空 `calibrations`。

同一 `camera_id + profile_version` 只允许完全相同的 JSON 内容重复注册。更新校准必须采用新版本。每段有效期按**校正后的 UTC** 定义为 `[valid_from_utc_ms, valid_until_utc_ms)`，不允许声明重叠。整个校正误差范围都必须落入有效期；跨边界返回 HOLD。

正 `offset_ms` 表示相机时钟快于真实时钟：

旧`CORRECTED_UTC`模式中，`校正 UTC = 解析得到的设备 UTC − offset_ms`。新`CAMERA_CAPTURE_60S`模式仍用这个校正值选择真实时间有效期，但输出的名义时间保持为解析得到的设备UTC；实际应用偏差`applied_offset_ms=0`。

`uncertainty_ms` 是校准偏差误差上界，必须覆盖有效期内漂移及校准读时误差；不能以一次实时读时外推任意历史。最终对称误差上界为：

`uncertainty_ms + max(time_resolution_ms, 输入文本精度) − 1ms`。

以上是`CORRECTED_UTC`的误差公式。`CAMERA_CAPTURE_60S`另外加上`abs(offset_ms)`，在校正前的设备误差范围上执行60000ms上限。例如相机快48000ms、100ms校准误差、整秒精度，名义时间总误差为49099ms；快60000ms且还有100ms误差则HOLD。采用完整量化宽度而非半宽，兼容截断/四舍五入语义不明的情况。

监控入流的policy/options.max_clock_uncertainty_ms默认60000，可收紧至0～60000；超出范围400，不能提高门槛绕过。调用者上传UTC/零误差字段由ClockAdapter结果覆盖，旧CORRECTED_UTC不能用于监控准入。名义采集时刻比本次可信UTC未来超过60000ms时HOLD，等于边界允许；旧离线补传仍保留capture时间及期限，不以收件时刻替换。底层1000ms默认是历史核心参数，不是当前HTTP入流绕过渠道。

## 归一化

```json
{
  "profile_version": "camera-clock-example-v1",
  "camera_id": "example-camera",
  "time_policy": "CAMERA_CAPTURE_60S",
  "raw_time": "2026-10-07T10:00:48+08:00"
}
```

支持 `YYYY-MM-DDTHH:mm:ss[.SSS]Z`、带明确 `±HH:MM` 的时间，以及配置了 `fixed_utc_offset_minutes` 的无时区时间。日期时间分隔符也可用空格。毫秒部分支持 1–3 位；不静默截断更高精度。无时区时间绝不从字段名称、服务器时区或另一时间字段自动猜测。`-00:00` 表示时区未知，拒绝；`+00:00` 和 `Z` 有效。范围为公历 0001–9999 年，负 Unix 时间正常保留。拒绝非法日期、闰秒、24:00、浮点整数字段、越界整数及算术溢出。

成功返回 `status: "NORMALIZED"`、`captured_at_unix_ms`、`clock_uncertainty_ms`、`time_source: "UTC_CAPTURE"`、`clock_domain: "UTC"` 以及完整 `provenance`。`provenance` 保留原始文本、配置和校准来源、偏差方向、有效期、误差及解析时区来源，并记录`time_policy/applied_offset_ms`；新策略还记录60000ms门槛及通过时的设备总误差界。

独立`/v1/clocks/normalize`兼容旧用途，省略time_policy仍为CORRECTED_UTC，预检应明确CAMERA_CAPTURE_60S。正常监控入流内部强制新策略，不能上传旧策略切回；normalize成功也不能自报结果给原始association/point重建绕过活动、权限或准入。

无法确定时返回 `status: "HOLD"`、`reason_codes` 和 `provenance`，**不输出可被当作合格观测的校正时间字段**。原因包括配置未知/未核验、时间非法、超出校准有效期、误差跨有效期边界、偏差跳变造成校准选择歧义、算术溢出。时钟回拨可能使两个不重叠 UTC 校准段都解释同一原始时间，这种情况下也保留 HOLD。

## 经验与边界

2026-10-10用户确认的规则已在0.11.0本地代码实现：使用摄像头抓拍名义时间，每台设备相对可信共同参考的偏差、测量/漂移及量化误差总界不得超过60000ms。不要把真实`offset_ms`伪填为0；新策略会保留偏差证据且不应用校正。超限（包括半小时）或无法核实误差界的记录HOLD，不输出合格时间字段。两台相机各±60秒时相对误差最坏可达120秒，区间重叠或仅接触仍不能确定先后；保留所有可能最后摄像头。图纸中的线路和设备标记没有提高时间、身份或拓扑的验证状态。

本地代码完成不等于相机校准完成。真实profile仍须有可信来源、有效期和测量界；用户此前提供的设备清单及旧时差记录不能自动改为verified。旧校正时间快照保留原版本；升级后用最新`expected_version`和完整权威记录集重放，不能只改一个时间字段或默默覆盖历史。如果记录由内部关联模块管理，还必须与该模块的当前来源记录一致，否则保持SOURCE_CONFLICT并先核实源资料。

- 服务器时钟、相机时钟与库内时间字段之间的差值只提供诊断线索。抓拍字段语义和历史校准范围需独立证据。
- 明确时区只解决表示方式；相机快慢仍由偏差校准处理，不做重复减八小时。
- 不使用宿主机器的 `mktime` 或系统时区，避免 Windows/Unix 差异与负时间问题。
- 本模块只认证时间条件，不建立人员关联。`CONFIRMED` 身份仍必须来自已有可靠关联证据。

## 本地验证与交接

0.11.0新产物位于`cpp/build-clock-policy/Release`。75项真实本机Go→C++接口检查、51项旧时钟检查、18项可达检查（含1248组独立枚举）通过；详见[本轮报告](../evidence/2026-10-10-clock-policy/REVIEW.md)。没有替换运行中服务或真实状态库。

2026-10-07 使用实际 `cpp/build-point/Release/face_trajectory_worker.exe` 运行 51 项合成时钟契约检查通过。日期转换期望值来自 Python 标准库 `datetime`，并覆盖偏差正负方向、无时区拒绝/显式配置、秒级误差、非法日期、负纪元时间、校准边界和回拨歧义、不可变版本、SQLite 重启和整数溢出。测试不连接服务器。

```powershell
python evidence/2026-10-07-point-trajectory/clock_tests.py --worker cpp/build-point/Release/face_trajectory_worker.exe --output evidence/2026-10-07-point-trajectory/clock-test-results.json
```

报告保留每次 worker 请求和响应。结果只证明本地时间适配契约；合成 `verified: true` 配置不能用于真实摄像头。已有生产诊断必须保持未核验，直到提供抓拍字段语义、有效期内偏差和误差上界的证据。
