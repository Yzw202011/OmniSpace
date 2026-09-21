# 第五轮深审报告（R5）— round-2026-09-20（ComfyUI 深水区·services 余部清零）

> **元信息**
> - 时间：2026-09-20 13:35~14:00（账户限额锁定期，**零代理，主审亲扫**）；基线 `f995a99` 无变化；全程只读。
> - 本轮主题（用户新增点名）：**ComfyUI 深水区**（custom_nodes 家底/工作流消费关系/嵌入环境健康）+ **services 余部七文件清零**（四轮以来从未被读过的最后一批后端文件）。
> - R3/R4 未竟面状态更新见 §四。

---

## 一、执行摘要（四问回答）

**问1（方向·含 comfy）**：ComfyUI 协作面结论微调——**产品消费面比装包面窄得多**：全产品唯一工作流模板（h3_chain_workflow_api.json，34 节点 27 类型）经逐类核对**全部由核心节点+链式包+turbo 包满足**，绘画链只用 PuLID-Flux2+核心；而 custom_nodes 里装了 14 个包，其中**三个同功能 latent 升采样包（合计 ~10MB）产品代码零引用**——纯手动使用便利件/试验残留。这不是方向病，是「装了没裁」的减法素材（归整理方案批3）。
**问2 增量**：新 P3×2、非 bug 澄清×2、阴性清零×1（services 余部七文件全部健康）。
**问3**：无新增（R2 三层病+四层更优方案结论维持；本轮 ComfyUI 证据与 R2 协作链全景互洽）。
**问4 总账**：未修 **P1×21、P2≈75、P3≈124**（+2）。

## 二、ComfyUI 深水区发现

**[P3-R5-1] 三个同功能 latent 升采样包并存，产品零消费**
- `ComfyUI-MiniMaxH3_LatentUpscaler`（761K，Chunked/Combined/Collect 三类）、`Comfyui_Minimax_h3_latent_Upscaler`（9.1M，其中 examples/ 8.5M）、`h3-latent-upscaler`（265K，LatentUpscale/ConditioningUpscale/AddNoise）——**src/ 全仓对这三家的节点类零引用**（唯一工作流模板 h3_chain 的 27 类型不含任何升采样节点，grep 实证）。
- 影响：~10MB 死重+启动时三包都被 ComfyUI import（加载面）+「哪个才是真的」的维护困惑。建议：留一个（与 H3 作者跟版的那家）、隔离另两个（整理方案批3 增补）。
- 附：各包 `__pycache__` 里同时存在 cpython-310/313 两代 pyc——py310 时代残骸（无害 clutter，随包处理）。

**[P3-R5-2] audio-T8 包（32M，custom_nodes 最大件）消费关系未证实**：VAEDecodeAudio 在 27 类型里是核心节点；audio-T8 自有节点类在产品代码与模板中均未命中（音频链另有 encoder_service 处理）。32M 是否为手动使用件待核（本轮限额内未展开）——列为整理方案批3 的待核项。

**非 bug 澄清（防误伤）**：
- `style_seed.ensure_seed` 每次启动都调（main.py:91-92），但种子文件只在发行包（开发目录无 assets_enc/）→ 开发库 art_styles=0 行、发行包首启自动重灌 1117 条——**与 09-02 拍板语义完全一致，非 bug**。
- `python_embeded` 的 onnxruntime **健康**：dist-info 与 `__version__` 同为 1.30.0（记忆中的 py/native 混装病历未复发）。

**产品消费的 custom_nodes 白名单（本轮实证）**：ComfyUI-MiniMaxH3-Contex-Loop（chain 家族 17 类节点）+ comfyui-minimax-h3-turbo（TurboLoRA/TurboSampler）+ ComfyUI-PuLID-Flux2（ApplyPuLIDFlux2）+ 核心。Impact-Pack/KJNodes/VHS/mixlab/rtx 五家为链式包的依赖或手动件（依赖关系未逐家核，留待续审）。

## 三、services 余部七文件清零（全阴，四轮盲区补完）

| 文件 | 行数 | 结论 |
|---|---|---|
| heartbeat.py | 131 | 干净：永不抛错取证+历史轮转 512KB/1000 行有界；与 R4 实况（心跳 jsonl 在写）互证 |
| periodic.py | 63 | **好范式**：daemon 工厂（Event.wait 可停+异常全兜）——本身就是克隆收敛的产物（头注自认收敛了三处克隆循环） |
| priority.py | 47 | 干净：单源枚举+训练相对级映射（头注自认收敛了两处克隆 dict） |
| quality_governor.py | 95 | 干净：双检锁单例+分级降参旗标 |
| style_seed.py | 46 | 干净：事务包裹+空表条件导入（行为见上澄清） |
| injection_service.py | 272 | 干净：双路 RAG（向量+FTS 各自独立降级）+RRF 融合+创作意图降权——与已知 sys_content 无截断（R1-P2-13）是消费侧关系非自身病 |
| web_search.py | 367 | 干净：三通道可插拔+查询缓存 TTL+时效判据+默认关纪律+超时齐备；浏览器路依赖 browser_pool（Chromium 僵尸=R2 已立 P2-D 的族成员，不另立） |
| cloud_provider_service.py | 836 | 结构复核（协议-槽位校验/掩码/绑定清理）与前轮结论互洽，未再深读全文 |

**结论：后端 services 层至此 100% 有过全文精读覆盖（四轮累计），最后七件全部健康——这层的质量底子是好的。**

## 四、R3/R4 未竟面状态（截至本轮）

- ✅ 已清：前端 manga 切片余部（R4）、services 余部七文件（本轮）、ComfyUI 装包消费关系（本轮）。
- ⏳ 仍待（限额 15:59 后可续，风险低）：克隆族谱剩余四族（ensure_loaded 全矩阵/锁配对/加密对/时间处理）、config 注释级默认值深对、tests/flow 逐文件断链、DialogView/DialogPage 全文、8 个小 store 全文、audio-T8/五支撑包依赖关系核实。

## 五、总账（截至第五轮）

未修 **P1×21**（清单=R2 §4.1 + R3 万能错码）｜**P2≈75**｜**P3≈124**（+R5 两项）｜文档漂移 25 处。今日修复入库 12 笔全无回归。**后端代码面（src/ 全部 .py）至此实现四轮+本轮的全文级覆盖**；剩余未竟面全部在前端深读/横切矩阵/测试层，风险预期低。

---

> **纪律声明**：本轮零代理（账户限额）、零写盘、零进程操作；ComfyUI 节点消费关系与升采样包零引用为 grep+模板解析实证；三库与所有结论均只读取得。
