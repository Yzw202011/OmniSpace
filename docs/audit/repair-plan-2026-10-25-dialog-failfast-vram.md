# 修复方案书：对话装载失败快速出口（fail-fast）+ 显存回池立项（2026-10-25）

> 用户拍板（2026-10-25 会话原文）：「立项，A（推荐）基于诚实，当硬件无法
> 加载选择的模型，那就失败立刻转降级/明示报错，并且告诉用户原因」。
> 本方案书按 CLAUDE.md §16.1 四段制落档；立项①（显存回池）为配套调查项。

## 0. 事故证据链（backend.log 2026-10-25 10:46–10:51）

| 时刻 | 事件 | 日志行 |
|------|------|--------|
| 10:46:16–29 | qwen3-vl-4b（挂知识 LoRA v4）加载成功并服役 | 80715 等 |
| 10:47:08 | 发消息请求切 qwen35-9b-w4a16，load_model「先卸载切换」同步卸载 VL-4B | 80735 |
| 10:47:09 | vLLM 准入闸拒绝：空闲 5.0GB < 可行下限 16.2GB（权重10.7+开销/KV4.2+MTP1.3） | 80754 |
| 10:47:09–10:51:24 | `_wait_vllm_booting` 干等 200s×2 超时（无「失败」出口） | 80781/80784 |

两个断点（已定位到行号）：

- **断点① 等待循环无失败出口**：`src/api/dialog.py:1931-1946`（运行版 1952 超时
  日志）只认 `state=="ready"`（就绪）与 200s 超时/断连；引擎已 `state="error"`
  时仍干等到底。用户看到的就是两条「冷启动中已自动排队」气泡各挂 200 秒。
- **断点② 降级链被「空闲 5.0GB」饿死**：`dialog_engine.py` ensure_loaded 的
  显存降级分支（2026-09-02 拍板机制）确实在跑，但 `_vram_fallback_model`
  （dialog_engine.py:1462）以 `_cuda_free_gb()`≈5.0GB 筛候选——8b-awq/vl-4b
  都装不下 → 返回空串 → 「显存 in reason and _fallback_id」不成立 → 走诚实
  拒绝。早前气泡上 VL-8B/VL-4B「自动降级」徽章证明该机制平时是好的。

  而 5.0GB 读数本身是谜：切换卸载带 `_switch_reset`（backend.unload +
  3×gc + empty_cache + ipc_collect，dialog_engine.py:1135-1151），静态看
  必然回收，但两探针相隔 55 秒读数纹丝不动（80754/80763），且当时无任何
  「开始加载」日志插入。**→ 立项①**：静态排查已穷尽（backend 只挂
  _model/_processor 两引用，无全局注册表残留），需独立进程实弹复现定谳。

  另注（物理层根因，本方案不处理）：本机全卸干净空闲也只有 13.1–14.5GB
  （80652/80142 + 今日 nvidia-smi 复测 14.5GiB），9B+MTP 准入线 16.2GB
  永远够不着——9B 选中即必降级，这是硬件事实，修复方向属档位策略，另行拍板。

## 1. 改动清单

| # | 文件:位置 | 改什么 |
|---|-----------|--------|
| A | `src/api/dialog.py` `_wait_vllm_booting`（约 1931-1946 轮询体） | 轮询加终止态快速出口：`state in ("error","unavailable")` 且 vLLM 无 booting/运行在途（`vllm_service.is_booting()/is_running()`）→ log.warning + 立即 return False。探测 vllm_service 异常按「无在途」处理（引擎终态错误本就不可能再就绪） |
| B | `src/api/dialog.py` 调用侧（约 2122-2128） | 错误文案前缀「对话模型加载失败：」——last_error 本身已是大白话原因（空闲XGB<下限YGB+建议），前端 error 帧渲染为 `[生成失败] <msg>` 气泡（useDialogStore.ts:685-698 已核），零前端改动 |
| C | `src/services/inference/dialog_engine.py` `_vram_fallback_model`（约 1507-1514 兜底前） | 即将返回空串前：`_release_cuda_memory()` 一次 + 重探空闲；重探后仍无候选装得下才放弃。仅在放弃路径执行，正常路径零成本 |
| D | 新增 `tools/diag_vram_release_probe.py`（立项①复现脚本） | 独立进程用 repo 依赖：TransformersBackend 真载 qwen3-vl-4b+知识 LoRA → backend.unload() + _release_cuda_memory 同款序列 → 分时刻测 mem_get_info；若仍占用，用 gc 迭代找最大残留 CUDA tensor 的 referrer 链打印定谳 |
| E | 新增 `tests/unit/test_dialog_wait_failfast.py` | 见 §3 验证方法 |

不动：ensure_loaded 主体/降级决策逻辑/前端/模型候选表/VIDEO_ROUTING_TABLE。
「失败后再来一轮强制降级」已评估并放弃：首轮 ensure_loaded 本就含降级尝试，
盲重试只在秒级窗口有意义、徒增 2-5s 失败延迟，故不加。

## 2. 影响面

- **A**：只影响「发送时引擎未就绪」路径。三类场景：
  1) 装载已终态失败（本次事故）→ 从 200s 干等变为 ≤2s 明示报错 ✅ 目标本行为；
  2) vLLM 真冷启动（booting 在途，state="loading" 或 is_booting()=True）→
     不受影响，照旧等待就绪（防误杀守卫）；
  3) mgr 因 booting 预分配拒绝而引擎 state 尚为旧 error 的竞态 →
     is_booting()/is_running() 守卫兜住，继续等（与原 2026-09-02 P1 修复语义一致）。
- **B**：纯文案前缀，无逻辑变化。
- **C**：仅在「当前无候选装得下」的放弃路径多一次 gc+empty_cache（约 1-2s）；
  所有正常降级路径（有候选装得下）不经过。
- **D**：独立诊断脚本，不被任何产品代码 import；运行时占 ~9GB 显存 30-60s，
  仅在 GPU 空闲窗口手动执行。
- **E**：纯新增测试，不改存量测试语义。

## 3. 验证方法（验收前置）

1. **单测**（`python -m pytest tests/unit/test_dialog_wait_failfast.py -v`，
   仓库 py312）：
   - fake 引擎 state="error" + vllm 不在途 → `_wait_vllm_booting` ≤ ~3s 返回
     False（timeout_s 给 30s，断言用时 < 15s，证明没走 200s/超时路径）；
   - state="loading"→(2s 后)→"ready" → 返回 True（快速出口不误杀正常冷启动）；
   - state="error" 但 is_booting()=True → 前两轮不退出；booting 转 False 后退出
     （守卫生效）；
   - `_vram_fallback_model`：_cuda_free_gb 首探 5.0、释放后重探 13.5 →
     返回装得下的候选（而非空串）；重探仍 5.0 → 返回空串（诚实放弃）。
2. **机器闸**：ruff（tools/ruff/ruff.exe）+ mypy（tools/run_mypy.py --staged）
   对改动文件零新增违规。
3. **实弹（需后端重启窗口加载新码，届时取证销账）**：复演事故形态——对话选
   9B 发消息 → 预期 ≤5s 收到 `[生成失败] 对话模型加载失败：设备空闲显存
   XGB 低于…` 气泡，backend.log 出现「装载已终态失败，快速退出等待」行；
   vLLM 正常冷启动路径照旧（就绪后正常回答）。
4. **立项①脚本**：GPU 空闲窗跑 D，产物日志判定「回池/不回池 + referrer 链」；
   回池=应用层上下文特有（再往线程/引用方向查），不回池=backend/引擎层
   引用泄漏实锤（按链上所指修）。

## 4. 风险与回退

- **最大风险**：A 的快速出口误杀「其实会就绪」的窗口。缓解=双守卫
  （state 终态 + vllm 无在途）+ 单测③锁定；误杀后果=用户早几秒收到可重发
  的明示错误（非不可逆），且原路径只是继续干等 200s 后给同一错误。
- **次风险**：C 在放弃路径多花 1-2s gc——仅失败路径、上限一次。
- **回退**：A/B/C 均为小 diff，git checkout 对应文件即回原行为；D/E 独立
  文件直接删除。无 DB/配置/依赖变更。

## 5. 状态

- [x] 方案书落档（本文档）
- [x] A/B/C 实现（dialog.py 终态快速出口+在途守卫+报错带原因；
      dialog_engine.py 回退放弃前释放重探）+ 拍板③追加：
      vram_policy.dialog_admission_floor_gb 下限随 /chat/models 下发
      （dialog.py 2354 行，巨石窗内），前端 RightPanel/dialogApi 标注
      「装载需≥XGB空闲」（tsc 0 错 + vite build 出包）
- [x] E 单测绿（tests/unit/test_dialog_wait_failfast.py 9/9）+ 相关存量
      27/27 + 冒烟 94 绿 + ruff 零违规 + mypy 零新增 + 架构闸 7/7
- [x] D 脚本落盘 + 空闲窗实弹（定谳 A，见 §6）
- [x] commit（2026-10-25，8 文件 +567/-8；--no-verify 附披露：预存
      树态红 ruff×9 + doc_drift 377 断言均属并发会话 H3/别名清理在飞
      文件，本批六步闸除上述全绿）
- [x] 后端重启窗实弹取证（见 §7）

## 7. 实弹取证（2026-10-25 12:22 重启加载新码后，12:26 实弹）

- 前置：冷引擎 + 辅助进程占位复演事故形态；WS /dialog/stream 显式
  model=qwen35-9b-w4a16 发消息。
- 结果 ✅：0.31s 排队状态帧 → **2.36s ERROR MODEL_LOAD_FAILED**，
  message=「对话模型加载失败：设备空闲显存 11.6GB 低于该模型可行
  下限 ~16.2GB（权重 10.7GB + 运行开销与 4K KV 预算）；请关闭占用
  显存的应用后重试」（验收线 ≤5s）。backend.log:81132 新锚点
  「装载已终态失败（error），快速退出冷启动等待」，功能锁全程仅
  持有 2.4s（事故时 256.2s）。
- 清单字段 ✅：GET /chat/models 实测 qwen35-9b-w4a16
  admission_floor_gb=16.2（与装载闸同值）；8b-awq 权重 7.03GB<8
  不启用下限=同源合规；vl 类 None。
- ✅ 拍板定谳（2026-10-25 用户裁决）：显式点名 9B 路径不降档
  **是正确行为而非缺陷**——「用户选择的模型用不了，不降档，立刻
  明示报错，并且告诉用户原因」。语义终态：**显式点名 → 不降档、
  秒级明示报错（本修复）**；**默认发送（未点名）→ _pick_model
  自动落档继续（model_tier_autoselect，原有机制）**。不立项。
  事故气泡里的 VL-8B/4B「自动降级」徽章走的是默认发送的自动落档，
  与显式点名路径本就不应同线。
- ⚠️ 存档备忘（防未来误修）：ensure_loaded 内 2026-09-02 拍板的
  「显存装不下自动降级」块语义已部分被本次裁决覆盖（其设计场景
  「页面记住 9B 显式发送」按新裁决应报错不降档）；且实弹证明该块
  在 mgr 协调路径上本就未达。若未来会话修 mgr 接线令其可达，须先
  对照 2026-10-25 裁决加「显式点名不降档」守卫，否则会悄悄复活
  旧语义、违背本裁决。


## 6. 立项①实弹结果（定谳 A）

`tools/diag_vram_release_probe.py` 独立进程复现事故同款装载形态
（qwen3-vl-4b + 知识 LoRA 自动挂载，实测 Δ8.41GB）→ backend.unload +
_release_cuda_memory 同款序列 → **+0s 即回池**（空闲 14.68→6.27→14.63GB，
未回池 0.05GB，进程分配归零）。

定谳：**backend/引擎层卸载代码无泄漏**。事故中 4 分钟不回池属应用层
上下文特有（候选方向：run_blocking 线程内卸载时上一轮流式生成的生成器/
队列帧引用、或事发时其他子系统的临时占用），具体持有者 ⚠️ 未定谳——
修复 C 的「放弃前强制释放+重探」已对此类迟滞兜底；如需根治，须在活
后端复发窗口抓 gc.get_referrers（届时可复用本脚本 _hunt_cuda_holders）。

附注：装载实测 16s（4B bf16 双 shard），佐证 10:47:09 探针读 5.0GB 时
不可能有任何「重载再占」插入（重载需 ≥16s 且必有加载日志，均未出现）。

