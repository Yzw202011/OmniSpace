# 第二轮极致深审报告（R2）— round-2026-09-20（显存调度/ComfyUI 专项·方向复评·全问题总账）

> **元信息**
> - 审计日期：2026-09-20（下午，第一轮 round-2026-09-20-full-stack-audit.md 收官后接续）；基线提交：`fbfafec`（HEAD，大考会话新入 P-14）。
> - 方法：8 路专项深扫（显存调度全链 / ComfyUI 协作链 / vLLM 让渡编舞 / 训练学习知识服务层 / 前端大组件二轮 / core·cutemamen 内核逐行 / 文档目录盘点 / 目录资产盘点）+ 主审亲核 6 条最重发现（STDP 死代码、负样本正训、显存三 P1、VirtualList 指数膨胀、ComfyUI 超时不中断、转换器单流死代码）+ 上游联网查证（vLLM sleep Windows 状态）。
> - 配套交付：**整理方案另册**=`docs/文档与目录整理方案-2026-09-20.md`（批1~批5，待拍板）。
> - 全程只读零改动（本报告与整理方案为仅有的两个新增文件）；审计期间大考会话活跃（新提交 fbfafec、后端重启过一次），全程避让。

---

## 一、执行摘要（五问五答，大白话）

**问1 方向走歪了吗？——没走歪，但有一条判断要加重。** R1 说插件三层栈「没走歪但没通电」；R2 逐行读完后必须说得更重：**内核的「学习」叙事是死装饰**——STDP 学习全局死代码（权重更新恒 0、指标恒 0 掩盖）、trainer 监督更新写进前向永远不读的死参数、跨进程复现承诺在路由维度失效。定性=「工程化外壳包着研究原型内核（半工程下缘）」。这不改变「OSP 插件运行时走对了」的判断，但把「补一行 SKILLS 通电」的预期收益打了折：通电后能用的是插件框架，不是脉冲主模型的学习能力。其余方向判断（进程工程/队列芯/SQLite/发行链=直的；减法欠账）维持 R1。

**问2 更深扫的结果？——R2 新 P1×4、P2×21、P3×26**（详见 §三/§四；与 R1 累计后未修 P1 共 20 条，§四全清单）。

**问3 显存调度/模型装卸/ComfyUI 有没有问题？有没有更优方案？——有问题，而且是「三层病」：**
- **机制层有 3 个真 bug（P1 级）**：准入台账预留「装载成功后永不回落」→ 在载模型被物理+台账双重扣减（系统性过早拒绝/驱逐）；paint 的统一准入口裁决被丢弃（paint 侧=无准入）；同模型并发装载双预留泄漏直至重启。
- **结构层是「13 个管家、4 把尺子」**：同一时刻「空闲显存」有四个互不相等的数（mgr 台账口径/budget 物理口径/vLLM 闸 torch-only 口径/vram_manager 本进程口径），统一账本 gpu_budget 2026-09-10 建成后停在顾问模式，收编半途。
- **物理层是 Windows 现实的税**：让渡=杀 vLLM 子进程→唤醒=全量重载 ~157s；真 sleep（L1 权重挪内存，唤醒快 18~200 倍）接线已就绪，唯一堵点=上游 Windows 轮子缺符号（2026-09 上游仍无 Windows sleep 支持，已联网查证）——「杀进程制」在当前是正确取舍，不是走歪。
- **ComfyUI 协作有两个 P2**：超时/取消不 interrupt（僵尸任务级联拖死后续+保活空闲守卫）；绘画「运行中取消」是假取消（完成后状态还覆写成 done）。另有崩溃残留无启动收账（实体已泄漏 144MB）、复用不验实例身份。
- **更优方案存在且分四层**（完整论证见 §二.4）：立即修 bug 级 8 件 → 拍板执行 V9 显存编排统一层（项目自己已立案）→ 挂账等上游（vLLM 升级后一行 config 重验真 sleep）→ 减负才是真优化（刀二死路径/gguf 第三腿裁决/RAM 治理）。**诚实的边界：16GB 单卡跑三线，互斥+让渡是物理必然；目标不是消灭让渡，是让让渡更快、更诚实。**

**问4 全问题汇总评级——§四总账**：P0×0｜**未修 P1×20**（全清单枚举）｜P2 ≈70（分域表）｜P3 ≈110｜文档漂移 12+5 断点。已修项（09-19 六项 P1+大考四项+reset_instance）全部复核无回归。

**问5 文档与目录整理——方案成册待拍板**：`docs/文档与目录整理方案-2026-09-20.md`（批1 零风险减法 ~2G → 批2 文档归位+宪法勘误+防漂移断言 → 批4 结构约定 → 批5 加法补缺 → 批3 逐项拍板减法，潜在释放 ~120G）。

---

## 二、问题 3 专项：模型装载/释放·显存调度·ComfyUI 协作

### 2.1 机制全景（时序，文件:行）

**让渡→排空→唤醒全链**（vLLM 编舞）：
```
[image_queue._acquire_paint_lock 持锁] → sleep_vllm_for_generation（4 编排点单源收编 VramYieldCoordinator）
→ vllm_service.sleep_for_paint:328 — sleep_mode:false → POST /sleep 404 → 杀子进程（秒级回收全部显存）:369/:1041
→ [生成期：在途对话流死；引擎 _state 仍 ready（无人清，靠假 ready 防线兜）]
→ 锁释放 → 排空去抖 10s（gpu_budget:559，只认全队最新排空）
→ wake 后台线程 → start()：收养探测→功能锁门禁→RAM 闸 60s→准入复测 60s→Popen→健康轮询 ≤420s→预热 ≤240s→ready
→ 状态查询 _maybe_reattach 零装载重挂（VLLMBackend.reattach）
[用户窗口体验] 生成期对话排队 300s→互斥拒绝；唤醒期 ensure_loaded 轮询 ≤300s 自动续跑
```
KV cache 备份：**不存在**（每次杀进程=KV+prefix cache 全灭，prefix-caching 只在进程存活期内有效）。

**ComfyUI 协作链**：选栈（gen_router）→ 启动（comfy_proc.spawn：孤儿清扫/模型挂接/参数/Job Object 绑 Backend/150s 就绪）→ 预热（后台线程 fire-and-forget）→ 提交（/prompt，引擎级 _gen_lock 串行）→ 轮询（3s /history，**四套实现各自漂移**）→ 产物（逐图读出即删源）→ 收尾（链式有队列守卫，paint 无）→ 空闲 300s 三防线自动关 → 下次复用探测（只探端口不验身份）。注入面 ✅ 干净（参数全走 JSON 值，无字符串拼模板；custom node _safe_name 兜底）。

### 2.2 R2 新发现（显存/编舞/ComfyUI 域）

**P1（3+1 条，主审亲核 ✅）**
| # | 条目 | 位置 | 触发路径 |
|---|---|---|---|
| R2-A | **准入台账预留装载成功后永不回落**——`_free_logical = NVML_free − _reserved_vram_gb`，装载完成后 NVML 已扣一次、台账又扣一次；同字段三种登记语义并存（ensure 不回落/register 补加/note 不加），注释互相矛盾 | model_manager/__init__.py:700-704（加）/:980/:1049/:1130（仅卸载/失败减）/:997-1034（成功装载无回落） | 凡走 mgr 装载的模型存活期内被双扣 → 后续准入系统性过紧 → 过早驱逐/拒绝（「12G 卡上 16G 的错觉」反向版） |
| R2-B | **paint.ensure_loaded 丢弃 mgr 裁决**——`ensure("paint",...)` 返回值弃置（异常仅 debug），无条件 `load_model`；对照 dialog 尊重 mgr 拒绝（V77 修复） | paint_engine.py:1070-1078 | mgr「显存不足」拒绝 → paint 照装 → 走自家 check_vram 驱逐链或 4GB offload 降档；「统一准入门」只对 dialog 成立，paint 域无准入 |
| R2-C | **同模型并发 ensure_loaded 双预留泄漏**——短路检查在 `_loaded_lock` 内即释放，装载窗口（10~157s）无互斥；三个互不串行入口（/models/load 可重入×2、/models/warmup 预热线程刻意无锁、发送路径） | model_manager/__init__.py:897-900→:945→:997 | 两并发均过短路 → 预留 +2×required → 第二次真短路 → 台账一条 → 卸载只减一次 → 预留虚高直至重启；与 R2-A 叠加放大误拒 |
| R2-D | **聊天虚拟列表偏移重建把旧累积偏移当行高**——`prev[i+1]` 是前 i+1 行总高却作第 i 行高度增量，且实测行高 Map 被清空；O(n²) 膨胀 | frontend/src/components/common/VirtualList.tsx:118-124 | 会话超 60 条激活虚拟化后任追加一条消息 → 间距暴涨/大片空白/窗口错位——**产品第一界面的必现 bug** |

**P2（本域 10 条）**：①vLLM stop 撞 start 锁前段（RAM/准入等待最长 ~120s 无取消自查）=无操作+抹取消旗标→让渡假成功、子进程照启（vllm_service.py:1000-1007/:530-607）✅；②ensure_loaded 收养失败无条件 _reap_orphans 可误杀自家 booting 子进程（跨目标场景，dialog_engine.py:1409-1417）✅；③ComfyUI 超时/取消不 interrupt：全仓唯一 /interrupt 在链式取消；超时仅 raise，僵尸任务排队级联假超时+GPU 空烧+「僵尸保活」空闲守卫（comfy_paint_engine.py:1004、h3_engine.py:471/:709）✅；④绘画运行中取消=假取消：旗标检查在心跳线程自灭，主线程跑完覆写 status=done（draw.py:1271/:555-575/:594）✅；⑤硬件档位覆盖（SET-008）不达路由，PUT /hardware/tier 文档承诺落空（dialog_engine.py:367-368、hardware.py:259-263）；⑥HARDWARE_TIER_TABLE 的 models 路由字典零消费（12GB 降级实为各引擎各自兜底，档位表=展示件）；⑦让渡量口径四写无实测锚（~7/~7-12/~12+157s/13.6 隐含）；⑧深层空闲回收无「常驻」判别——300s 把 release_for_module 特意热备的 vLLM 卸掉，热备承诺只有 5 分钟有效期（scheduler/__init__.py:290-292 vs resource_guard.py:311-318 有 resident 保护）；⑨vram_manager 读数只见本进程 CUDA（遥测与状态栏两数并存）；⑩memory_manager 记账单位错位（显存 GB 记成 RAM MB）+lz4「压缩」小 dict 空转。

**P3（本域 11 条）**：准入复测 torch-only 口径系统性乐观（GGUF 在跑时高估 6.18GB、vLLM 占 15.6GB 时主进程仍报空闲 11.69GB——gpu_budget 文件头自证；间隙窗口无兜）；sitecustomize 补丁常驻替换 find_loaded_library（sleep_mode:false 也在改写，升级三重假设全静默失效面）；取消协议 warmup 盲区（240s 不查取消）+三处注释漂移（240 vs 420/2s vs 5s）；vLLM 异常退出不清档（status 报 stale）；降级链无冷却（空闲机每 900s 重演「试 9B→失败→降 4B」广播抖动）；纯云对话仍占本地互斥锁（云的零显存优势未兑换锁豁免）；release_for_module 停止超时半途（台账同步被跳过）；DIALOG_TIERS 档位表零消费；ComfyUI 崩溃残留无启动收账（input/output 实测已泄漏 144MB：5 组 paint_ref/pulid + 2 个 h3chain 孤儿目录）；复用探测不验实例身份（外部 ComfyUI 无重定向参数→「完成但无产物」）；/unload 无队列守卫+绘画错误码复用 VIDEO_RESOLUTION_DEGRADED（排障受扰）。

### 2.3 管家清单（同一张 16GB 卡上并存的账/闸）

| 管家 | 记什么 | 与谁对账 | 盲区 |
|---|---|---|---|
| ModelManager `_loaded`+`_reserved_vram_gb` | 台账+预留（**现行决策真源**） | budget ledger 回读 | 双记（R2-A）/三登记语义/并发漏账（R2-C） |
| GpuBudget+BusyRegistry | 物理双口径+忙碌簿 | ledger 注入回读 | advisory 无决策权；torch_only 分裂口径 |
| VramManager | 遥测 track_alloc | 无人 | 子进程显存不可见 |
| MemoryManager | RAM 块表 | 无人 | 单位错位/压缩无解压 |
| ResourceGuard | RAM/VRAM 危急动作线 | mgr+引擎 status | paint 兜底条目 vram_gb=0 |
| ThermalGuard | 温度 | feature_lock 入口 | — |
| Scheduler 两级回收 | 60s/300s 空闲 | lock.idle+BusyRegistry | 无常驻判别（P2-⑧） |
| vLLM 准入闸 | torch_only 空闲×util | 无（口径独走） | 乐观 6GB+ 实测案例 |
| vram_policy+DIALOG_TIERS | 阈值+档位 | 无 | TIERS 零消费 |
| HARDWARE_TIER_TABLE | 型号档+路由字典 | 无 | 死数据/override 不达路由 |
| paint check_vram | 引擎自裁准入 | mgr 只读 | 与 mgr 双轨（R2-B） |
| dialog _vram_fallback | 降档自选 | mgr.estimate | 与 DIALOG_TIERS 平行两表 |
| switch_engine | 切换任务+allocate | mgr | video 分支自记 |

**一句话：同一时刻「空闲显存」有四个数**——mgr 口径（最紧，因双记）、budget 物理口径、vLLM 闸 torch-only（最乐观）、vram_manager 本进程（最松）；/models/vram 端点同时回两套 free。

### 2.4 更优方案（诚实分层，含可拍板路径）

**A. 立即可修（bug 级，1~2 天量级，全部有明确修法）**：①R2-A 预留回落（成功装载后 `_reserved -= required`，或台账改为只记在途）；②R2-B paint 尊重 mgr 裁决（对齐 dialog 的 V77 写法）；③R2-C 装载互斥（短路检查挪进装载锁窗口或 per-model in-flight 登记）；④准入复测改 NVML 全口径（P3，gpu_budget.read_physical_bytes 现成双口径）；⑤降级失败冷却计时器（P3-9）；⑥纯云后端锁豁免（P3-10）；⑦ComfyUI 超时路径挂 /interrupt + paint 轮询加取消检查点（链式现成模板）；⑧绘画取消复核（done 写入前查旗标）。

**B. 工程收敛（已立项待拍板=V9 显存编排统一层，2026-09-08 方案）**：把 13 管家收敛为「1 本账（gpu_budget 转正）+2 个守卫（资源/温度）」；让渡量单源化；空闲回收加常驻判别；冷启动 157s 压缩试验。R2 证据（四把尺子/三种登记语义）说明 V9 不是锦上添花，是止血。**建议把 A 组 8 件并入 V9 第一批验收单。**

**C. 等上游解锁（挂账路径正确，勿自研绕行）**：vLLM sleep L1 真睡眠——接线全就绪（补丁常驻+config 一键翻转 `vllm.sleep_mode:true`），唯一堵点=Windows 轮子 cumem_allocator 缺符号；上游 2026-09 仍无 Windows sleep 支持（官方文档只有 Linux L1/L2，博客口径唤醒快 18~200×）。升级 vLLM 时的验单已取证在案：qwen3 parser 注册名硬耦合、sitecustomize 三重假设、MTP #53912 雷区。

**D. 减负才是真优化（与整理方案/瘦身三刀联动）**：video_engine 死路径收敛（幅度勘正≈1650 行非 350）、gguf/llama 第三腿去留拍板（12GB 用户占比数据）、RAM 治理（90% 危急线实况反复触发=比显存更紧的约束；ComfyUI 动态显存已确认启用）、ComfyUI 崩溃残留启动收账+独占权重台账（整理方案批3-4/批4-3）。

**E. 诚实的边界**：性能杠杆已基本拉满（MTP 常开 +40%、FP8 KV auto、16K 上下文、sage on）——16GB 单卡跑「9B 对话+12GB H3 视频+绘画」三线，互斥与让渡是物理必然；产品级的更优解是编排体验（常驻档选择/预热时机/云兜底路由），不是幻想消灭让渡。

### 2.5 附：训练栈（装载释放的邻域）

- **musubi-tuner 补丁资产风险已兑现**（P2）：唯一补丁活在内层 git 的 `stash@{0}`（树=上游原样，按 OMNISPACE_PATCH.md 配方跑必炸）；第二处补丁（zimage 底座解冻）完全未记档；外层仓库不跟踪该目录——一次 `stash drop` 全灭。且 **src 三训练服务与 musubi 零接线**（全部进程内 diffusers+peft），3.6G 仓+3.5G venv 只被脚本链消费。
- **知识训练三泄漏**（P2）：HF Trainer checkpoint 无 save_total_limit 且 `_prune_versions` 永不命中 checkpoints 目录；dataset jsonl 每任务一份无回收；data_prune 两不覆盖。
- **行为「负样本」被当正目标灌进 SFT**（P2，主审亲核 ✅）：reject 事件构造 `pair_type:"negative"`+output=被拒内容，训练侧不筛 pair_type 照收——**用户拒绝的内容反而被训练强化**。
- 其余：Chromium 崩溃僵尸态无回收（P2）；风格素材三目录只进不出（P2）；preempt_session 死线（学习会话不避让创作）；暂停期照烧预算；版本白名单三处修法漂移（character rollback 裸拼→任意 adapter 可部署到资产旁被 D-LoRA 自动挂载）；character/style 训练无显存预检；跨服务锁竞态即 error 不回队。

---

## 三、R2 其他新发现（前端二轮 / core 内核）

**前端（P1×1+P2×6+P3×10）**：P1=VirtualList（§2.2-D）；P2=①InferProgressModal「后台运行」吞完成回调→inferring 永久卡 true 工序按钮死锁 ②StoryboardTable 卸载清防抖不 flush→1.2s 窗口内编辑静默丢失 ③ErrorBoundary 仅根层无路由级隔离（任一页面渲染错=整壳全屏崩）④批量工序运行中无取消通道+单行挂起即弹窗永久锁死 ⑤VideoDrawer 批量关键帧 invalidate 不 refetch→缩略图集体消失 ⑥升级等待遮罩无失败出口；P3=Logs 页 gallery 签漏判双卡渲染、设置保存双 toast 矛盾、AssetDetail 草稿无守卫回包覆写手输、5 处常驻轮询无退避无可见性门控、CSS 变量违规新增面（tsx 内联+5 个非主题族 CSS 20+ 处+Novel/Comic 整模块 Tailwind 调色板约 40 处）、ESC 叠层互踩、关键帧预取 200 并发、小说章节切换无脏检查+AI 回写覆盖手输、公共弹层无焦点陷阱、弱项打包杂项。**公共件盘点勘正**：实际无 OmniModal/OmniDrawer/独立 ErrorBoundary 文件（Modal.tsx+App 内联），存在三套并行弹层+两套灯箱。
**core/cutemamen（P2×3+P3×11）**：STDP 全局死代码（§一）；trainer 监督更新写死参数（向量化前向只读构造时数组，作者在 readout 打过同类补丁但 trainer 没打）；跨进程「逐位一致」承诺在路由维度断裂（hash() 进程盐化→rust_coding 演示链精度坍塌）；P3=抑制损失 off-by-one（第 5 类自相矛盾）、预算淘汰保护承诺落空、kernel._dispatch 不隔离插件异常（一个插件边缘输入崩整个 think()）、版本门双真源（0.8.7 vs 2.5.0 两路判定相反）、全局 RNG 重播种污染链、rust_coding 语料兜底=延迟崩溃、运行时写盘四路径绕过 gitignore、KVStack 持锁 O(n) 循环+docstring 撒谎、KV 写入守卫形同虚设、无上限缓存×2、内核零锁并发面。**成熟度定性：cutemamen 插件框架≈半工程；SNN 数值内核≈精心装饰的研究原型。**

---

## 四、全问题总账与评级（问 4）

**评级口径**：P1=资损/数据损坏/闸门失效/必现功能死；P2=特定条件功能坏/安全面/慢性资源病；P3=工程卫生/体验/潜伏雷；文档漂移单列。状态：全条目行号证据在三轮报告（09-19 极审/R1/R2）。

### 4.1 未修 P1 全清单（20 条）

| # | 领域 | 条目 | 来源 |
|---|---|---|---|
| 1 | 许可 | 回拨守卫锚点随校验滑动（时限码慢钟续命） | 09-19 P1-1 |
| 2 | 许可 | license.state.json 无签名可删→守卫/墓碑归零 | 09-19 P1-2 |
| 3 | 许可 | 吊销名单未签名 fail-open | 09-19 P1-3 |
| 4 | 队列 | 统一队列 worker 丢失唤醒（任务永久滞留） | 09-19 P1-6 |
| 5 | 数据 | 备份恢复后不重跑迁移（旧备份→no such column） | 09-19 P1-9 |
| 6 | 前端 | 会话/项目切换竞态族四点（慢响应覆盖新态） | 09-19 P1-10 |
| 7 | 前端 | GlobalSearch 死端点+吞错（模型搜索恒空） | 09-19 P1-11 |
| 8 | 启动链 | stop.py 前缀子串+按名全机认杀（跨副本误杀） | 09-19 P1-12 |
| 9 | 启动链 | launcher 'omnispace' 子串判定杀他副本冷启动 | 09-19 P1-13 |
| 10 | 对话 | transformers 停止不停真生成→双路并发 prefill | R1 |
| 11 | 资损 | page_guard 云盲区→全页关闭退出丢付费云任务 | R1 |
| 12 | 发行 | updater 硬编码 py310→发行包升级必败+回滚后不自启（三处同族） | R1 |
| 13 | 工具 | 转换器 is_double 判定序错→单流映射死代码+自检假绿（**在飞件，勿原样提交**） | R1 |
| 14 | 文档 | CLAUDE.md 三处自相矛盾（user_version 7v13/路由 9v11/store 清单含已删项） | R1 |
| 15 | 插件 | invoke 超时在事件循环抢插件锁→单插件冻结全后端 | R1 |
| 16 | 测试 | 恒真断言×4（含批6 语义验证被掏空） | R1 |
| 17 | 显存 | 准入台账双记（预留永不回落） | R2 |
| 18 | 显存 | paint 丢弃统一准入口裁决 | R2 |
| 19 | 显存 | 并发装载双预留泄漏直至重启 | R2 |
| 20 | 前端 | 聊天 VirtualList 偏移重建指数膨胀 | R2 |

### 4.2 未修 P2 分域汇总（≈70 条；明细见三轮报告）

| 领域 | 条数 | 代表条目 |
|---|---|---|
| 数据/备份/安全 | ~17 | 自动备份明文整库、导出未脱敏、加密备份不可清、TrustedHost-LAN 结构互斥、v3 迁移跳过、SSRF 探测面、musubi 补丁在 stash、checkpoint/素材泄漏、负样本正训 |
| 对话链 | ~7 | WS 断连不停止+反清旗标、_maybe_reattach 无锁改状态机、sys_content 无截断、booting 断连检测恒假、云停止漏记账 |
| 引擎/ComfyUI | ~9 | 超时不 interrupt 级联、假取消、llama 收养假卸载、H3 前置裁剪装 VL、ffmpeg 静默挂死、vLLM stop 撞 start、收养失败误杀自家子进程 |
| 队列/调度 | ~9 | 训练锁双取泄漏、4xx 喂舱壁误重启、gpu_budget 非原子（休眠）、submit_and_wait 饿死、档位覆盖不达路由、常驻判别缺失、让渡量四写 |
| 前端 | ~14 | videoSlice 失败放锁、ws 双活、LicenseGate 空转、断流兜底缺口、零 Zod、裸 fetch×6、后台运行吞回调、防抖丢编辑、批量无取消、批删分裂 |
| API 面 | ~6 | 云端先落库后校验幽灵 pending、/models/{id}/files rmtree 链、插件包全量读、voice 死端点族+文档谎言、知识批删无备份 |
| 插件内核 | ~5 | user_data llm.provider 任意 URL 出口、沙箱孙进程、STDP 死、trainer 死参、跨进程复现断裂 |
| 启动链/工具 | ~8 | 8189 兜底无归属、双 boot 互杀、robocopy 无护栏、升级回滚部分失败钉死、让位旁路 copy_id 闸、L2 npm 假挂（已修）余 dir_audit 退出码等 |

### 4.3 P3（≈110 条）与文档漂移（12+5）

P3 大头：吞错/无界/卫生类（09-19 62+R1 26+R2 26，绝大多数仍在盘）。文档漂移：R1 十二条对照表 + R2 盘点五断点（INDEX 悬空登记/归档未搬/口径过时/审计报告未登记/根级 13 件零登记）+ 两份「现行」文档不在 git。**防漂移闸自身**：端点哨兵三源失谐（378/376/360）+claims why 算术自错+覆盖缺口（DB 事实/运行时版本族/端口零断言）——修法已列整理方案批2-5。

### 4.4 已修无回归清单（两轮复核背书）

09-19 第一梯队六项（mypy 闸/torch 判定键/apikeys/character_lora/云鉴权头/llama 死锁）+ 大考四项（P-06 气泡/P-08 预热卡死/P-11 显存口径/P-14 过期标记）+ reset_instance——全部真修、未见回潮（副作用仅两笔提交夹带行尾翻转，见 R1 §六.5）。

### 4.5 修复梯队（在 R1 §七 基础上增补）

- **第一梯队（并入或先于 V9）**：R2-A/B/C 显存三 P1 + R2-D VirtualList（聊天主界面）+ R1 未动的 N-P1-1/2/3 + P1-6；ComfyUI interrupt 两件（超时挂断+取消检查点）——这九件都是「改点小、收益直」。
- **第二梯队**：V9 显存编排统一层拍板执行（含 §2.4-A 余项+常驻判别+口径单源）；训练栈四件（负样本过滤/musubi 补丁落树登记/checkpoint 回收/Chromium 僵尸回收）；09-19 第二梯队存量（许可三件套/stop·launcher 归属/恢复重迁）。
- **第三梯队**：整理方案批1~批5 执行；瘦身三刀（按 R1 勘正幅度）；P3 批量清账。
- **方向拍板项**：插件内核裁决（补 SKILLS 通电演示链 vs 按死重收敛——R2 证据支持「框架保留、SNN 学习叙事降级为研究件」）；gguf 第三腿；测试态隔离。

---

## 五、整理方案（问 5）

见 `docs/文档与目录整理方案-2026-09-20.md`：批1 零风险减法（~2G，拍板即执行）→ 批2 文档归位+宪法勘误+防漂移断言加固（等在飞会话收卷）→ 批4 结构加法（tools 分层/备份统一/manifest 字段/logs TTL）→ 批5 文档加法（runbook/API 机器清单/备份恢复手册 Top3）→ 批3 逐项拍板减法（~120G 潜力：冗余快照/py310/独占权重/隔离区 L2·L3/musubi venv）。关键盘点事实：docs 393 文件 124MB、根散 102 件、12 份 mermaid 重复 30MB、11 份待拍板方案最老 16 天；全仓 ~271G（tools 141G 的 98%=ComfyUI 便携包；旧「36387 文件」之谜=第三方非自有代码）；**旧「41G/144G 隔离区」口径已了结**（09-17 真删 47.7G，剩 L2/L3 99G 按原计划）。

## 六、复核无恙面（R2 各路，防下轮重复劳动）

- 显存：model_manager P0-5 锁序无恙（旧相向环两边均已拆、模块头宣言过时待更新）；gpu_budget 三单例双检锁/BusyRegistry 幂等/VramYieldCoordinator 单源去抖在位；竞态矩阵其余场景（装载中卸载/切模块/让渡中新任务）结局均诚实。
- vLLM：收养/热备/Job Object/去抖唤醒/reattach 零装载重挂等存量部件健康；降级本身粘滞（需手动换回）。
- ComfyUI：注入面干净（JSON 值传递+节点校验+custom node _safe_name）；空闲 300s 三防线结构无恙；P-08 双栈 status 语义未再发现新洞。
- 训练：api/training.py 三源 fail-soft 与 learn.py 错误分层质量好；知识四写/容量裁剪/降级链与已知口径一致；行为四列加密读写对称。
- 前端：common/ 组件清理纪律优秀（定时器/观察器全配对）；MangaWorkspace 右栏槽位状态机一致；batchOps「如实汇总失败」语义诚实；tokens.css 登记合规。
- 内核：KVStack 行视图/LRU 语义、LinearReadout 数值护栏、EventBus 订阅隔离、pkg 解压炸弹闸+allow_pickle=False、encode_text crc32 跨进程确定——在位。

---

> **纪律声明**：本轮零代码改动、零进程操作；仅新增本报告与整理方案两份文档（新文件，不动既有件）。所有 P1 级新发现均经主审亲读源码裁实；上游结论（vLLM sleep Windows 状态）附联网查证。大考会话在飞期间全程避让，未碰其工作区文件。
