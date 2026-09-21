# 第三轮极致深审报告（R3）— round-2026-09-20（横切对账·在飞复核·总账终版）

> **元信息**
> - 审计日期：2026-09-20（第三轮，R1/R2 收官后）；基线提交：`f995a99`（审计期间并发会话新入库——PuLID EVA 自愈，已复核）。
> - **受限披露（诚实优先）**：本轮规划 6 路代理，**4 路因账户 5 小时用量上限（15:59 重置）中断**（config 深度默认值对账/克隆族谱八族完整矩阵/tests-flow 逐文件/前端切片余部+小 store 全文）；2 路完成（在飞件与模型注册表复核 ✅、错误码+WS 主题+HTTP 状态三专项横扫 ✅）。**主审亲自顶替完成了受阻路径的高价值子集**（tests 体系盘点、config 全键消费对账、队列 worker 克隆族丢失唤醒横查、videoPoller 全文、flow_trace 孤儿产地、model_registry P2 亲核）。未竟深读面在 §五列明。
> - 全程只读零改动（本报告为唯一新增文件）。

---

## 一、执行摘要（五问五答·第三轮口径）

**问1 方向**：维持 R1/R2 结论——没走歪、减法欠账、插件内核「研究原型」。本轮新增佐证：错误码体系横扫显示**体系骨架是好的**（108 码注册表+建议全覆盖+HTTP 恒 200 仅 3 处文档化例外），病在新增模块反复绕过体系（~40 野生码）——又一次「加法快于收敛」的实例，但属纪律病非方向病。

**问2 深扫增量**：R3 新 P1×1、P2×4、P3×~12；另复核新入库修复 1 笔（f995a99，放行）。

**问3 显存/装卸/ComfyUI**：R2 结论维持；本轮补充——**f995a99 正是这层的实战修复**（PuLID EVA 权重缓存自愈：备份+回灌+sha 双闸+原子替换，修法正确、三防线语义零改动、测试质量高），间接验证 R2 对 ComfyUI 恢复面的判断方向。新增一条 ComfyUI 域 P1（错码撒谎，见下）。

**问4 总账终版**：**未修 P1 = 21 条**（R2 的 20 + 本轮新增 1）；P2 ≈ 74；P3 ≈ 120；文档漂移再 +4（pytest.ini py310 注释、learn 专用 WS 端点登记、CLAUDE.md WS 口径、~35 注册零使用码的文档宣称）。明细见 R2 报告 §四 + 本轮增补。

**问5 整理**：方案维持（`docs/文档与目录整理方案-2026-09-20.md`），批2 勘误清单增补 4 条本轮新漂移。

---

## 二、本轮新发现

### P1（1 条，✅代理实证+主审复核）

**R3-P1 错误码 VIDEO_RESOLUTION_DEGRADED 已成 ComfyUI 万能码——出路文案对用户撒谎**
`src/services/inference/comfy_paint_engine.py` **20 处** + `h3_chain_engine.py:137,498,501` + `api/manga/video.py:1452`：ComfyUI 不可达/启动超时/执行失败/产物缺失/数据库不可用等**二十类不同故障**共用同一 code；其注册建议文案是「已自动降级；如需更高清可关闭其他功能后重试」——**降级并没有发生**，违反「降级必须诚实」铁律；且按 code 聚合的自愈/监控（record_failure）把 20 类故障记成同一信号（与 P2-8 舱壁误判同向放大）。修法：ComfyUI 域立 4~6 个真语义码（COMFY_UNREACHABLE/START_TIMEOUT/TASK_TIMEOUT/NO_OUTPUT/...），全量替换。

### P2（4 条）

| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| R3-P2-1 | **~40 个野生错误码未入册**：PLUGIN_*×10、NOVEL_*×15、CHARACTER_LORA_*×5、VOICE/VLLM/UPGRADE/UNBIND/LICENSE 等——批1 修过 11 个后新增模块（plugins 09-16、novel、character_lora）重蹈覆辙；仅拿通用兜底建议 | api/plugins.py 等 raise 点 vs error_handler.py SEMANTIC_CODES 108 码 | ✅ |
| R3-P2-2 | **_SEMANTIC_HTTP 映射死键**：写死 `"RATE_LIMITED": 429`，真实码是 `SYSTEM_RATE_LIMITED` 永不命中→限流响应信封 `meta.http_status` 计算得 200（真实 HTTP 层是 429）——监控解盲目标在限流场景自相矛盾 | error_handler.py:423 vs rate_limit.py:231 | ✅ |
| R3-P2-3 | **双死广播**：plugin_event（插件事件）与 update_available（检测到更新）每帧全员 send_json，前端零订阅零处理——广播侧无订阅计数，浪费不可观测 | bridge.py:36、main.py:320 | ✅ |
| R3-P2-4 | **损坏 manifest → 线上账实对账闸假绿**：解析失败缓存为 `{}` 且带 mtime 缓存→validate 对空 models 直接返回全空报告→main.py lifespan 记「模型账本与磁盘对账一致」（三计数全 0）——清单存在但损坏时整闸失明（主审亲核：model_registry.py:78-92/:200-205 + main.py:119-146） | src/data/model_registry.py | ✅ |

### P3（~12 条，紧凑）

①task_preview 前端死分支（useTaskStore.ts:187 订阅一个批4-1 已移除的后端类型）②learn/session/progress 专用 WS 端点零前端消费者（learning.py:624 每 2s 推帧入空；CLAUDE.md §4 仍把它列为 4 WS 之一——登记漂移）③dialog.py:826 后端下发前端专属码 FRONTEND_REQUEST_ABORTED（打破注册表「前端构造后端不下发」契约，分类撒谎无害）④同义双码：TRAIN_STYLE_LOCKED vs 注册码 STYLE_TRAINING_LOCKED、PARAM_INVALID vs SYSTEM_PARAM_INVALID⑤**~35 个注册零使用码**（BROWSER_* 全 10 码+TRAINING/LEARN/HARDWARE 大部）——文档宣称的浏览器错误码体系实际零落地⑥pytest.ini 注释仍写 py310 跑 run_tests（实际 py312）⑦f995a99 三瑕疵：反向播种不验 sha（坏缓存会播种坏备份→回灌被拒永久双缺告警，与其注释承诺相悖）、新测试无 smoke 标记（pre-commit 第 3 闸不执行它）、首次 816MB 回灌在 comfy_proc 锁内阻塞数秒（一次性）⑧model_registry remove_manifest_entry 非原子写（半途崩溃→直接落入 P2-4 假绿）⑨validate_against_disk 对 MODELS_DIR 外路径 relative_to 未捕获（一条脏 path 让整闸失明，有外层兜底）⑩dialog error 帧 TS 类型谎言（code 实为语义串、类型标 number，运行时未炸）⑪InspectorPanel 按中文 message 正则匹配错误（文案一改匹配碎）⑫flow_trace 每启 54 孤儿=上游调用方正常退出不收尾流程（flow_trace 自身按设计诚实标 orphan；30 天自动清理兜着）。

### 阴性结果（复核无恙，防下轮重复劳动）

- **config.yaml 全 60 键零死键**：逐键 grep 消费点，全部有消费（含 plugins.security_gate/model_cache.compression/gpu.feature_devices 等冷门键）——config 层防漂移现状良好。
- **队列 worker 克隆族丢失唤醒无扩散**：behavior=标准库 Queue（构造免疫）、novel=asyncio 单线程（无抢占窗口）、训练 style/lora=标准库 PriorityQueue——两锁窗丢失唤醒确认为 TaskQueueCore 独有（P1-6 不变）。
- **videoPoller.ts 干净**：可见性工厂+幂等注册+对称 stop，37 行无问题。
- **tests 三层体系健在且口径诚实**：pytest.ini 只收 tests/unit（设计如此+注释在案）；tests/flow 14 文件（harness+九域 cases）+4 个 e2e_live 脚本+golden 基线齐备，`--e2e` 才跑；backend/tests 旧目录已不存在的记忆口径已对上（e2e 已迁 tests/ 根）。
- **f995a99（PuLID EVA 自愈）修复放行**：纯插入不触碰三防线语义、原子回灌+sha 双闸、测试真断言且避免 816MB 进测试、registry 豁免两行是必需件（防 orphan 哨兵误报）——修法正确的实战样本。

---

## 三、在飞件复核详情（f995a99）

见 §二 P3-⑦与「复核无恙」。补充组合风险一条：未暂存的 `.githooks/pre-commit`（第 5 步）引用的 `tools/doc_drift_check.py` 仍 untracked——**若 pre-commit 先单独提交而漏 add 对账闸三件（脚本+claims+run_tests L0 步），他机克隆后钩子必炸**；建议第 5 闸批同批提交（已在整理方案批 2-6 登记）。

## 四、总账终版（截至第三轮）

- **未修 P1 = 21 条**：R2 §4.1 的 20 条 + R3-P1（ComfyUI 万能错码）。
- **P2 ≈ 74**：R2 的 ~70 + 本轮 4。
- **P3 ≈ 120**：R2 的 ~110 + 本轮 ~12（部分与 R2 重叠已合并）。
- **文档漂移累计 16+9**：R1 十二条+R2 五断点+本轮四条（pytest.ini/learn WS/零使用码宣称/HTTP 口径）。
- **今日累计修复入库 12 笔全部复核无回归**：09-19 六项+大考五项（P-06/08/11/14+reset）+f995a99。
- 修复梯队更新：R3-P1 并入「ComfyUI interrupt 两件」同一批（一次把 ComfyUI 域的错误语义收干净）；R3-P2-4 并入诚实性批（与 P2-3 make_dist 空名单同族）；R3-P2-2 一行修；R3-P2-1 建议做一次「野生码收编」小专项（~40 码入册+替换，半天）。

## 五、未竟面（诚实登记，15:59 限额重置后可续）

①config 键的**默认值/注释级**漂移深对（本轮只完成了键级消费对账）②克隆族谱八族完整矩阵（本轮只完成了 worker 族+错误码族+WS 族三个族，版本白名单/加密对/finally 清理/时间处理/锁配对/ComfyUI 轮询六族沿用 R1/R2 已有结论未重扫）③tests/flow 逐文件断链核查（本轮只盘点了存在性与口径）④前端 manga 切片余部（keyframeSlice/rowsSlice/assetSlice/voiceSlice/useMangaStore 组合层）+小 store 全文⑤DialogView/DialogPage 全文。这些面风险预期中低（所在域的兄弟代码两轮已扫），续扫优先级：④＞②＞①＞③＞⑤。

---

> **纪律声明**：本轮零代码改动零进程操作；4 路代理受账户限额中断如实披露并由主审顶替关键子集；所有 ✅ 项有行号实证，f995a99 复核基于提交后代码。
