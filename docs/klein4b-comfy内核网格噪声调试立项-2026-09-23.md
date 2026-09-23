# Klein-4B comfy 内核网格噪声调试（立项）

> **立项拍板**：2026-09-23 用户拍板立项（4B 内核调试）。
> **状态**：方案待细化开工；现网已止血（5c6891a——4b 摘出可选面 + D-LoRA 回落 diffusers）。
> **前置阅读**：`docs/klein4b-comfy转换残局修复立项-2026-09-23.md`（第一轮转换格式修复，已收官但未解决本问题）。

---

## 一、问题陈述

comfy 栈（v0.37.0）加载 bf16 Klein-4B 权重（`diffusion_models/flux-2-klein-4b.safetensors`，
2026-09-23 重转版）采样输出**系统性网格点阵噪声**——构图/颜色分布/服装轮廓大体正确
（attention 大方向在工作），但高频细节全坏。任何参数组合均复现。

同一权重在 **diffusers 0.39 栈（paint_engine）完全健康**（四视图/D-LoRA 产线实测）；
同架构 **9B-fp8 在 comfy 完全健康**。

## 二、排查矩阵（2026-09-23 全部排除项——后人勿重走）

| 变量 | 已测组合 | 结果 |
|---|---|---|
| 分辨率 | 832×480 / 640×640 / 768² / 1024² / 1024×576 / 1344×752 | 全崩 |
| 步数 | 8 / 36 | 全崩 |
| seed | 42 / 20250923 / 随机 | 全崩 |
| 画幅比 | 横幅 / 方图 | 全崩 |
| 提示词 | 长中文 / 短中文 / 长英文 / 短英文；人像 / 场景 / 动物 | 人像场景全崩（早期猫图健康记录存疑，见 §三） |
| ComfyUI 内存态 | 运行中多模型轮换后 / 强杀进程冷启动 | 全崩 |
| 转换产物数值 | 源↔产物逐元素对照（single/double/modulation/输入输出层抽样） | **数值完全一致** |
| 键名结构 | 与 9b-fp8 官方 comfy 权重模板对拍 | **100% 一致** |

**关键时间线勘误**：曾被引用为「4b 出图正常」的早期证据（ginger cat 832×480、
漫画分格 v3/v5）经复核均不成立——「Model storage policy」日志行是启动期文件清单
打印而非装载记录，且部分轮次 params.model 未透传实际由 9b 底座执行。
**结论修正：4b 在 comfy 栈从未出过健康图**（与 v0.37 部署时间线自洽）。

## 三、根因假设（按优先级）

- **H1（首选验证）：fp8 路径健康、bf16 路径缺陷**——9b-fp8 正常说明 comfy 的
  Flux2 图执行框架无恙；差异变量 = 权重精度路径（fp8 走 comfy-kitchen 量化内核，
  bf16 走原生 Linear/cast）。疑似 bf16 某层（或某类层）数值路径错误 → 网格噪声。
- **H2：v0.37 新增编译/图优化对 bf16 Klein 误编译**（commit 1d61dcc3
  「Compile Qwen Image 2.1 transformer blocks」邻域）。
- **H3：comfy-kitchen 0.2.31→0.2.35 升级引入**（本次升级唯一内核级变动）。

## 四、调试方案（分层，代价从低到高）

- **方案 E（最便宜，最先做）：换官方 fp8 4B 权重**——查 Comfy-Org/黑森林官方是否
  发布 FLUX.2-Klein-4B fp8 comfy 包；有则直接替换自转换 bf16 版，UNETLoader
  weight_dtype=fp8_e4m3fn 走已被验证的 fp8 路径。若成功 = 一步复活 + 显存减半。
- **方案 B：自转 fp8**——官方无 fp8 包时，转换器追加 fp8 量化输出（bf16→e4m3fn
  per-tensor/逐层 scale），走 fp8 路径验证 H1。
- **方案 A（H1 数值定位）：双层插桩对照**——同 prompt/seed/latent 分别跑
  diffusers 管线与 comfy 工作流，逐 block 抓 hidden states 对比，定位首个发散层；
  再对该层逐子模块（norm/attn/mlp/modulation）二分。
- **方案 C：comfy 版本二分**——v0.34↔v0.37 检出切换复测（12d52794 锚仍在），
  锁定引入 commit 后读 diff。
- **方案 D：comfy-kitchen 降级对照**（0.2.35→0.2.31）。

## 五、验收标准（实弹）

1. 4b comfy 出图：832×480 与 1024² 各一张人像+场景，无网格噪声，目验与
   diffusers 同 seed 输出结构一致
2. D-LoRA comfy 路径恢复：带角色 LoRA 分格走 comfy+4b 正常出图
3. 接线回真：`_KLEIN_4B_BROKEN=False` + 真源恢复条目 + 双清单可选 +
   keyframe D-LoRA 闸回开（keyframe.py 止血块删除）
4. klein-9b / z-image / qwen21 回归不退；全量单测绿

## 六、风险与回退

- 调试全程不影响现网（4b 已摘除，产线走 diffusers）
- 方案 E 若官方 fp8 不存在则零成本跳过
- 修复回真后如复发：翻回 `_KLEIN_4B_BROKEN=True` + revert 对应 commit

## 七、接线点清单（修复落地时）

1. `comfy_paint_engine.py`：`_KLEIN_4B_BROKEN=False` + 真源恢复条目（files 指向
   fp8 新文件）+ 守卫文案退役
2. `keyframe.py`：删除「4b-comfy 质量缺陷止血」回落块（恢复 B6+ 放行语义）
3. 白名单数据面补回 4b；`comfy_model_map.json`/manifest 更新 fp8 文件登记
4. 测试：诚实性/槽位测试的 4b 断言翻回四件套
