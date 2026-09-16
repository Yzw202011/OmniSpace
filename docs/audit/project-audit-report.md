# OmniSpace AI v2.3.1 项目审计报告（四轮完整版）

- 报告日期：2026-08-07
- 审计对象：`e:\OmniSpace` 全仓（src/ 71+ Python 文件、frontend/ 约 1.3 万行 TS/TSX、launcher、tools、配置、文档）
- 审计基线：五份需求文档，矛盾裁决优先级 **B（可信）> E > D > A > C**（正交语言细则以 C 为准），详见 `baseline-checklist.md`（456 条合规基线）
- 审计方法：零信任原则——一切代码、注释、配置均不可信，逐条与文档交叉验证；所有"疑似问题"必须回查代码事实方可定性
- 执行团队：技术主管 / 前端 / 后端 / UI-UX / AI-ML / 测试 六角色协作模式

---

## 一、四轮审计总览

| 轮次 | 主题 | 报告文件 | 发现 | 修复 | 结论 |
|---|---|---|---|---|---|
| Round 0 | 零信任基线评估（前后端） | `round0-backend-assessment.md` / `round0-frontend-assessment.md` / `baseline-checklist.md` | P0×2 + 大量 P1/P2 | —（评估轮，产出基线） | 后端完成度 ~75%，前端架构健康但有契约漂移 |
| Round 1 | 代码质量与规范 | `round1-backend-fixes.md` | 13 条 | 13/13（P0×1、P1×9、P2×3） | ✅ 通过 |
| Round 2 | 功能完整性与需求符合性 | `round2-function-integrity.md` / `round2-fixes.md` | 16 条（R2-B01~B16） | P1×7 全部修复；P2×5 修复；4 项文档侧裁决 | ✅ 通过 |
| Round 3 | 性能、安全与可维护性 | `round3-fixes.md` | P0×1、P1×5、P2×22 | P0×1、P1×5、P2×13；12 项建档 | ✅ 通过 |

**累计：P0×3 全部修复并运行时验证；P1 全部修复；P2 修复 21 项、建档 12 项；文档侧裁决 4 项。**

---

## 二、Round 0：零信任基线评估

### 2.1 后端评估结论（完成度 ~75%）

| 模块 | 完成度 | 关键发现 |
|---|---|---|
| AI 对话 | ~85% | 真实 Qwen 推理 + SSE/WS 流式；mock 内存双写为历史残留 |
| AI 绘画 | ~80% | SDXL 真实管线（fp16/offload/VAE tiling）；ControlNet 预览空壳 |
| 漫剧创作 | ~75% | 分镜/导演台/视频任务真实落库；音色试听为占位音频 |
| 知识学习 | ~80% | 主题/会话/配额/Agent 循环/FTS5+RRF 真实；WS 进度端点缺失 |
| 模型管理 | ~85% | 注册/导入/SHA256/加载/卸载/互斥/预测均接线 |
| 视频风格 | ~60% | LTX-2 基座未随包，出货环境诚实降级 |
| 调度器 | ~80% | 协同调度历史学习引擎缺失；阈值与文档 B 不一致 |
| 系统/硬件 | ~60% | **/system/diagnose 全量假数据（P0）**；项目导入/导出空壳 |
| 内置浏览器 | ~85% | Playwright 专属线程 + 安全层 + 进程池预热，真实实现 |

**两项 P0 级违约**：BK-002（diagnose 26 项硬编码假结果，欺骗性）、契约/真实性违约各一。

### 2.2 前端评估结论
- 架构健康：Hash 路由 8 页、Zustand 10 store、Three.js 6 模块分层清晰；
- 主要问题：部分 store 与后端契约漂移、布局规范（文档 D）局部未落地、残留调试代码。

### 2.3 基线产出
456 条机器可核对合规基线（技术栈 40 / API 契约 26 / 数据库 22 / 布局视觉 47 / 功能任务 123 / 引擎调度 68 / 优化验收 32 / 语言规范 42 / 性能指标 36 / 矛盾裁决 20），作为后续所有轮次的唯一核对依据。

---

## 三、Round 1：代码质量与规范审计

**修复 13 条，全部经运行时验证：**

| 优先级 | 编号 | 主题 |
|---|---|---|
| P0 | BK-002 | `/system/diagnose` 26 项硬编码假结果 → 注册表驱动真实探测（7 项复用 startup_check、12 项新增、2 项诚实 warn） |
| P1 | BK-028 | 文件路径任意读取 → `_resolve_safe_path()` 白名单 + 穿越校验（实测 `C:/Windows/...` 被拦截） |
| P1 | BK-041/042 | 项目导出/导入真实化（ZIP 归档 + DB 恢复） |
| P1 | BK-023 | 调度阈值对齐文档 B §4.1（五项阈值） |
| P1 | BK-011 | 硬件等级按 GPU 型号名六档自适应 |
| P1 | BK-012/013/014 | ControlNet/语音/视频诚实降级标记 |
| P1 | BK-010 | 协同调度历史学习引擎 `history.py` 落地 |
| P1 | BK-046/047/048 | tools 三脚本引用已删除模块修复并实跑 |
| P1 | BK-027 | 调度器 GPU 利用率键名错误（恒 0.0） |
| P1 | BK-018 | 激活/机器指纹子系统如实 warn 标注 |
| P2×3 | — | DB 表数硬编码→动态统计；调度器初始化缺漏；注释文档矛盾 |

**验证示例**：`POST /system/diagnose` 返回 `{total:26, pass:21, warn:5, fail:0}`，warn 全部为预期诚实降级项。

---

## 四、Round 2：功能完整性与需求符合性审计

### 4.1 端点完整性比对（文档 B §7.1 权威基线）
- /chat、/paint、/video、/models、/style、/browser（6/6）全部符合；
- 发现缺口：分镜 list/reorder/ai-describe/preview、导演台 export、/system/info、学习进度 WS。

### 4.2 P1×7 全部修复
1. **R2-B01 对话被动补全**：不确定性标记检测（24 标记）→ 关键词提取 → 浏览器池快速搜索（≤3 页/25s 预算）→ 重推理一次；三条推理路径（非流式/SSE/WS）全接入；`dialog_gap` 触发器沉淀学习主题。
2. **R2-B02 学习触发器接线**：5 触发器（手动/定时/空闲/项目驱动/对话缺口）全部生效；变更类请求上报用户活动，GET 轮询不误判。
3. **R2-B03 LoRA 自动微调**：频率窗口 + 样本阈值 + GPU 空闲门控 → 自动入队；防重入 + 600s 节流。
4. **R2-B04 知识 LoRA 接入推理**：`PeftModel.from_pretrained` 挂载 + 热更新 + 基座回退，"学习→进化→应用"闭环打通。
5. **R2-B05 学习进度 WS**：`/learn/session/progress` 2s 快照推送。
6. **R2-B06 分镜 list/reorder**：sort_index 持久化。
7. **R2-B07 分镜 ai-describe/preview**：单行重生成 + 引擎预览（未就绪诚实降级）。

### 4.3 P2 处理
- 修复 5 项：R2-B08（director/export + 占位图诚实标记）、R2-B09（/system/info 聚合）、R2-B13（§4.4 三处默认值 weekly/mask/true）。
- 文档侧裁决 4 项：R2-B11（文档 E 独有端点裁剪，ADR-001）、R2-B14（以代码事实修订文档 B 端口/WS 路径）、R2-B15（Celery/Redis 标注为可选横向扩展）、R2-B16（users/assets 表裁决）。
- R2-B12（§4.3 策略自适应 3 项）→ 移交第三轮评估（见 Round 3 闭环决策）。

---

## 五、Round 3：性能、安全与可维护性审计

三路并行（后端 / 前端 / 架构契约），全程代码事实核验，剔除误报 20+ 项（含路径穿越、SQL 注入、SSRF、命令注入、反序列化、XSS 等"疑似漏洞"——经逐条复核均已有多层防护）。

### 5.1 P0×1（技术主管复核升级）
- **launcher 心跳 TypeError**：`Request(url, timeout=3)` 误用 → `is_healthy()` 永远 False → 一键启动必然失败。系 RC 交付版修复未回灌源码树的历史残留。修复三处（端点 `/health`、timeout 位置、信封校验 `data.db`），实测存活实例 True / 死端口 False。

### 5.2 P1×5 全部修复
1. **R3-SEC1 WS 无 Origin 校验（CSWSH）**：新增 `ws_origin_guard()` 复用本地白名单，4 个 WS 端点全接入。实测恶意 Origin 拒绝 403 / 本地放行 / 无 Origin 放行。
2. **R3-FE1 流式全列表重渲染**：MessageBubble memo + 自定义按值比较 + DialogPage 回调 ref 稳定化。
3. **R3-FE2 WS 连接池泄漏**：`releaseWsConnection`/`releaseDialogStream` + 流式结束 cleanup 释放。
4. **R3-ARCH1 launcher 端口 8765→5800**：消除一键启动后前端 REST 全灭的割裂故障。
5. **R3-ARCH3 DB 损坏无自愈**：`PRAGMA quick_check` + `.corrupt-{ts}` 隔离 + 空库重建，与 `/system/backup` 形成备份-恢复闭环。
6. **R3-ARCH8 契约断裂 8 端点分层处置**：cancel/reorder 后端真实补齐（含 `train_tasks.priority` 幂等迁移、`TrainStatus.CANCELLED`、训练线程出队跳过）；6 个无 UI 入口超前接线端点前端 `@deprecated` 标注建档。

### 5.3 P2×13 修复
上传预检大小（×3）、HOST 非回环启动告警、vram_manager 死代码重命名、ApiError.http_status 死参数删除、中间件注释修正、方案文档阈值同步（80/90/95）、launcher 磁盘水位 20GB、流式滚动跟底判定、blob URL 释放、API_BASE 同源推导等。

### 5.4 已验证正确的关键机制（正面清单，防后续误报）
- **安全**：路径穿越多层防护（_resolve_safe_path / file_store.resolve / Path().name）、SQL 全参数化、subprocess 全 list 形式无 shell=True、SSRF 路由拦截（协议白名单/重定向链/黑名单）、yaml.safe_load、零硬编码密钥、静态挂载 API 命名空间排除；
- **性能**：显存记账闭环 + 强制卸载真实生效 + 嵌入模型启动期独占低位显存段（16GB 卡实测空闲 5.1GB→13.4GB）、秒级阻塞调用全部线程池化、文件哈希 1MB 流式、WAL + busy_timeout + 写锁；
- **前端**：Three.js 资源 dispose 完整、31 处定时器全配对、18 处全局监听全配对、零 dangerouslySetInnerHTML、零显式 any、零 console 调试残留。

---

## 六、技术债建档（12 项，全部如实记录于 round3-fixes.md §六）

消息列表虚拟化、4 个巨型组件拆分、WS→SSE 迁移、config.yaml 零容错兜底、DB 显式事务、video_engine 真实管线显存预检（启用前必做）、ws_hub 反向依赖、模型导入路径白名单、Redis pickle 边界、base64 内联迁移、宽捕获渐进收窄、6 个超前接线端点。

---

## 七、四轮审计结论

| 维度 | 结论 |
|---|---|
| 代码质量与规范 | ✅ 通过（文档 C 铁律遵守，审计标记可追溯） |
| 功能完整性与需求符合性 | ✅ 通过（文档 B 基线端点 100% 覆盖，缺口全部补齐或诚实降级） |
| 性能 | ✅ 通过（显存/事件循环/DB/大文件/后台线程无缺陷） |
| 安全 | ✅ 通过（零信任复核后 8 大攻击面全部有防护，CSWSH 已闭环） |
| 可维护性 | ✅ 通过（分层单向、配置集中、降级统一、技术债建档） |

**总体判定：四轮审计全部通过，项目达到发布质量基线。**
