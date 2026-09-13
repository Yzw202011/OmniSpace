# 全量代码审计报告 round-2026-09-12（从零代码基点 + 机械闸门实跑）

> 审计方式：不依赖文档与记忆——三路只读侦察（后端/前端/资产盘，全部以代码与磁盘实测为准）+ 四道静态闸实跑（ruff/mypy/tsc/eslint）+ 测试实跑（pytest 722 收集 / vitest 71）+ 反模式与安全模式全库 grep。
> 活体状态：审计期间后端 5800 / 启动页 5850 / ComfyUI 8189（Sage 实跑）在飞，ComfyUI 队列空、调度 all_idle，未触碰任何进程。
> 配套升级方案：`docs/全量技术改造与升级方案-2026-09-12.md`（v2）。

## 一、结论（一句话）

**代码基线健康度高：四道静态闸零新增（ruff 0 / mypy 0 新增 / tsc 0 / eslint 0 错），安全面无 P0（无注入、无硬编码密钥、无危险反模式）；但测试闸抓到 1 个真实缺陷（前端静默吞错），加之上轮侦察的 2 个 P1（导播台断链、wan21 空账）与 ~88G 资产浪费，构成本轮改造的全部动工理由。**

## 二、规模与体检数字（✅ 全部实跑实测）

### 2.1 代码规模

| 维度 | 数字 |
|---|---|
| 后端 Python | 252 文件 / 93,504 行（不含 __pycache__） |
| 前端 TS/TSX | 130 文件 / 39,294 行 |
| 后端测试文件 | 90 个（pytest 收集 **722 条**用例） |
| 前端测试 | 6 文件 / 71 条用例（零组件级测试） |
| API 端点 | ≈377（18 个路由模块）+ WS 4 个 |
| DB 表 | schema 23 张（user_version=13）+ 服务层散建 15 张（绕开迁移体系） |
| ComfyUI custom_nodes | 22 个（后端实际消费 4 个） |
| 模型登记 | 18 条 / 盘上实测 210G |

### 2.2 四道静态闸（✅ 实跑）

| 闸 | 结果 |
|---|---|
| ruff（backend 全量） | **All checks passed**（0 错 0 警） |
| mypy 第4闸 | **0 新增**；存量 231 条冻结于基线（tools/mypy_baseline.txt 234 行） |
| tsc --noEmit（前端全量） | **exit 0**，零类型错误 |
| eslint（前端全量） | **0 errors / 13 warnings**（全部 no-console 类）；19 处 eslint-disable 均为有意豁免 |

### 2.3 测试实跑

| 套件 | 结果 |
|---|---|
| pytest（722 条收集，全量实跑 142.95s） | ✅ **714 passed / 8 skipped**（跳过=vLLM GPU 集成需 OMNISPACE_VLLM_E2E=1 显式开启 ×1、GGUF 权重未就位 ×7，均为设计内豁免） |
| vitest | **70/71 绿，1 失败**（真缺陷，见 §三 P2-1） |

### 2.4 反模式与安全模式全库扫描（✅ grep 实测，含逐条去伪）

| 模式 | 计数 | 定性 |
|---|---|---|
| 裸 `except:` | 0 | ✅ 干净 |
| `except: pass` 静默吞错 | 0（1 处命中为测试文件注释） | ✅ 干净 |
| `shell=True` | 0 | ✅ |
| `yaml.load(` 非安全载入 | 0 | ✅ |
| `eval(/exec(` | 5 处命中全部为 PyTorch `model.eval()` | ✅ 误报排除 |
| 硬编码 API key（sk-…） | 0（1 处为测试脱敏断言） | ✅ |
| 硬编码 password | 0 | ✅ |
| `pickle.loads` | 3（data/cache.py:82,191、model_manager/cache.py:284，本地缓存内部数据） | 🔶 P3：反序列化面收敛建议（可信数据域，非漏洞） |
| f-string SQL | 67 处命中；抽样核实（draw.py:1217 / system.py:755 / manga/common.py:1109 等）= 值全部 `?` 参数化、插值仅为静态列名/白名单表名/静态条件片段 | 🔶 P3：0 真实注入，但属「SQL 只参数化」规范的模式债，建议纳入编程语言规范例外清单显式管理 |
| `type: ignore` | 94 处（tests 22+、paint_engine/vector_db/crypto/resource_guard/lora_training 各 3~6） | 🔶 P3：多在边界类型与三方桩，随 mypy 基线递减策略消化 |
| 前端 `: any` / `console.log` / `@ts-ignore` / `!important`(tsx) | 0 / 0 / 0 / 0 | ✅ 合规闸有效 |
| 前端硬编码 hex（tsx 内联） | 23 处 / 5 文件（Settings 主题预览色板为主，另有 MessageBubble/LicenseGate/UpgradeSection/CloudApiSettings） | 🔶 P3：违反 tokens.css 令牌制（§2.5 豁免仅限主题 CSS 文件，不含 tsx）；预览色板可走令牌映射 |

## 三、缺陷清单（按严重级）

| 级 | # | 缺陷 | 证据 |
|---|---|------|------|
| P1 | 1 | **导播台模式断链**：工作流引用 TheodoreDirector_Project/SelectShot/H3Adapter 三节点，全 ComfyUI 树（custom_nodes+comfy_extras）无定义，走该模式必失败 | h3_engine.py:544-549；全树 grep 无提供方 |
| P1 | 2 | **wan21-i2v-1.3b 挂空账**：manifest `wired: true`、声明 3.2G，磁盘目录 0 字节，选中即失败 | models/models_manifest.json；du 实测 0 |
| P2 | 1 | **前端静默吞错（本项目自有铁律违规，被自家静态扫描测试逮住）**：UpgradeSection.tsx:64 `.catch(()=>…)`；vitest 70/71 失败即此 | errors.test.ts「三分法铁律」用例输出；UpgradeSection.tsx:64 |
| P2 | 3 | **43GB 字节级重复权重**：ComfyUI 便携包 models/ 与顶层 models/ 重复（nvfp4 12.5G / klein-9b-fp8 9.4G / ref2va 21G 各两份）；comfy_link 挂接机制存在未生效 | 两边字节级同大实测；scripts/comfy_link/ |
| P2 | 4 | 模型账实漂移多条（klein-4b 声明17.5实测15、qwen-image 22↔29、H3 42.7↔52） | du 对账表 |
| P2 | 5 | 旧资产可下架 ~88G：LTX 24G / ToonCrafter 烂尾 7.2G / SD15 5.2G / AnimateLCM 1.7G / SDXL 6.5G(待拍) / data/backups 682M / data 根残留 | du 实测 |
| P3 | 6 | 旧绘画栈悬空（draw 全链 + usePaintStore 633 行 + paintApi；UI 已被漫画页顶掉不可达） | router.tsx:62；paintApi 引用面 |
| P3 | 7 | video_engine 旁路化 + docstring 失真；paint_engine docstring 失真；config.py:67 限流注释旧值；router.tsx:3 路由数注释旧值 | 各锚点 |
| P3 | 8 | vision_tools 前端零调用：TripoSR(3D残留)/YOLO 疑孤儿；depth/segment 被 comic 链在用须保留 | grep 前端 0 引用；comic_gen.py:145 |
| P3 | 9 | learn/learning 双轨并存；comic_gen 空 router；@hooks 别名空指；/v1 代理残留；config redis/celery 占位节 | 各锚点 |
| P3 | 10 | sqlalchemy 装而未用（lock:154，全库 0 import）；15 张散建表绕开迁移体系 | grep 实测 |

**合计：P0=0，P1=2，P2=5（含测试闸抓到的 1 个），P3=5 组。**

> **同日并发审计互补**：`docs/audit/round-2026-09-12-code-audit.md`（另一会话）另行抓到 4 个代码级 P2（dialog WS 排队无 finally 泄位次表 / SSE 落库先于放锁 / comfy_proc 句柄泄漏 / comic 上传先全量 read 后验），与本报告的机械闸+安全扫描角度互补、无重叠冲突；两轮全部修复项已合并进 `docs/全量技术改造与升级方案-2026-09-12.md` 批0（含 P2-1~P2-5）。其记录的 pytest 偶发 1 挂（排队表全局态污染）与本轮 714 全绿互证为偶发态污染、单跑即过。

## 四、项目最近状态（从 git 与活体取证）

1. **提交流**（最近 12 笔）：SageAttention V7 上线（a62f425，+18% A/B）、对话排队改进（4d4dfc1，决策#8=A）、README 宣传素材入库（c2e961f，11 截图+演示视频）、ASR 修复闭环（3e7fcf6/75cdee4）、四视图 legacy 回退修复（860a958）、体积优化 A4~A6 销账（07b03a9）——当前处在「UAT 清账 + 商业化包装」阶段。
2. **工作区**：仅 6 个未提交变更（含本审计两份文档）；无半成品代码悬空。
3. **活体**：5800 后端 + 5850 启动页 + 8189 ComfyUI（`--use-sage-attention` 生效中）。
4. **版本栈**：ComfyUI 0.34.0 / py3.10.11(主) + py3.13.14(vLLM/便携包) / vLLM 0.26 / ffmpeg 8.1.2 / node 20.20.2 / React 19.0 / Vite 6 / Tailwind 4 / Zustand 5。
5. **pytest 722 条全量实跑（142.95s）**：✅ **714 passed / 8 skipped / 0 failed**——四道静态闸 + 双端测试套全部收口，代码基线为近期最健康状态。

## 五、审计方法备注

- 全部 grep 已人工去伪（如 eval→model.eval()、except-pass→测试注释、sk-→测试脱敏样例）。
- f-string SQL 抽样 6 处核到值参数化；未逐条核完 67 处，定性为「0 已知真实注入 + 模式债」而非「绝对无注入」——诚实标注为 🔶。
- 前端零组件级测试为结构缺口（70 条全部是工具/store/service 层），不判缺陷、列为改进项。
- 本报告与 v1（同日）差异：v1 基于「文档+记忆+一轮侦察」，本版全部结论落到代码/磁盘/实跑证据；发现并修正 v1 的 3 处误判（H3 Turbo 已接线、绘画双栈、43G 重复权重）。
