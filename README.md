# 时空算法完整源码审核

这是算法开发交付方为外部源码审核提供的2026-10-14快照，时区Asia/Singapore。主要对象是P2 `face-trajectory`，并提供P1 `face-recognition-engine`的相关完整自有Python源码供跨模块合同审核。仓库没有Windows程序、运行数据库、图像、模型权重、历史服务器原始资料或开发环境。

**请先读 [GEMINI_REVIEW_PROMPT.md](GEMINI_REVIEW_PROMPT.md)，再按源码提出问题。** Gemini此前没有解压ZIP，基于文件名提出的风险尚未证实。该仓库是当前文件快照，原工作区无Git历史。

## 源码入口

| 审核范围 | 入口 |
| --- | --- |
| P2完整交接与当前能力/限制 | [face-trajectory/HANDOFF.md](face-trajectory/HANDOFF.md) |
| P2完整C++核心、头文件、当前C++测试与内置依赖 | [face-trajectory/cpp](face-trajectory/cpp) |
| P2 Go服务、鉴权、UTC、进程管理、清理、推送及现有测试 | [face-trajectory/go](face-trajectory/go) |
| HTTP合同 | [OpenAPI 0.13.0](face-trajectory/openapi.yaml) |
| 活动隔离、可信时间、保留/恢复与后台生命周期 | [activity-service.md](face-trajectory/docs/activity-service.md) |
| 身份入流和P1/P2对接 | [monitoring.md](face-trajectory/docs/monitoring.md)、[P1运行合同](face-recognition-engine/docs/monitoring-runtime.md) |
| 点位连接、方向与不确定性 | [point-connections.md](face-trajectory/docs/point-connections.md) |
| P1采集/质量/索引/调度/投递/维护源码 | [face-recognition-engine/src/face_engine](face-recognition-engine/src/face_engine) |
| 完整清单及原文件SHA256 | [SOURCE-MANIFEST.json](SOURCE-MANIFEST.json) |
| 直接上传Gemini的UTF-8文本 | [audit_bundle.txt](audit_bundle.txt)，排除第三方SQLite/JSON源码，仅用于减小读取体积 |

P2所有当前自有代码、内置SQLite/JSON依赖、构建文件、文档、配置、collection、tools和现有测试均逐文件复制并校验；未删改算法源码。历史evidence仅附选定合成检查报告和tools依赖的驱动。交接中的其它历史报告/服务器资料/旧程序路径属于本机旧快照，未公开；请勿把旧SHA、旧测试数量或遗漏资料链接当成本次执行结果。原生源码路径与行号保持不变。

## 构建与审核边界

P2 C++17需要CMake≥3.20及C++工具链，Go模块声明1.22。以P2为当前目录，Windows可运行 `cmake -S cpp -B cpp/build -G "Visual Studio 17 2022" -A x64` 和 `cmake --build cpp/build --config Release`；在go目录 `go build ./cmd/face-trajectoryd`。内置依赖源码完整保留；第三方JSON许可在 `cpp/vendor/LICENSE.MIT`，SQLite来源说明保留在供应源码头部。仓库没有对全部代码额外授予一个推测的统一开源许可证；模型/权重许可也不由源码审核替代。

P1仍有Python运行原型，正式C++/Go全链迁移未实施；源码提供不代表模型/运行环境已交付。现场同人标签、真实后端鉴权/UTC/接收器、相机→P1→P2→业务前端、生产部署/清理、目标Linux和600路/3090容量仍待验收。普通输出是相机点到点连接，鹰眼单镜连续画面轨迹后置。

本次只导出/发布源码及文档，不运行新增算法测试，不改生产。10-14独立Release构建和Go build/vet/test已在此前打包完成，其它证据保持原日期。

GitHub完整性可用Python逐项核对 `SOURCE-MANIFEST.json` 的bytes和sha256；清单记录当前源文件原始字节摘要、排除项及P2当前目录的覆盖检查。源码之外的本机编译目录、库和缓存不属于上传范围。

## 交接

旧ZIP被审核工具当二进制读取，无法据此审查正文。改为公开GitHub原生文本源码，并附单个带路径/原行号的audit_bundle.txt。Gemini建议的PowerShell命令漏选.cpp/.hpp，导出时明确保留。P2源码复制保留原字节；运行库、模型、旧二进制和历史原始数据排除。完整上传以清单逐文件核对为准。

审核意见请给严重性、文件/行号、实际触发条件、源码证据、影响和修复方向；将已确认缺陷、待复现风险、已实现保护和现场待验收项分开。建议先审Go与C++时空核心，再核对P1/P2完整入流；不要仅以双进程或多个交接文件名判断缺陷。
