# 全身极致审计报告 — round-2026-09-20（逐层扫透·方向评估·文档漂移）

> **元信息**
> - 审计日期：2026-09-20；基线提交：`5e0e8d1`（HEAD）；工作区含并发会话未提交改动（第5闸收尾件 + convert_klein4b 重写版 + router/CLAUDE/INDEX 注释更新），全部按盘上版审计并单独评估。
> - 方法：10 路并行专项深扫（引擎层/对话链/队列调度/数据安全许可/API 面/插件内核/前端/启动链工具闸/测试诚信+文档漂移/架构方向）+ 主审亲核（昨日 9 笔修复逐条复核、全部机械闸亲跑、实况日志取证、**8 条最重新发现逐条亲读源码裁实**）。
> - 与昨日 round-2026-09-19 极审的关系：不重复立项，昨日 9 项未修 P1 + P2/P3 台账逐条核实在案；本轮增量=复核已修项质量、更深逐行、架构方向评估、文档漂移专项、第5闸实效评估。
> - 验证状态：✅=主审或代理追全链且主审亲核/复核；🔶=静态证据充分（附行号）；⚠️=环境依赖疑似。
> - 未实弹项：GPU 推理链、真浏览器走查、发行出包链（纯只读审计；审计期间用户实弹任务在跑——12:34 关键帧 4 镜生成全程避让）。

---

## 一、执行摘要（大白话总括）

**问题 1 的回答：整体技术没有走歪，方向是直的；真正的病是三处「明知是脂肪」的复杂度挂着方案书等拍板（减法欠账），以及一处「建好了没通电」（插件三层栈）。**

- **承重墙是直的**（证据见 §五）：Windows 进程生命周期工程（Job Object/单实例/看门狗/优雅退出）是桌面 AI 产品最容易翻车的地方，这里做得最扎实；TaskQueueCore「观察期→删 legacy」方法论已被 5166 任务零事故验证；反撒谎基建（对账闸/降级诚实铁律）本轮审计反复受益。
- **减法欠账**：video_engine 死路径（B3 计划 09-13 挂账至今）、CubeGPT+插件内核消费密度倒立（§五.4）、队列复刻成瘾（第 10 套 09-17 刚新增）。
- **新揪出的硬伤集中在四条暗线**：①「停止」语义（对话 transformers 后端停止≠真停）；②「看不见云」（page_guard 不看云任务→整机退出丢付费生成）；③「备份面漏风」（自动备份默认明文整库，P16 加密承诺被默认路径绕过）；④「发行链 py310 残留」（updater 硬编码 py310→发行包内升级必失败且回滚后不自启）。
> 注：刀二「video_engine 2754→~350 行」的收敛幅度被实测勘误为**高估约 2.2 倍**（死/休眠实测 ≈1100 行，generate() 本体是活路径）——不影响「该减」的结论，影响拍板预期（详见 §四.启动链域）。

**问题 2 的总账**：本轮**新发现 P1×7 ｜ P2×15 ｜ P3×26**（另测试诚信专项 + 文档漂移 12 条）；昨日台账未修项**全部核实仍在原位**（P1×9、P2 大部、P3 大部，含前端 P3-48/51 复核勘正）；昨日 6 项 P1 修复 + 大考 3 项修复**全部真修且未引入新病**（两笔夹带行尾翻转除外，见 §六.5）。前端契约面好消息：**全仓唯一断链=已知 P1-11，无同族新案**；训练中心三源接线确认健全。

---

## 二、机械闸基线（主审亲跑，2026-09-20 午）

| 闸 | 结果 | 备注 |
|---|---|---|
| ruff | ✅ All checks passed | — |
| L0 文档对账闸 | ✅ 24/24（warn 1） | warn=端点哨兵 **实测 378 vs 断言 376**；且 CLAUDE.md 在飞版写 360、claims why 字段自算 370≠361——**五个数同仓并存，防漂移闸自己的档案先漂了**（§六.4） |
| L1 pytest（py312） | 🔶 1142 绿 / **1 挂** / 9 跳 | 挂 = test_knowledge_sentinel 真库哨兵逮住**大考测试数据污染**（id 尾 58d63363「虚构测试数据」条目留真库）；9 跳全是显式环境闸 GPU 实弹（合法）。**非代码回归，属流程病**（§六.6）。269.8s |
| L2 vitest（便携 node 直调） | ✅ 101/101（2.49s） | run_tests L2 假挂已修（npm 兜底便携 node，盘上版核实） |
| mypy 第4闸 | ✅ 无新增（存量 229 冻结） | P1-14 修复持续有效：ini 健康解析 24 节、rc fail-closed 在位 |

---

## 三、昨日修复复核（9 笔全部真修，2 笔夹带行尾翻转）

| 项 | 复核结论 | 证据 |
|---|---|---|
| P1-14 mypy 闸 | ✅ 修实 | mypy.ini configparser 双模式解析零重复；run_mypy.py:32-50 预检 fail-closed + :114/:136 两处 rc 拦截；基线实测 229 |
| P1-15 torch 判定键 | ✅ 修实 | launcher.py:291/:296、check_torch_contract.py:49/:58 均 `'主运行时' in label`；残余：check_torch_contract.py:9 用法头注仍写 py310（P3） |
| P1-5 apikeys 装饰器 | ✅ 修实 | system.py:2143-2152 真路由 api_keys_list，返回真实行；_ensure_audit_table:1983 裸函数 |
| P1-4 character_lora import | ✅ 修实 | character_lora.py:140-142 `from ..services.character_lora_service`，containment 闸可达 |
| P1-7 云结果下载鉴权头 | ✅ 修实 | 两客户端 _result_headers 仅同源附 Bearer（cloud_video:63-79/cloud_image:65-81）；残余边角：base_url 无 scheme 时同源判定恒假→宁少带（fail-safe 方向，观察项） |
| P1-8 llama 自死锁 | ✅ 修实 | _stop_locked 拆分（:236-259），start 持锁内直调（:179/:228）；**但收养态另有一病见 P2-新-6** |
| 大考 P-06 气泡形状 | ✅ 修实 | 后端 BubbleLine 补 shape 五值枚举（models.py:580-582）+ 前端 override 绕闭包（ComicWorkspace.tsx:521/:826-831），双层根修 |
| 大考 P-08 预热卡死 | ✅ 修实（**夹带行尾翻转**） | draw/status 双栈语义正确（真实改动仅 25/1 行）；提交显示 1711/1687=**整文件 CRLF↔LF 翻转**（§六.5） |
| 大考 P-11 显存恒 danger | ✅ 修实 | _pct() 归一双口径兼容（system.py:646-653） |
| 03cfe5b reset_instance | ✅ 无新病 | 三契约自洽（零构造/锁序/幂等）；仅 P3-15 join(1.0) 残余窗口（昨日已知） |

---

## 四、本轮新发现（P1×7 / P2×14 / P3×26）

> 标 ✅ 的均经主审亲读源码裁实。行号=盘上当前版。

### P1 级（7 条）

**N-P1-1 ✅ 对话 transformers 后端「停止」不停真生成——僵尸 generate 与下条消息并发双 prefill（16GB 卡死条件被停止路径复活）**
`src/services/inference/backends/transformers_backend.py:340-361`（daemon 线程 `model.generate` + finally `thread.join(timeout=5.0)` 即返回释放推理锁）+ `dialog_engine.py:1797-1798`（stop_check 只 break 消费循环，无 StoppingCriteria、无 streamer.end()）。
触发：点停止 → 5 秒后放行 `_infer_lock` → worker 里 generate 继续跑满 max_new_tokens（可达数十秒）→ 立刻发下一条 → 同模型两路并发 generate——正是该文件注释自认的「实测 16GB 两路并发 prefill 卡死」。卸载同理假成功（显存实际不还）。vllm/llama/remote 路径经连接关闭可中断，唯 transformers 实锤。

**N-P1-2 ✅ 云端付费任务跑着时全页关闭 60s → 整机自动退出，生成结果丢失（资损链完整）**
`src/services/page_guard.py:63-98`（`_busy` 只查功能锁+图像/视频队列 `current/queued`）vs `src/services/video_queue.py:161`（snapshot 实有 `cloud_running` 键）；云道 dispatcher 全程不取功能锁（task_queue.py:355-375 锁只在本地 worker 道取）。
触发：提交云端视频/图片（分钟级计费）→ 关掉全部页面 → 60s 后 `_busy()==False` → /api/quit 正规链退出 → 云任务下次启动被清扫标 error，**付费调用作废**。BusyRegistry 正是为「云任务=系统非空闲」而建（gpu_budget.py:478-498）却无人接线到 page_guard。

**N-P1-3 ✅ 发行包内升级必失败且回滚后不自启（updater 硬编码 py310）**
`updater/updater.py:152-158`：拉起 Boot 用 `runtime/py310/OmniSpace-Boot.exe` 与 `runtime/py310/python.exe`——make_dist 批0-1 起包内只有 py312/py313（make_dist.py:137-142 注释自认）。
触发：发行包用户走应用内升级 → verifying_start 拉起两路径全缺 → Popen OSError → 空转 300s 超时 → 自动回滚（文件无损）→ 回滚里的 `_spawn_boot` 同样失败 → **用户面前软件保持关机**。与已知 P2-40（omnispace_exe.c）、P2-4（protect_build）同族第三处，建议三处并修。

**N-P1-4 ✅【盘上未提交件】convert_klein4b 转换器单流块映射整支死代码——产物静默损坏且自检假绿**
`tools/convert_klein4b_to_comfy.py:65-67`：`is_double = "transformer_blocks." in nk` 在 `replace("single_transformer_blocks."→"single_blocks.")` **之前**判定——单流键名天然含该子串 → 恒走双流分支，else 分支（`to_qkv_mlp_proj→linear1` 等，:86-92）**对任何输入不可达**；且 :96 `proj_out.` 替换无块级限定，单流块逐块 proj_out 碰撞覆写 `final_layer.linear.*`；六项自检（:173-178）全是双流/外层键，坏产物照打「✓ 全部验证通过」。另：验证失败仅 print 不设退出码（:182-185）。
（昨日 §六-5 的 QKV 静默丢弃/无残留审计/非原子写三旧病也原样在盘。）

**N-P1-5 ✅ 宪法自身自相矛盾：CLAUDE.md §0/§1 写 user_version=7，§5 写 13（实库=13）**
`CLAUDE.md:24/:51`（7，09-01 口径）vs `:230`（13）vs `src/data/database.py:486`（SCHEMA_VERSION=13，实测主库 PRAGMA=13）。§0 是每次会话必读检查清单——**教错价值最高的一处文档漂移**。同文件还有两处自矛盾：`:42` 写「9 个一级路由」vs `:86` 与 router.tsx 实测 11；§2.2 写「11 个 store」且列**已被清退的 usePaintStore**（实测 14 个，见 §六.4 漂移表 B3）。

**N-P1-6 ✅ 插件 invoke 超时处理器在事件循环上抢插件锁——一个卡住的插件冻结整个后端**
`src/services/plugin_runtime/registry.py:610-613`（`except asyncio.TimeoutError: with entry.lock:` 在事件循环线程）vs `:660-665`（工作线程 `with entry.lock:` 内跑 `on_think`）。
触发：进程内档插件 on_think 卡 ≥timeout_s → wait_for 只取消 await，线程仍持锁 → 超时分支在事件循环上 `with entry.lock` 阻塞 → **全部 API/WS 冻结**至插件线程自然结束（最长 120s，可 unload→invoke 循环续冻）。错误提示「unload 可复位」同样在 entry.lock 上挂死。

**N-P1-7 ✅ 恒真断言测试 ×4——批6 fail-soft 语义等被 `or True` 掏空（假绿测试）**
`tests/unit/test_batch6_features.py:32`、`test_character_lora.py:166`、`test_power_guard.py:59`、`test_debug_log_sweep.py:46`（grep 原文核实）。批6「errors 无 training: 前缀」与「回滚后 v2 非当前」两条验证被完全掏空。另（🔶）：TaskQueueCore 测试把竞态面 mock 掉（`_acquire_paint_lock` 恒 True / `_thermal_paused` 恒 False），**昨日 P1-6 丢失唤醒在结构上无回归测试可测**；license 回拨测试只测单次超窗，P1-1 锚点滑动场景无用例。

### P2 级（15 条）

| # | 条目 | 位置 | 状态 |
|---|---|---|---|
| N-P2-1 | TrainingLockGuard 双取+fire-and-forget 释放：事件循环阻塞 >10s 时 acquire 超时降级双入账 / release 不等结果——training 锁永久残留 → 全功能互斥卡死（image/video 已修的同型漏网克隆） | training_common.py:50-79 vs image_queue.py:259-261 正确写法 | ✅ |
| N-P2-2 | 自动备份默认明文整库落 data/backups/（对比手动链 encrypt_bytes_gcm→.db.enc）——remote key 明文行/lan token/审计日志随库外带，P16 备份加密承诺被默认路径绕过 | system.py:1779-1789 vs :300-305 | ✅ |
| N-P2-3 | TrustedHost 白名单硬编码回环三址——`OMNISPACE_ALLOW_LAN=1` 下远程浏览器 Host=LAN IP 被 400 拦在 LanAuth 之前，**LAN 远程功能结构性不可达**（deployment-manual.md:152 仍在宣传该模式） | main.py:538-542 + config.py:63 | ✅ |
| N-P2-4 | 升级中断恢复：部分文件还原失败仍置 phase=rolled_back——混合版本永不重试静默运行，下次升级备份的又是混合态 | boot.py:1272-1286 | ✅ |
| N-P2-5 | splash 让位路径无 copy_id 校验（守卫只比 build_id，开发版恒空串互认）——同机兄弟副本 B 让位后直开 A 的后端 UI（看错数据），批2-4 跨副本闸被自家兄长旁路 | boot.py:716-731 vs :986-996 | ✅ |
| N-P2-6 | llama 收养态进程不被 stop 接管：收养置 ready 但 `_proc` 恒 None，`_stop_locked` 对 None 直接 return True——~5.7GB 显存滞留、vLLM 准入持续被拒（对照 vllm 收养态有 _reap_orphans） | llama_service.py:140-156/:244-247 | ✅ |
| N-P2-7 | VideoEngine.generate：H3 判定之前先跑多视角裁剪（装 qwen2-vl-2b ~4-5GB）——「H3 就绪免 VL」守卫被击穿，VL+H3 峰值叠加挤兑 16GB | video_engine.py:2027-2033/:1823-1841 | 🔶 |
| N-P2-8 | encoder ffmpeg 超时仅在 stderr 有行时检查——ffmpeg 静默挂死则 1800s 超时永不触发，视频任务恒 generating | encoder_service.py:590-604 | 🔶 |
| N-P2-9 | 视频云端路径「先落库 pending 后校验首帧」——校验失败留下幽灵 pending 行，该分镜后续生成全被连点闸拒绝直到手动取消（H3 分支同位置是先校验后落库） | api/manga/video.py:1200-1216→1245-1251 | 🔶 |
| N-P2-10 | /models/{id}/files 对登记的任意绝对路径整目录 rmtree——叠加已知 P3-46（/models/import 接受任意绝对路径）= 导入登记+前端「彻底删除」两步误删任意用户目录 | api/models.py:1575/:1598-1599 | 🔶 |
| N-P2-11 | 纯数据档（user_data）llm.provider 插件无确认门即任意 URL 出口：宿主向 manifest 指定的任意 URL POST 任意 JSON（内网探测/外传），Authorization 附 CUBEGPT_LM_KEY，响应原样返回 | api/plugins.py:284-290 + cutemamen/llm_provider.py:98-106/:130-133 | ⚠️ |
| N-P2-12 | WS 冷启动等待断连检测恒假：`websocket.client_state == 1` 与本环境 starlette 枚举（DISCONNECTED=2）错位=死代码——客户端等待期断开永不被发现，干等满 200s 持锁 | api/dialog.py:1875-1876 | ✅ |
| N-P2-13 | 系统侧上下文组装无截断：sys_content（RAG+联网+插件注入叠加可达 11K+ token）不进预算——vLLM 400/prefill 颠簸，对话直接失败 | dialog_engine.py:1679-1696 | 🔶 |
| N-P2-14 | 加密备份永不清理且 API 删不掉：自动清理 glob 与删除端点后缀校验都不认 .db.enc/.json.enc（restore_list 却列得出）——密钥可用后手动加密备份无限堆积 | system.py:1797/:515/:555-557（=昨日 P2-5 的完整根描） | ✅ |
| N-P2-15 | 批删会话中途失败→UI 与后端分裂：最终 set 只在全部批次成功后执行，第 1 批已删但前端列表原样保留，点击即 404 直至下次拉取 | frontend/src/stores/useDialogStore.ts:287-305 | 🔶 |

### P3 级（26 条，紧凑）

**队列/治理**：①character_lora worker 空队列即退线程+is_alive 滞后窗→单提交滞留（character_lora_service.py:265-270/:236-239；char_lora_tasks 也不在 recovery 清扫表）②一键体检队列项读不存在的 pending/running 键→恒显「队列空闲」（health_check.py:219-221）③self_heal「防风暴三闸」缺上限闸（MAX_AUTO_ATTEMPTS 无判定拦截，self_heal.py:151-173）④云道 dispatcher 弹出→登记两锁窗 cancel 落 missing 任务照跑（task_queue.py:357-372）⑤多卡 active_feature 只回主卡域持有者（feature_lock.py:148-154，双卡环境限定）⑥style_lora 暂停自旋 0.5s 一次 DB UPDATE 且持 training 锁（style_lora_service.py:587-595）。
**引擎**：⑦vllm `_kill_locked` taskkill TimeoutExpired 未捕获（vllm_service.py:1041-1045，与 llama 修法不对称）⑧comfy_paint 双 PuLID 第二身份图泄漏 input 目录（comfy_paint_engine.py:751-753/:800-806）⑨paint_engine.generate 状态检查在推理锁外（paint_engine.py:1266 vs :1283）⑩h3_chain 多镜 ETA 按单镜计——慢机长链被提前误杀（h3_chain_engine.py:511/:575-576）⑪dashscope 200-非JSON 裸抛 JSONDecodeError 无出路文案（cloud 两客户端）。
**对话**：⑫WS 被动补全硬编码采样参数丢弃用户设置（dialog.py:2155-2157）⑬ws_hub 广播慢客户端队头阻塞无隔离（ws_hub.py:214-226）⑭会话列表 N+1 全量解密（dialog.py:1512-1549）⑮WS 附件原始 base64 直接落库（dialog.py:2088-2089）⑯云端对话停止即漏记账（remote_backend.py:331-333）。
**API 面**：⑰comic script-generate replace=true 清行不级联（comic_script.py:141-142，对照 storyboard.py:204-241 有级联先例）→ keyframes/video_tasks/MP4 永久孤儿（video_tasks 无 project_id 连删除级联都找不到）⑱知识图片删除孤儿文件（knowledge.py:428 vs knowledge_service.py:1244-1267）⑲learning 设置裸 int 可 500+预算无上界（learning.py:738/:757/:501-510）⑳删学习主题不避活跃会话不级联子表（learning.py:247-268）㉑知识 CSV 导入非原子半截+中途 500（knowledge.py:670-678）㉒/logs/raw 全文件读入内存后截断（logs.py:195/:198）㉓storyboard 全量保版权删除行不取消在跑任务（storyboard.py:226-228）。
**数据/安全**：㉔/system/dialog-remote/test 任意 URL 探测面（SSRF，LAN 模式放大）（system.py:227-229）㉕/license/status 白名单内泄露 licence 绝对路径（impl:481，昨日 P3-4 加重项）。
**工具链**：㉖打包台预检版本恒 "?"（读已不存在的 backend/config.yaml）+cancel 竞态 AttributeError+`cands[-1]` 未排序取错目录（packaging_console/preflight.py:216/orchestrator.py:131-139/:218-226，冻结范围自身 bug）。

---

## 五、技术方向评估（问题 1 的正面回答）

### 5.1 真实架构全景（实测）

- **进程/运行时**：七品牌进程（Boot/Backend/Shell/BootC/BackendC/LLM/Engine），**6 套运行时并存**（py310 回退锚/py312 主链/py313-vLLM/py313-ComfyUI/node20/ffmpeg+llama.cpp），其中 3 个带 CUDA 上下文。16GB 共存矩阵：9B vLLM 峰值 14.9GB 近满卡；H3 ~12GB 与 4B 不可驻留；**让渡=杀 vLLM 子进程（真 sleep 8921 次失败后回退），每次让渡=全量重载权重、切换以分钟计**——这是 16GB 单机的物理约束，不是设计失误，但切换税是真实体验成本。
- **队列**：实测 **10 套**（CLAUDE.md 记 9 套已漂移）——TaskQueueCore 芯+图像/视频（已归口）+小说+训练×3（第三套 09-17 复刻）+行为+浏览器+对话位次表。训练三套与 Core 零关联、~90% 同构克隆。
- **对话引擎矩阵**：5 后端（vllm/llama/gguf/remote/transformers）+ 三段降级链（档位→4B inproc→云）；绘画=diffusers+ComfyUI(+PuLID)+云三道；视频=H3 链（锁定令）+云+Ken Burns 兜底；语音=SAPI5+whisper-tiny（四套 AI 语音权重全门控，诚实）。
- **体量**：src/ 92,755 行（services 47%>api 31%）；前端 43,078 行/150 文件/14 store+5 切片；测试 190 文件/collect 1152。最大文件：keyframe.py 2887、video_engine.py 2754、comic_asset.py 2529、system.py 2469、dialog_engine.py 2217；前端 ModelManager.tsx 1549。

### 5.2 逐项判断

| 方向 | 判断 | 证据要点 |
|---|---|---|
| 自维护 vLLM+双 Python | **说得清（偏对）** | Windows 无官方轮子，这是 16GB 跑 9B 的唯一路径；付息被控制在 vllm_service.py 单文件+torch 契约闸内；sleep 学费（8921 次失败）已付并文档化 |
| 三推理后端并存 | **vLLM+云对；llama/gguf 第三腿存疑** | 降级韧性真实且诚实；但 gguf 旁路专为 12GB 降级档服务，产品基线已上移 16GB——若 12GB 用户占比低，这是最可砍的一层（砍前需用户占比数据） |
| 插件三层（CuteMamen+OSP+CubeGPT） | **OSP 对；内核+主模型「没走歪但没通电」** | 接线质量是真的（gate 全覆盖无第二入口、技能插座三端到端、tar 安检完整）；但消费密度倒立：CubeGPT 2132 行只被 cutemamen+17 个测试引用、产品 UI 调用点=0；出厂唯一插件零 skills 声明→出厂态整套插座空转；rust_coding 是「主模型→迁移管线→插件」的自我演示品。**给 rust-coding 补一行 SKILLS 声明即可三层贯通；若三个月无人补，内核层+主模型应按死重重新裁决** |
| 队列不统一 | **走歪了但已在回头** | TaskQueueCore 方法论被 5166 任务零事故验证；病根=「加法快于减法」惯性（character_lora 09-17 复刻是最新病例）；刀三就是收编路线图 |
| SQLite+字段加密+三库 | **对** | 单机卖断制正解；迁移组 append-only 有纪律；无可指摘 |
| 前端 14 store+自研库 | **撑得住，接近上限** | Omni 库仅 18 件/2623 行非负担；负担是 5 个 ≥900 行巨组件+6.7% 测试覆盖（刀一护栏先行顺序正确） |
| 发行链 | **完备度较高，单点在人** | 发码/激活/吊销二期/打包预检/升级签名全在位（keys/ 零密钥入 git 实证）；**风险=锁芯只存在于开发机（bus factor 1）**+本轮 N-P1-3 升级链 py310 三处残留 |
| 反撒谎基建 | **对，且本轮反复受益** | 对账闸/哨兵/降级诚实铁律让文档与代码偏差多数在 ±3% 内——但闸自身档案也在漂（§六.4），需按 §七.4 补强 |

**复杂度付息过高 Top3**：①video_engine 死/休眠路径（实测 ≈1100 行，收敛目标勘误见下）；②CubeGPT+为 1 个演示插件维护的整层内核；③队列第 7-10 套+draw.py 24 端点中 21 个无消费。
**走对了 Top3**：①Windows 进程生命周期工程；②TaskQueueCore 收敛方法论；③反撒谎基建。

### 5.3 对瘦身三刀方案（待拍板件）的勘正

- 刀一（前端测试网）：事实基础准确，无勘正。
- 刀二（B3 减肥尾巴）：**draw.py 24→3 端点属实（前端仅用 /draw/status、/draw/models、/draw/result，paintApi.ts:16/30/34 实证）；但 video_engine「2754→~350 行」高估约 2.2 倍**——实测硬死码（AnimateLCM 族+路由守卫锁死）≈520 行 + 条件休眠（仅手动 /models/load 可达的 diffusers 主体）≈580 行，generate() 本体（H3 分派/Ken Burns/VL 增强）是活路径。**实际可收敛到 ≈1650 行，不是 350**。结论「该减」不变，幅度预期须校准。
- 刀三（训练三队列收编）：三套队列行号与重复度全部核实准确；补充：character_lora 还有 worker 空即退+recovery 不扫两张配套病（N-P3-1），收编时应一并治。

---

## 六、专项深挖

### 6.1 已知台账核实（昨日极审未修项）

- **P1×9 全部仍在原位**：P1-1/2/3（许可三件，数据安全路复核并细化触发路径——P1-1 锚点滑动需「恒慢钟」长期运行才成立，逐日回拨第 2 天即被 saved_at 小时节流拦住，**触发成本高于昨日描述但仍成立**）、P1-6（队列丢失唤醒，✅主审亲核两锁窗原文）、P1-9（恢复后不重迁）、P1-10/11（前端，见 §六.7）、P1-12/13（stop/launcher 归属判定，启动链路 15 项已知逐条核实全部原样）。
- **P2/P3 大部仍在**：各路代理逐条核实（P2-8/9/10/12/13/14/16/17/18/20/21/22/23/24/25/26/27、P3 共 ~40 条全部在案原样）；P2-11（云视频成功记账死代码）行号更新为 cloud_video_client.py:217-224 仍在。
- **已变形/缓解**：P3-31（comfy taskkill）原病灶不在，新形态=taskkill 自身可无限挂起（低概率）。

### 6.2 实况运行证据（在飞后端日志取证）

- **RAM 压力是常态**：真实绘画负载下资源守卫 77%→84%→90% 危急线反复触发、「无空闲模型可卸」只能 gc/工作集收缩（12:32-12:34 实录）——历史上三次静默死亡即 RAM 提交耗尽（WER 实锤），当前靠 shed_memory 活着但贴边运行。**RAM 是比显存更紧的长期约束**。
- **flow_trace 每次启动标 54~55 个孤儿流程**（连续两次启动数字几乎相同）——正常退出也不闭环，流量库数据质量病（与错误面板污染同族）。
- **stderr 10.5MB**：大头是 diffusers tqdm 进度条整段刷入 backend_stderr.log（每任务一次）+「Guidance scale ignored」重复告警——日志噪音/磁盘churn，建议 tqdm 禁用或降频。
- 近两日后端错误仅 32 次 ApiError+1 次任务失败，无崩溃签名、无看门狗重启——**生产稳定性事实良好**。

### 6.3 测试诚信专项

- **假绿清零面**：try/except-pass 包断言 0、无理由 skip 0、xfail 0、「mock 被测物再测 mock」未发现。
- **但 4 处恒真断言**（N-P1-7）+ TaskQueueCore 结构性盲区 + license 回拨覆盖错位 + knowledge_pipeline 纯 happy path 且带 5~50ms 性能断言（flaky 源）。
- **单例复位口只护 dialog 域**：conftest 仅复位 dialog_engine，src 内 11 个 `instance()` 单例（ws_hub/feature_lock/encoder/thermal_guard 等）无复位口——测试顺序耦合残余面。
- 强覆盖确认：upgrade/plugin_runtime/database 迁移/crypto/feature_lock 五模块失败路径扎实。

### 6.4 文档漂移专项（12 条对照，全部双侧行号）

| # | 文档说法 | 代码实况 | 严重度 |
|---|---|---|---|
| B1 | CLAUDE.md:24/:51 user_version=**7** | database.py:486=**13**，实库 PRAGMA=13（主审亲测） | P1（=N-P1-5） |
| B2 | CLAUDE.md:42「9 个一级路由」 | 11（router.tsx:3/:62-75） | P2 |
| B3 | CLAUDE.md §2.2「11 个 store」+列 usePaintStore | **14 个**（主审亲测 ls）；usePaintStore 已清退不存在 | P2 |
| B4 | CLAUDE.md §4「360 端点/18 路由模块」 | **378**/**21 模块**（main.py:445 _API_MODULES 主审亲测） | P2 |
| B5 | doc_claims.json:27 why「实测360→361」「get149+post164+put31+delete26」 | 分项和=370，断言=376，实测=378——**why 自身算术错** | P3 |
| B6 | README.md:49「376 端点（对账闸实时核对）」+「pytest 1132 用例」 | 378（warn 不挡）；def test_=1137/collect=1152 三口径 | P2 |
| B7 | CLAUDE.md:53 队列「9 套…训练×2」 | **10 套训练×3**（character_lora 09-17 新增） | P3 |
| B8 | 表数四口径：32（§0/INDEX）｜41（§5）｜42×2（database-er/对账总表） | 主库物理表 **43**（含 FTS 影子 6；主审亲测）；schema 正源 23 张+散建；**3D 已剔除但 director_stages/cameras/characters 三张死表仍在实库** | P2 |
| B9 | INDEX.md:24「360」 | 378（昨日刚写即漂） | P2 |
| B10 | architecture-overview.md「15 模块/319 装饰器」 | 21/378，落后两轮对账无横幅 | P3 |
| B11 | api-endpoints.md 行号级小漂 | hardware.py:381/learning.py:624 | P3 |
| B12 | troubleshooting.md:145 排障命令指 py310 | 主链 py312 | P3 |
| 补 | 版本锚七项全对 ✅（torch/vLLM/FastAPI/React/Zustand/py312/router）；INDEX 现行文档登记 8/8 在位；README 功能宣称抽 8/8 结构在位 | — | ✅ |

### 6.5 行尾债（.gitattributes 说好的 renormalize 一直没做）

`.gitattributes`（09-18 立，`* text=auto`）注释自认「存量待安静窗口一次性 renormalize」——至今未做：**index 里仍压 102 个 CRLF 文件+6 个混合文件**。代价已现：03cfe5b（dialog_engine 真实改动仅 31 行）与 7775112（draw.py 真实改动仅 25/1 行）两笔修复提交都**夹带整文件行尾翻转**（1711/1687、2217/2186 的 diff 全是噪音）——blame 污染、review 噪音，且未来会继续随机爆。建议：安静窗口做一笔专用 `git add --renormalize .` 提交（用 --ignore-cr-at-eol 验证零内容变化）。

### 6.6 流程病：实弹测试污染生产数据（三例同族）

①大考知识条目「虚构测试数据」留真库（pytest 哨兵逮住，待备份后正规删除——**本次审计未动，属拍板项**）；②flow_trace 每启 54~55 孤儿；③历史错误面板污染。同根：**测试态与生产态共库**。建议方向（待拍板）：大考专用测试项目标记+收尾自清脚本，或测试走独立 DB 别名。

### 6.7 前端深扫（重派版，全仓 ~200 条路径字面量对照后端路由）

**契约断链横扫结论：全仓唯一断链=已知 P1-11（GlobalSearch `/models/list` 死端点+吞错），无同族新案**；其余全部路径（chat/novel/comic/manga/style/learn/knowledge/behavior/training/license/logs/plugins/system/hardware/cloud/upgrade/browser/character/art + 3 WS）逐一命中后端路由。voice 线索澄清：**前端零 `/voice/*` 调用**（音色全走 /manga/voices/*）——后端 voice.py 死端点族属纯后端死重+文档谎言（§四 P3 域），前端无感。

**已知项核验 10/10 全部仍在**（行号刷新）：P1-10 竞态族四点（useDialogStore.ts:233-262 / useNovelStore.ts:149-177/:243-250 / manga/projectSlice.ts:139-152）、P1-11（GlobalSearch.tsx:50，模型源永久静默空）、P2-28（app.css:2360-2362 hover 块解析成后代选择器永不命中+`.chatic-bubble` 拼写错+opacity 覆盖）、P2-29（videoSlice.ts:279-280 失败路径无条件 release，`:188` 同族；成功路径 :38-47 有 busy 检查证明作者知道正确写法）、P2-30（ws.ts:157-168 onclose/onerror 无 socket 归属守卫→双活）、P2-31（LicenseGate.tsx:57-64 闭包钉死 open=false 永空转）、P2-32（useDialogStore.ts:584/:597-600 从未 open 的 WS 无兜底→generating 永真）、P2-33（QuotaCard.tsx:24/TopBar.tsx:258/learnApi.ts:115,134 裸 as 残留；dialogApi/mangaApi 已改 unknown+手工收窄=改善中）、P2-34（四处裸 fetch 漏 X-Omni-Token：DialogPage.tsx:109-113/LogSupportWidgets.tsx:62/videoSlice.ts:68/ArtSegmentDialog.tsx:40）。

**新发现与勘正**：①[N-P2-15] 批删会话中途失败→UI 与后端分裂（useDialogStore.ts:287-305 最终 set 只在全部批次成功后执行，第 1 批已删但前端列表原样保留，点击即 404）；②[P3-48 复核勘正] 全局 Alt 快捷键不避让 input/textarea（App.tsx:266-292 仅排 isContentEditable，输入中 Alt+N 打断草稿；:277 吞 createSession 失败零提示）；③[P3-51 实况勘正] 懒加载全路由零 errorElement（懒加载真身在 router.tsx:46-51 非 App.tsx；升级后旧 chunk 404=英文堆栈页无中文重载引导）。

**复核无恙**：TrainingPage 三源归一接线**健全**（trainingApi.ts:69-92 手工归一+单源故障如实披露不伪装空队+后端 training.py:65 fail-soft 对应；5s 轮询有卸载清理）——09-17 记忆中「训练核心条件通路待接线 fail-closed」的疑虑可以销账；videoSlice 轮询收敛链（终态三分支+3 失败熔断）、projectSlice 删除清理、ws.ts 帧信封 Zod+退避上限+pending 上限、localStorage 12 键无冲突——在位。store 实测 **14 个 + manga 切片 8 文件**（6 切片+helpers/types）。

---

## 七、修复优先级建议（供拍板，未动任何代码）

**第一梯队（资损/闸门/必现死）**
1. N-P1-2 page_guard 接入 cloud_running+BusyRegistry（资损面，改一处 _busy）
2. N-P1-1 transformers 停止语义（StoppingCriteria/kill 旗标；vllm 已有正确范式）
3. N-P1-3+P2-40+P2-4 三处 py310 残留并修（发行升级链一口气治）
4. P1-6 丢失唤醒（出队与 wait 合并同锁窗——同文件云道现成模板）+ 补真实竞态回归测试
5. N-P1-7 恒真断言 ×4 修正（一分钟级）+ N-P1-4 转换器 is_double 判定挪位（**注意：该文件是并发会话在飞件，须协调后动**）
6. N-P1-5/B1~B4 宪法勘误一批（user_version=13/路由 11/store 14/端点 378/模块 21/队列 10）+ 端点哨兵 expected 376→378 且重写 why——**让第5闸的档案先诚实**

**第二梯队（安全/稳定）**
7. N-P2-2 自动备份接 P16 加密 + N-P2-14 .enc 清理链
8. N-P1-6 插件超时处理器改非阻塞标记（如「置 faulty 由锁外线程做」）
9. N-P2-1 训练锁对齐 image_queue 的 fut.result 语义
10. N-P2-6 llama 收养态 stop 补 _reap_orphans
11. 昨日第二梯队存量（P1-12/13 归属判定、P1-9 恢复重迁、许可三件套签名化）
12. N-P2-3 TrustedHost 动态白名单（LAN 模式放行本机 LAN IP 或 ALLOW_LAN 时加 "*" + 依赖 LanAuth 令牌闸）——若 LAN 模式不再卖可改为文档明示废弃

**第三梯队（体验/卫生）**
13. 行尾 renormalize 专用提交；stderr tqdm 降噪；flow_trace 正常退出闭环；N-P2-9/10/12/13；P3 批量清账；瘦身三刀按勘正后幅度拍板执行

**方向性拍板项（不催，只呈）**
- rust-coding 补 SKILLS 一行让插件三层通电 vs 三个月后按死重裁决 CubeGPT/内核层
- gguf/llama 第三腿去留（取决于 12GB 用户占比数据）
- 测试态与生产态隔离方案

---

## 八、复核无恙面（防下轮重复劳动）

- 历史病历全部在位未复发：vLLM 热切换准入（锁内杀后复测）、comfy 空闲 300s 三层防线、feature_lock RLock 全临界区、stats 时区、加密读写对称、SQL 注入面 0 新增（60+ 处动态 SQL 全参数化复核）、上传面 10 端点白名单/魔数/有界读在位、XSS 面零 dangerouslySetInnerHTML、CORS/origin guard 在位。
- 升级包校验链（zip-slip 双防线+Ed25519+逐文件 SHA256）、恢复文件名白名单、/manga/media 五目录 containment、comic_asset 三上传全链安检——在位。
- model_manager P0-5 锁序、periodic/scheduler 无重叠执行、gpu_budget 三单例双检锁、BusyRegistry 幂等——在位。
- h3_chain "final in sub" 判据闭环（34 节点模板核对）；gen_router 词法遮蔽/白名单/RAM 闸无新发现。
- 插件 tar 安检（内存直读/条目白名单/64MB/256MB 解压炸弹闸/npz allow_pickle=False）+ gate_plugin_call 两路径前置无绕过 + chat 注入面 ≤1条×8K 即焚不落库——在位。
- 版本锚七项全对；INDEX 现行文档 8/8 在位；README 功能宣称 8/8 结构在位。
- 大考三修复+昨日六修复全部真修无回潮（§三）。

---

> **审计纪律声明**：本轮零写盘（本报告为唯一新增文件）、零进程操作、零测试执行（机械闸除外，pytest/mypy/vitest/ruff/对账闸为基线取证必需）；审计期间用户实弹任务在跑（12:34 关键帧生成）全程避让；并发会话在飞件（第5闸+转换器）按盘上版审计并单独标注。所有 ✅ 项经主审亲读源码裁实。
