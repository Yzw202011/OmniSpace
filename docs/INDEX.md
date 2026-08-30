# docs/ 文档索引与时效声明

> **回答一个问题：哪份文档代表现状。** 每份文档标注时效状态，历轮审计口径漂移自此有据可查。
> 维护规则：新增文档必须在本索引登记；文档被覆盖必须归档并在此声明，禁止同名多版并存。

## 状态图例

- **现行** —— 代表当前现状，决策依据
- **记录** —— 历史时点的快照（已执行的计划 / 已发生的审计 / 已接受的 ADR），只读不改，作追溯证据
- **参考** —— 原始素材（竞品抓取物等），无时效语义
- **归档** —— 已被其他文档覆盖，勿据此决策

---

## 一、现行文档（代表现状）

| 文档 | 主题 | 说明 |
| --- | --- | --- |
| `../README.md` | 项目入口 | 是什么/怎么启动/目录结构/开发工作流 |
| `requirements-baseline.md` | **需求基线** | 从外部文档E/B抽取的仍有效需求（RTM 附录 A），需求-矩阵对应关系的仓库内真源 |
| `requirements-traceability.md` | **需求追踪矩阵** | 44 条 F/M/A/E 需求状态唯一真源；「来源§」指向基线章节 |
| `design/architecture-overview.md` | 架构现状 | 分层结构/中间件链/推理引擎/数据流 |
| `design/api-endpoints.md` | API 现状 | 端点总表（2026-08-20 机器枚举快照：270 HTTP + 4 WS；2026-08-28 静态实测已增至 **328 HTTP 装饰器（含别名）**，4 WS 不变，增量以代码为准） |
| `design/database-er.md` | 数据库现状 | **31 张用户表**（37 个对象含 FTS 影子表）ER + 迁移机制（user_version=7）+ 加密范围 |
| `design/frontend-error-policy.md` | 前端错误策略 | 错误呈现三分法（TOAST/CONSOLE/SILENT 白名单），代码载体 @/utils/errors |
| `deployment-manual.md` | 部署 | 硬件门槛/环境装配/模型资产配置表/RC 交付 |
| `troubleshooting.md` | 故障排查 | OOM/端口冲突/DB 版本冲突/模型探测失败四大故障 |
| `model-deployment-plan.md` | **模型搭配与部署** | 模型推荐主题唯一权威版（2026-08-20 收敛裁定）；接线进度以 RTM M 域为准 |
| `omnispace-tech-doc/`（HTML） | **全量技术文档（协作者上手版）** | 2026-08-26 基于代码实测编写：架构 / 技术栈 / 前后端详解 / 漫剧管线 / 资源治理 / 数据与模型 / 部署 / 协作指南；面向新加入协作的开发者，含 CLAUDE.md 与代码现实差异对照 |
| `audit-task-checklist.md` | 工程治理 | P0/P1/P2 任务清单与完成记录 |
| `ADR-001.md` | 架构决策 | 从零实现架构决策（已接受） |
| `ADR-002-tauri-shell-decision.md` | 架构决策 | Tauri 桌面壳裁剪裁决（2026-08-20）：浏览器+launcher 为正式交付形态，含重启条件 |
| `ADR-003-model-foundation-layer.md` | 架构决策 | 模型服务通用底座裁决（2026-08-29 已接受，**P1-P3 全部落地**）：统一注册表/BaseEngine 协议+品类注册表/生命周期状态机，验收口径与实测记录在文 |
| `manga-v8-comfy-workflow.md` | 漫剧 ComfyUI 工作流 | 漫剧分镜关键帧生视频 V8（2026-08-29 建档）：V6 单采骨架 + 关键帧/资产锚定输入组；JSON 在 ComfyUI user 工作流目录（不追踪），生成脚本 `tools/build_manga_v8_workflow.py` 为真源 |
| `test-plan-manga.md` | **漫剧模块全维度测试计划** | 9 轮测试（R1 契约/R2 数据/R3 引擎/R4 前端/R5 集成/R6 异常恢复/R7 性能资源/R8 安全/R9 回归冒烟），含缺陷分级、档位表、排期；用例来源=历史实测证据 |

## 二、记录文档（历史快照，只读）

| 文档 | 时点 | 性质 |
| --- | --- | --- |
| `architecture-security-plan.md` | 2026-08-07 | Round-0 裁决版架构方案；架构现状已由 `design/architecture-overview.md` 接棒，本文保留裁决过程与 20 条矛盾裁定记录 |
| `plan-g1-g2-implementation.md` | — | G1/G2 实施计划（已执行完毕：F-06 分镜四端点 ✅、F-08 解说漫剧 ✅） |
| `视觉优化报告-20260820.md` | 2026-08-20 | 前端第十一轮视觉终评报告（均分 9.49） |
| `audit/`（14 个文件） | round0~round5 | 历轮审计与修复报告链，证据性存档；结论以 RTM + git 历史为准 |
| `audit/round-2026-08-28-code-audit.md` | 2026-08-28 | **全库逐行代码审计**（后端 60k 行全覆盖 + 前端安全关键路径 + launcher）：1 P0 / 27 P1 / 137 P2，含分模块功能审计表、Top10 修复优先级；P0/P1 均带 文件:行号 证据，主审计者亲核条目已标注 |
| `audit/round-2026-08-29-competitor-alignment.md` | 2026-08-29 | **竞品分镜图全维度对齐实验**：同源素材+描述词两轮真实生成（comfy+PuLID），DINOv2 逐维测量（场景锚 0.853 超竞品 0.610，四格全对位）；含节拍拆分/text_priority 开关等 8 项修复与学说分歧裁定记录 |
| `design/art-style-library.jsonl` | 2026-08-29 | **画风库数据资产（1200 条）**：19,189 候选中按类配额精选；261 基础画风 × 时代/地域/色彩等 32 维修饰；命名对齐豆包/GPT-4o 词表；durable 副本供重建。⚠️ 落库口径 2026-08-29 变更：1200 条全量入库后经用户裁定**修剪至 517 条**（仅保留 STYLE_PACKS 正则可路由命中的条目，无风格包支撑者剔除；全表备份 `data/art_styles_backup_20260829.json` 可整体回灌）——art_styles 表现值 517，`custom:{id}` key 建项目即用 |
| `model-deployment-plan/`（HTML） | 2026-08-14 | 部署计划的 HTML 可视化版 |
| `project-sdlc-audit/`（HTML） | — | SDLC 审计 HTML 版（P2 系列任务的立项依据） |

## 三、参考素材（无时效语义）

| 目录 | 内容 |
| --- | --- |
| `competitor/`（34 个文件） | 竞品漫剧编辑器抓取产物（JS/CSS/字符串/API 分析），漫剧模块 1:1 复刻竞品布局的依据 |

## 四、归档文档（已被覆盖，勿据此决策）

| 文档 | 归档原因 | 权威替代 |
| --- | --- | --- |
| `archive/model-recommendation-based-on-doc.md` | 排名表口径被部署计划取代 | `model-deployment-plan.md` |
| `archive/model-recommendation-final.md` | "final" 命名与实际不符，内容被部署计划吸收 | `model-deployment-plan.md` |

## 五、仓库外源文档（根目录 txt，不入库）

| 文档 | 时效定位 |
| --- | --- |
| `程序开发计划文档B.txt` | **需求可信度最高**（架构安全方案裁定：B > E > D > A > C）；仍有效需求已抽取入 `requirements-baseline.md` |
| `极致细粒度全量文档E.txt`（修正版） | 需求主源（RTM 对照基线）；仍有效需求已抽取入基线 |
| `布局视觉与规范D.txt` | 前端视觉规范源（Sakura 主题、4px 栅格等铁律仍在 project_memory 生效） |
| `编程语言规范A.txt` / `全量项目技术文档C.txt` | 编码规范与技术描述，部分被代码现实覆盖 |
| `标准裁定书.txt` | 历史裁定记录 |
| `开发清单.txt` | 历史开发清单 |

> 与仓库内文档冲突时，一律以仓库内现行文档为准（仓库内真源原则，基线管理规则 A.7）。

## 六、按场景找文档

| 我想… | 看这份 |
| --- | --- |
| **新加入协作，要一份完整地图** | `omnispace-tech-doc/`（全量技术文档 HTML）→ `../CLAUDE.md`（注意与代码差异，见技术文档 12.4 节） |
| 了解项目是什么、怎么跑起来 | `../README.md` → `deployment-manual.md` |
| 查某条需求的状态与决策 | `requirements-traceability.md`（状态）→ `requirements-baseline.md`（需求原文） |
| 找某个 API 端点 | `design/api-endpoints.md` |
| 查某张表结构 | `design/database-er.md` |
| 写前端的 catch/错误提示 | `design/frontend-error-policy.md`（三分法）→ `@/utils/errors` 两个入口 |
| 理解系统架构 | `design/architecture-overview.md` |
| 配置模型资产 | `deployment-manual.md` §3 → `model-deployment-plan.md`（目标架构） |
| 排查故障 | `troubleshooting.md` |
| 跑全量测试 | `../tools/run_tests.py`（唯一入口）→ 定位见 `../tests/README.md` |
| 查治理任务进度 | `audit-task-checklist.md` |
| 查历史审计结论 | `audit/` 对应轮次 → 以 RTM 为最终口径 |

---

*索引建立：2026-08-20（TASK-P2-04）*
