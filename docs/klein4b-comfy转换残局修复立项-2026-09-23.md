# klein-4b ComfyUI 格式转换残局修复（立项）

> **立项拍板**：2026-09-23 用户拍板立项（与「切换诚实性修复」同夜，b1d219b 的守卫即本项的止血带）。
> **状态**：方案待细化拍板，未开工。
> **关联**：`src/services/inference/comfy_paint_engine.py` 的 `_KLEIN_4B_BROKEN` 守卫（修复后翻 False 即恢复）。

---

## 一、问题陈述

`tools/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/flux-2-klein-4b.safetensors`
（7.75GB，mtime 2026-09-19 01:13，由 `tools/convert_klein4b_to_comfy.py` 产出）是**转换残局**：
comfy 的 Flux2 模型类无法消费其布局，加载后在采样期炸
`RuntimeError: mat1 and mat2 shapes cannot be multiplied (4352x3072 and 27648x3072)`
（`comfy/ldm/flux/layers.py:325` SingleStreamBlock.linear1）。

**影响面**（2026-09-23 时点）：
- comfy 栈 `model=flux2-klein-4b` 显式点名 → 已被诚实性守卫拦截（如实报错）
- D-LoRA 自动切 4b 路径（角色 LoRA 在场 + lora_base=4b）→ 同上已拦截
- 四视图产线走 diffusers 栈（paint_engine.py），**不受影响**
- `comfy_model_map.json` 的 `unet/flux-2-klein-4b.safetensors` 条目对端已不存在（陈旧条目）

## 二、证据链（2026-09-23 取证）

1. **文件解剖**（safetensors 头直读）：
   - 149 键 ≈ 3.88B 参数；`double_blocks.0-4`（60 键，img_attn/img_mlp/txt_attn/txt_mlp）
     + `single_blocks`（80 键，**fused `attn.to_qkv_mlp_proj.weight` (27648,3072)**）
     + **外挂调制**（顶层 `double_stream_modulation_img/txt`、`single_stream_modulation`）
     + `img_in.weight (3072,128)`、`txt_in.weight (3072,7680)`、`final_layer`、`time_in`
   - 即 **Klein 原生布局**：qkv+mlp 融合单投影 + 独立调制模块
   - comfy Flux2 期望布局：`transformer_blocks.N.linear1/linear2`（内嵌调制）——**键名完全不交**
2. **对照实验**（升级无辜证明）：ComfyUI v0.34.0（回滚锚 12d52794）与 v0.37.0
   跑同一 `model=flux2-klein-4b` t2i 请求，**报一模一样的形状错配**——残局在
   2026-09-19 转换产出时即存在，与 2026-09-23 的 ComfyUI 升级无关。
3. detect_unet_config 依 img_in/txt_in 键将其分类为 flux2 → 建标准 Flux2 模型 →
   加载时键对不上 → 权重错位 → 采样期形状炸。

## 三、修复目标与验收标准

**目标**：comfy 栈可真实加载 klein-4b 并出图，恢复 D-LoRA（角色 LoRA）在 comfy 栈的可用性。

**验收（实弹）**：
1. `POST /draw/generate model=flux2-klein-4b` 出图成功（832×480 与四视图 720p 档）
2. D-LoRA 路径冒烟：带角色 LoRA（lora_base=4b）生成不炸、LoRA 生效目验
3. `_KLEIN_4B_BROKEN` 翻 False 后：`COMFY_PAINT_MODELS` 恢复 4b 条目，
   双清单（/draw/models + 漫剧 paint 槽）出现 4b 且可选
4. 单测：detect→config 断言（num_heads=24 / hidden=3072）+ 诚实性测试翻 flag 用例
5. klein-9b / z-image 回归不退（真源其余条目不受影响）

## 四、方案候选

- **方案 A：重写转换器（权重手术，推荐起点）**
  从源料 `models/paint/flux2-klein-4b/models/AI-ModelScope--FLUX.2-Klein-4B-diffusers`
  重转：`to_qkv_mlp_proj` 拆分/重排为 comfy `linear1 [3h+mlp_first] / linear2` 布局，
  外挂调制折进块内；纯张量搬运（split/concat/转置），无数值变换。
  优点：一次转换全栈受益、零 comfy 侧改动。风险：映射细节需对读 comfy/ldm/flux 源码核定。
- **方案 B：comfy 侧适配层**
  仿 W3-C 先例（model_detection 补丁）：新增 Klein 原生布局的检测分支 + 加载期键映射
  （model_loading 阶段重排 state_dict）。
  优点：不动权重文件、对将来其他 Klein 系权重通用。风险：跟随 ComfyUI 升级维护补丁。
- 估时：A ≈ 0.5~1 天；B ≈ 1~2 天（含回归）。

## 五、接线点清单（修复落地时）

1. `comfy_paint_engine.py`：`_KLEIN_4B_BROKEN = False` + `COMFY_PAINT_MODELS` 恢复条目
   + `_build_workflow` D-LoRA 分支解除拦截
2. `comfy_model_map.json`：修正陈旧 `unet/flux-2-klein-4b.safetensors` 条目指向新产物
3. `models_manifest.json`：核对 flux2-klein-4b 条目 size/路径
4. 测试：`test_paint_model_switch_honesty.py` 的 skip 条件自动退役（翻 flag 后守卫测试跳过）

## 六、风险

- 转换映射错位 → 出图花屏/不收敛：验收第 1 条目验把关
- 4b 回归可选后与 9b 抢底座：现有智能路由口径复核（auto 模式不主动选 4b）
- ComfyUI 升级再次改变 Flux2 布局（方案 B 的长期成本）
