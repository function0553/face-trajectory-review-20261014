# 多活动服务与168小时保留合同（0.13.0）

2026-10-10。C++17 管理活动状态、身份关联、监控与清理；Go 管理签名授权、HTTP、按活动轮转请求、订阅与周期任务。P1 Python 只传递作用域字段并兼容隔离、墓碑和维护握手。当前验证结果及四层交付状态以两个项目现有 `HANDOFF.md` 为准。本文件是可执行接入合同，不代表生产已部署。

## 身份、活动与监控

`activity_id` 是 `security_activity.id`；唯一 `activity_num` 经权威同步映射到该 ID；`venue_id` 是场地。创建活动默认 `enabled=false`。用户选择、创建活动、监控启停三个操作独立：关页、退出、切换、关闭订阅均不改变监控；计划结束时间不自动停止。算法不扫描处理全部历史活动，不选择“最新活动”，不把共享场地设备无条件分发到所有活动。

底层 State 的物理命名空间为 JSON `["activity", activity_id, namespace]`，图库、人员类别、匿名身份、同人关联、会话、轨迹版本及热索引均在其中；业务 `person_id/target_id` 保持原值。登记图库首版也是每活动同步；后端想复用同一人员照片时，须将相同权威登记模板显式同步到各有权活动。登记模板排除于轨迹清理，抓拍衍生的匿名/自动模板按轨迹期限清理。

活动接口使用 CAS：

| HTTP | 动作授权 | 请求字段 |
| --- | --- | --- |
| `POST /v1/activities/sync` | `config` | `activity_id,activity_num,venue_id,config_version,expected_config_version`，可选 `configuration` |
| `POST /v1/activities/bindings` | `config` | `activity_id,config_version,binding_version,expected_binding_version,bindings[]` |
| `POST /v1/activities/monitoring` | `monitor` | `activity_id,config_version,binding_version,monitoring_version,expected_monitoring_version,enabled` |
| `POST /v1/activities/get` | `query` | `activity_id` |

新版本大于 expected；首次 expected=0；已有配置用其当前值。相同修订与完全相同内容安全重试；内容变更或旧 CAS 返回 409。已建立的 `activity_num/venue_id` 不能用配置更新换成其他活动。get 返回身份、当前三版本和 enabled，完整配置与绑定资料仍由后端持有。

完整无凭据请求示例见[activity-service.example.json](../collection/activity-service.example.json)，包括创建停止活动、完整设备范围、停止状态登记分类、明确启动、查询/订阅、绑定修订、明确停止及dry_run。示例claims/proof只是待签名结构，固定时间和placeholder不能作为真实授权或可信UTC；现场需签发当次有限期内容。

每条 binding 包含 `source_id,device_id,kind=CAMERA|GATE,route_rule_id`。bindings 提交完整启用范围；同一活动内重复 `(source_id,device_id,kind)` 拒绝。共享摄像头可以由不同活动分别明确绑定，但每个事件必须有当前活动版本及匹配 route。绑定/配置变更后旧事件、停止后新到事件均 HOLD，不影响其他活动；已有原交易的相同重试只回原回执，不产生新副作用。

## 授权与可信当前UTC

除 `/health` 外，每个接口要求 `Authorization: Bearer <签名值>`。值为 `base64url(JSON原始字节).base64url(Ed25519签名)`，不是 JWT；签名覆盖解码后的 JSON 原始字节。服务只装公钥，不装后端私钥。公钥文件：`{"keys":{"key-id":"<32字节Ed25519公钥的base64>"}}`。issuer 和 key_id 必须匹配受信配置。

授权正文含 `issuer,key_id,subject,session,context_id,context_generation,authz_revision,not_before_unix_ms,expires_unix_ms,grants,global_actions`。grant 是 `{"activity_id":"...","actions":["query","subscribe","config","monitor","ingest","identity"]}` 的明确子集；全局动作是 `global_config,push_status,retention` 的明确子集。有效期最多24小时，并按可信UTC误差区间核验。不能用前端活动 ID、自报 client_context、票号或 P1 回调机密替代用户授权。HTTP 禁止输入 `_` 开头字段及 `client_context`。

相机准入仍使用不可变 ClockAdapter profile、已核验 `DEVICE_CAPTURE` 和 `CAMERA_CAPTURE_60S`。名义采集时间保持时区明确；`abs(offset)+测量/漂移/量化误差<=60000ms` 才准入。名义capture比本次可信UTC未来超过60000ms时HOLD，等于60000ms允许；离线补传的旧capture不因当前收件时间被改为now，仍按原期限。未知、未经核验或超限HOLD，从首次可信隔离最多168小时；准入事件的误差区间重叠保留顺序歧义。

保留与授权需要另一条可信当前UTC链：外部时钟权威读 `/health` 的 `clock.challenge`，核验自己的参考来源、同步状态与误差，签名 `ClockAuthorityProof`，原子替换 `-clock-proof-file`。正文含 `issuer,key_id,challenge,now_unix_ms,uncertainty_ms,valid_for_ms,source_verified:true,source,evidence_id`。挑战有效30秒，proof最长120秒；签名误差加挑战往返年龄不得超过1000ms，实际必须快速签发。接收成功旋转挑战，UTC按进程单调时间推进，不允许水位倒退。Go进程冷启动需新挑战证明；同一Go进程中的C++子进程恢复仅可沿用仍有效的Go单调UTC租约，过期仍需刷新；缺失、过期、来源不合格、旧 challenge 均关闭相关服务操作。系统墙钟可读取、NTP开关或历史核验快照不能替代运行证明。测试脚本的 `SYNTHETIC_TEST` 仅验证合同。

## 查询、通知与客户端切换

所有人员、查询、归属修正接口都显式提供 `activity_id,config_version,binding_version,monitoring_version`；脸/闸机入流另需 `route_rule_id`。请求可带 activity_num，但必须与权威映射一致。归属修正引用脸另需其 `source_id`。每个活动接口分别验证动作，存在活动不等于有权限。原始 `association_observe/trajectory_rebuild/raw_trajectory/build_visits/point_trajectory_rebuild` 入流已拒绝，不能自报UTC绕过监控准入；现有查询、归属修正及基于核心权威集合的 association_rebuild 继续使用。

回应带 `activity_id,activity_num,venue_id` 和三版本，Go 附签名授权的 `client_context` 五字段。客户端保存当前活动/版本/context_id/context_generation/authz_revision；请求发出后即使用户已切换，返回仍属原上下文。客户端逐项匹配，丢弃旧活动、旧选择上下文或旧版本的结果，不能只看人员 ID。

| HTTP | 请求 | 行为 |
| --- | --- | --- |
| `POST /v1/subscriptions/open` | 活动ID及三版本 | 验证 `subscribe` 权限；从当前已提交序号开始，不补发历史积压 |
| `POST /v1/subscriptions/poll` | `subscription_id,generation`，可选一致的 activity_id | 每次最多100个真实已提交算法结果，推进该订阅游标 |
| `POST /v1/subscriptions/close` | `subscription_id,generation` | 只关闭当前用户订阅 |

订阅绑定 subject/session/context 全字段和授权期限，其他用户或新的选择上下文不能取旧订阅。回包含 subscription_id/generation/after_sequence，通知另含 subscription_generation、活动版本及 client_context。版本改变返回409，过期/关闭/进程重启丢失返回410，重新 open。订阅为内存用户访问状态；持久算法结果流、活动启停与监控状态不随之消失。后端主动结果推送是独立持久队列，每活动最多256、全局10000；活动间轮转，各活动内FIFO，失败活动单独退避不阻塞其它活动。原内容重试不重复占额，真正入队配额拒绝和算法结果整笔回滚。见[推送合同](backend-push.md)。

## 幂等与168小时边界

事件键为 `(activity_id,source_id,event_id)`，脸用 observation_id 作为 event_id；帧与跟踪会话也加入 source 作用域。不同活动、不同来源使用相同 event_id 不冲突。相同键相同全部内容只返原交易回执；同键变内容，包括活动版本、route、时间材料、reconsider、闸机后补 binding，返回409。不得改键重放同一物理事件逃避冲突/期限；需要原事件补证修订时，应先约定独立有版本的后台接口，当前没有自动改写原键的入口。

`cutoff=可信UTCnow-604800000ms`，合格相机名义采集时间等于 cutoff 仍保留，早于则过期。原始 ClockAdapter 误差留存用于判断，不因接收、投递、重试、重放、活动结束或用户切换重计时。P2 新未知/超限 HOLD 从首次可信隔离接收入库起最多168小时，重复/restart不重置；时间补齐也不能延长先前期限。缺可信首次隔离依据的旧/未归属数据及 P1 无合格采集时间数据采用立即到期，禁止迁移时凭空给新窗口。

清理覆盖原入流、当前/历史脸与闸机、所有轨迹历史版本、refs-v1/v2共享对象、关联索引、自动模板、投影缓存、订阅结果、回执、推送/恢复来源，以及明确配置的 P1 captures/deliveries/旧frames/outbox。历史版本会被修剪超窗证据及派生路线；空的旧版本删除，最新空头仍保留版本号以防倒退。因此“指定历史版本”不意味着永远能还原过期内容。共享对象先以全部仍存在根做标记，再清理不可达对象；仍在窗口内的其他活动数据保留。依赖旧历史的仍活回执会压成 `detail_query_required=true`，当前详情重新查询。仍活闸机事件按自己的采集期限保留效果；其已确认绑定脸到期时，抹去原脸ID/绑定来源详情并保留当次核心确认的target及不可逆引用摘要，避免合法CLOSED/ENTRY效果因依赖清理丢失。脸仍活时归属修正继续动态跟随当前核心，清理冻结不允许调用者自报身份。

登记图库、活动主表、必要配置/绑定版本、启停生命周期不随轨迹清理；后端验票历史和录像不由算法删除。最小监控checkpoint只保留继续监控所需状态与不可还原旧点的标识，不留过期摄像头/时刻/事件序列。跨cutoff误差组用存活事件不可逆哈希和pruned_positive/pruned_exit布尔量保留已有顺序歧义，相关存活事件全部到期后清除；随后新的非重叠证据仍可正常成为ACTIVE/CLOSED，不永久锁定歧义。哈希幂等墓碑仅保存不可逆逻辑键/内容摘要，阻止清理后重新入库；墓碑自身随不同事件累计，不能把总文件恒定当作已证明。

## 隔离本地启动与清理观察

使用本轮成对 worker/daemon，下面新目录不指向既有状态库；路径、公钥及签发服务由现场提供。先创建空隔离目录并准备公钥文件和外部证明签发端，公钥占位符不是凭据。该命令默认只统计，每分钟运行一次。

```powershell
$activityDemoRoot = 'D:\人脸算法\face-trajectory\runtime\activity-local-demo'
New-Item -ItemType Directory -Path $activityDemoRoot -Force | Out-Null
& 'D:\人脸算法\face-trajectory\evidence\2026-10-10-activity-retention\face-trajectoryd.exe' `
  -worker 'D:\人脸算法\face-trajectory\cpp\build-activity\Release\face_trajectory_worker.exe' `
  -state-file "$activityDemoRoot\isolated.sqlite" -addr 127.0.0.1:8081 `
  -auth-public-key-file "$activityDemoRoot\auth-public-keys.json" -auth-issuer '<受信后端issuer>' `
  -clock-public-key-file "$activityDemoRoot\clock-public-keys.json" -clock-issuer '<受信UTC权威issuer>' `
  -clock-proof-file "$activityDemoRoot\clock-proof.txt" -retention-interval 1m
```

服务启动只读 `/health` 可取得 challenge；在首次有效证明到达前其它HTTP失败关闭。本地重复验证直接运行唯一交接列出的 `verify_service.py/verify_retention.py`，它们生成合成签名器及新隔离库，不需要也不使用生产凭据。

有 `retention` 全局授权的后台客户端：

```powershell
$activityHeaders = @{Authorization=$env:FACE_TRAJECTORY_AUTHORIZATION}
Invoke-RestMethod 'http://127.0.0.1:8081/v1/retention/run' -Method Post -Headers $activityHeaders -ContentType 'application/json' -Body '{"dry_run":true}'
Invoke-RestMethod 'http://127.0.0.1:8081/v1/retention/status' -Headers $activityHeaders
```

删除要求启动时明确 `-retention-enabled`，请求 `{"dry_run":false}`；未启用返回409。指定P1库用重复 `-p1-retention-db '<已存在的绝对本地SQLite文件>'`，禁止HTTP传任意路径；缺库不自动新建，网络共享拒绝。不能漏掉仍使用的旧bridge库。P1维护请求、消费者租约、热缓存清空与重开需使用本轮匹配的P1程序；旧P1不配合时状态显示 BLOCKED/INCOMPLETE，不能据此认为全链清理成功。

Windows维护标记原子替换/清除遇到错误5/32/33时按5ms间隔有界重试，每个操作最多约1000ms；不是删掉旧标记再发布。P1单库成功须物理清理、统计和标记清除全部完成，报告 `pause_handshake=QUIESCENT, maintenance_request_status=CLEARED`；清除失败仍INCOMPLETE，不能仅凭VACUUM成功认定P1可恢复。已有屏障和COMMIT开始后的异常保留暂停，错误reason含Win32错误码、phase及retry_limit_ms。下一轮在同一显式配置库重试；不能手删request。2026-10-12本地修复证据见两个现有HANDOFF的恢复修复条目。

P1待投递载荷对过期脸/闸机的引用也会清除并隔离，原内容摘要及自身capture期限保留，不能把删掉引用当作允许原键新内容继续发送。Windows离线P1 dry_run采用主库/WAL/SHM拒绝写入句柄，已有WAL/SHM可只读检查；当前非Windows外部P1冻结dry_run明确fail closed，不为了统计建文件；C++核心不受此限制。Linux全链维护需补齐冻结支持并重新构建验证。

观察status的attempts/successes/failures/running/last_duration_ms/last_error/last_result，以及报告各活动observations_before/after、snapshot版本改写/删除、共享对象回收、push删除、P1各表统计。before/after_logical/after_physical分别报告逻辑量、freelist/reusable_bytes、主库/WAL/SHM字节数；VACUUM与WAL截断完成才排除过去空闲页残留。物理维护失败保持PENDING，显式retention_run返回`ok=false,complete=false`，Go计入failures并保留last_error/last_result，下一轮继续。dry_run在内存PlanningCopy中执行与真实清理相同的修剪及共享对象标记清扫，再回滚内存副本，`count_semantics=EXACT_IN_MEMORY_PLAN_NO_SOURCE_MUTATION`；原数据库、水位、索引和墓碑不改，原始行删除与GC计数是该固定now的精确规划值。默认删除关闭时只有统计，不能宣称保留已生效。

## 新活动、迁移与回退

新活动顺序：后端签发该活动的明确授权 → sync身份/config版本 → bindings完整范围 → 同活动登记图库/分类并配置核验时间profile → 明确 monitoring enabled=true → P1传当前作用域 → 用户新context选择并open/query。更换设备范围先按CAS同步绑定，再刷新P1版本与客户端订阅；旧版本输入HOLD，不能偷偷重写旧持久请求。P1原型摄像头配置变更目前需要重新加载服务；核心活动同步无需改代码。

生产启用属于另行部署授权，本轮未执行。启用前核对真实权威映射、权限动作、时钟签发溯源、全部P1库清单、当前版本与身份库、错误/容量告警、恢复和接收端到期拒绝。首次用新版本 `retention-enabled=false` 和 dry_run 核对，再在另行授权下启用删除。不得对生产库运行本轮隔离脚本。

迁移前停写并用 SQLite 一致性备份包含WAL；在隔离副本验证。旧全局数据保持未归属，不按时间/场地猜活动；有可信采集时间按原采集期限，无可信原期限立即清除历史载荷。登记全局图库不会隐式成为各活动图库，后端显式重同步。新的作用域键和墓碑属于新合同；旧二进制会忽略隔离或不能读新存储，回退必须同时恢复匹配的旧程序与清理前备份到隔离路径，不在新库上直接降级。备份恢复也不能把超窗历史送回服务，应保持清理启用并先取得新的可信证明；保留备份本身亦需部署方单独管理。

前后端最小配合：权威ID/num/venue及设备route/CAS接口、有限期签名授权、UTC挑战证明刷新、同次通行人脸关联、不可变source/event、选择上下文更新、旧回包/通知丢弃、推送活动版本/期限核验、全链存储清单及维护握手。真实登录退出、前端切换、生产鉴权、真实闸机/相机时间和容量均现场联调待完成；约600路/3090优先/2秒目标3秒可接受仅设计约束。

## 合同复核

文档校验工具在隔离目录安装固定版本依赖，P1实际配置检查使用已有模型运行依赖环境，不加载模型、不连接相机。以下service-final-03是31项最终成对产物的真实本地Go/C++行为检查所保存的请求/回包；最终重新运行时使用交接中最新calls路径。

```powershell
$activityValidatorVenv = Join-Path $env:TEMP 'face-activity-openapi-venv-20261010'
& 'D:\人脸算法\.venv-ops\Scripts\python.exe' -m venv $activityValidatorVenv
& (Join-Path $activityValidatorVenv 'Scripts\python.exe') -m pip install -r 'D:\人脸算法\face-trajectory\docs\contract-validation-requirements.txt'
& (Join-Path $activityValidatorVenv 'Scripts\python.exe') 'D:\人脸算法\face-trajectory\docs\validate_activity_contract.py' `
  --p1-python 'D:\人脸算法\face-recognition-engine\.venv\Scripts\python.exe' `
  --calls 'D:\人脸算法\face-trajectory\evidence\2026-10-10-activity-retention\service-final-03\calls.json' `
  --output 'D:\人脸算法\face-trajectory\evidence\2026-10-10-activity-retention\openapi-validation-current.json'
```

工具拒绝重复YAML键，完整OpenAPI规格验证并解析全部内部ref；按当前Go源码精确匹配HTTP路由、动作和scope，检验成功请求/回包及错误回包，验证新活动工作流和三份P1 JSON示例schema，再实际调用P1 validate/capacity-report。capacity-report保持UNMEASURED；结构schema通过不代表相机、权重、照片、时钟签发端或生产鉴权可用。示例配置state_file使用新隔离activity-local-demo路径，禁止将测试脚本改指生产库。
