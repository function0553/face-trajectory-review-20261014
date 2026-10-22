# 时空算法完整源码审核

**2026-10-22 当前算法侧接口：** 已按人员轨迹三方协议v1.0实现固定名单登记、已确认单脸点位UPSERT/RETRACT推送、持久修订及失败重试，HTTP200/code=200/event_id匹配才确认交付。当前接口版本为0.14.0。

- [可转发给后端的接口说明与完整JSON/HTTP样例](face-trajectory/docs/backend-integration-v1/时空算法-后端接口对接说明.md)
- [算法侧部署接入与实际模板准备](face-trajectory/docs/person-track-platform-v1.md)
- [本轮验证证据](face-trajectory/evidence/2026-10-22-person-track-v1/README.md)

算法负责识别后上报人员经过的摄像头点位；本次不实现后端存储、查询或UE显示。真实摄像头表、人员数据、凭证、状态库、图片、模型及编译产物不公开。下面的10-14说明保留其历史上下文；当前文件及SOURCE-MANIFEST.json为本轮源码。

**2026-10-14本地修复后更新：** D-01～D-07与可选3D拓扑折线已完成本地验证，见[修复与交接](face-trajectory/docs/repair-topology-2026-10-14.md)与[本轮验证摘要](face-trajectory/evidence/2026-10-14-repair-topology/validation-summary.json)。Windows MSVC和Linux GCC12支持C++17/20；Go全包、31秒HTTP恢复和两平台签名端到端均通过。新测试、固定Windows依赖vendor、合成拓扑示例与最新SHA清单一起同步。现场联调、生产、准确率及容量仍待验收。

这是算法开发交付方为外部源码审核提供的2026-10-14快照，时区Asia/Singapore。主要对象是P2 `face-trajectory`，并提供P1 `face-recognition-engine`的相关完整自有Python源码供跨模块合同审核。仓库没有Windows程序、运行数据库、图像、模型权重、历史服务器原始资料或开发环境。

**请先读 [GEMINI_REVIEW_PROMPT.md](GEMINI_REVIEW_PROMPT.md)，再按源码提出问题。** Gemini此前没有解压ZIP，基于文件名提出的风险尚未证实。该仓库是当前文件快照，原工作区无Git历史。

## 源码入口

| 审核范围 | 入口 |
| --- | --- |
| P2完整交接与当前能力/限制 | [face-trajectory/HANDOFF.md](face-trajectory/HANDOFF.md) |
| P2完整C++核心、头文件、当前C++测试与内置依赖 | [face-trajectory/cpp](face-trajectory/cpp) |
| P2 Go服务、鉴权、UTC、进程管理、清理、推送及现有测试 | [face-trajectory/go](face-trajectory/go) |
| HTTP合同 | [OpenAPI 0.14.0](face-trajectory/openapi.yaml) |
| 活动隔离、可信时间、保留/恢复与后台生命周期 | [activity-service.md](face-trajectory/docs/activity-service.md) |
| 身份入流和P1/P2对接 | [monitoring.md](face-trajectory/docs/monitoring.md)、[P1运行合同](face-recognition-engine/docs/monitoring-runtime.md) |
| 点位连接、方向与不确定性 | [point-connections.md](face-trajectory/docs/point-connections.md) |
| P1采集/质量/索引/调度/投递/维护源码 | [face-recognition-engine/src/face_engine](face-recognition-engine/src/face_engine) |
| 完整清单及原文件SHA256 | [SOURCE-MANIFEST.json](SOURCE-MANIFEST.json) |
| 直接上传Gemini的UTF-8文本 | [audit_bundle.txt](audit_bundle.txt)，排除第三方SQLite/JSON源码，仅用于减小读取体积 |

P2所有当前自有代码、内置SQLite/JSON依赖、构建文件、文档、配置、collection、tools和现有测试均逐文件复制并校验；本轮包含经过回归的最小算法/运行修复，变更见当前交接。历史evidence仅附选定合成检查报告和tools依赖的驱动。交接中的其它历史报告/服务器资料/旧程序路径属于本机旧快照，未公开；请勿把旧SHA、旧测试数量或遗漏资料链接当成本次执行结果。原生源码路径与行号保持不变。

## 构建与审核边界

P2 C++17需要CMake≥3.20及C++工具链，Go模块声明1.22。以P2为当前目录，Windows可运行 `cmake -S cpp -B cpp/build -G "Visual Studio 17 2022" -A x64` 和 `cmake --build cpp/build --config Release`；在go目录 `go build ./cmd/face-trajectoryd`。内置依赖源码完整保留；第三方JSON许可在 `cpp/vendor/LICENSE.MIT`，SQLite来源说明保留在供应源码头部。仓库没有对全部代码额外授予一个推测的统一开源许可证；模型/权重许可也不由源码审核替代。

P1仍有Python运行原型，正式C++/Go全链迁移未实施；源码提供不代表模型/运行环境已交付。现场同人标签、真实后端鉴权/UTC/接收器、相机→P1→P2→业务前端、生产部署/清理、目标生产服务器和600路/3090容量仍待验收；本机WSL Linux验证已完成。普通输出是相机点到点连接，鹰眼单镜连续画面轨迹后置。

本次先本地修复并完成新增/既有回归，再同步源码与摘要。没有修改生产。旧ZIP与旧报告代表修复前快照，最新验证以本轮交接和摘要为准。

GitHub完整性可用Python逐项核对 `SOURCE-MANIFEST.json` 的bytes和sha256；清单记录当前源文件原始字节摘要、排除项及P2当前目录的覆盖检查。源码之外的本机编译目录、库和缓存不属于上传范围。

## 交接

旧ZIP被审核工具当二进制读取，无法据此审查正文。改为公开GitHub原生文本源码，并附单个带路径/原行号的audit_bundle.txt。Gemini建议的PowerShell命令漏选.cpp/.hpp，导出时明确保留。P2源码复制保留原字节；运行库、模型、旧二进制和历史原始数据排除。完整上传以清单逐文件核对为准。

审核意见请给严重性、文件/行号、实际触发条件、源码证据、影响和修复方向；将已确认缺陷、待复现风险、已实现保护和现场待验收项分开。建议先审Go与C++时空核心，再核对P1/P2完整入流；不要仅以双进程或多个交接文件名判断缺陷。
