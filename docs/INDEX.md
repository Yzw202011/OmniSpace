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
| `requirements-traceability.md` | **需求追踪矩阵** | 45 条 F/M/A/E 需求状态唯一真源；「来源§」指向基线章节 |
| `design/architecture-overview.md` | 架构现状 | 分层结构/中间件链/推理引擎/数据流 |
| `design/api-endpoints.md` | API 现状 | 270 HTTP + 4 WS 端点总表（机器枚举零遗漏） |
| `design/database-er.md` | 数据库现状 | 30 张表 ER + 迁移机制 + 加密范围 |
| `design/frontend-error-policy.md` | 前端错误策略 | 错误呈现三分法（TOAST/CONSOLE/SILENT 白名单），代码载体 @/utils/errors |
| `deployment-manual.md` | 部署 | 硬件门槛/环境装配/模型资产配置表/RC 交付 |
| `troubleshooting.md` | 故障排查 | OOM/端口冲突/DB 版本冲突/模型探测失败四大故障 |
| `model-deployment-plan.md` | **模型搭配与部署** | 模型推荐主题唯一权威版（2026-08-20 收敛裁定）；接线进度以 RTM M 域为准 |
| `audit-task-checklist.md` | 工程治理 | P0/P1/P2 任务清单与完成记录 |
| `ADR-001.md` | 架构决策 | 从零实现架构决策（已接受） |
| `ADR-002-tauri-shell-decision.md` | 架构决策 | Tauri 桌面壳裁剪裁决（2026-08-20）：浏览器+launcher 为正式交付形态，含重启条件 |

## 二、记录文档（历史快照，只读）

| 文档 | 时点 | 性质 |
| --- | --- | --- |
| `architecture-security-plan.md` | 2026-08-07 | Round-0 裁决版架构方案；架构现状已由 `design/architecture-overview.md` 接棒，本文保留裁决过程与 20 条矛盾裁定记录 |
| `plan-g1-g2-implementation.md` | — | G1/G2 实施计划（已执行完毕：F-06 分镜四端点 ✅、F-08 解说漫剧 ✅） |
| `视觉优化报告-20260820.md` | 2026-08-20 | 前端第十一轮视觉终评报告（均分 9.49） |
| `audit/`（14 个文件） | round0~round5 | 历轮审计与修复报告链，证据性存档；结论以 RTM + git 历史为准 |
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
| 了解项目是什么、怎么跑起来 | `../README.md` → `deployment-manual.md` |
| 查某条需求的状态与决策 | `requirements-traceability.md`（状态）→ `requirements-baseline.md`（需求原文） |
| 找某个 API 端点 | `design/api-endpoints.md` |
| 查某张表结构 | `design/database-er.md` |
| 写前端的 catch/错误提示 | `design/frontend-error-policy.md`（三分法）→ `@/utils/errors` 两个入口 |
| 理解系统架构 | `design/architecture-overview.md` |
| 配置模型资产 | `deployment-manual.md` §3 → `model-deployment-plan.md`（目标架构） |
| 排查故障 | `troubleshooting.md` |
| 查治理任务进度 | `audit-task-checklist.md` |
| 查历史审计结论 | `audit/` 对应轮次 → 以 RTM 为最终口径 |

---

*索引建立：2026-08-20（TASK-P2-04）*
