"""显存/内存策略阈值单源（V9 尾款① 2026-09-09；显存调度机制批1
2026-09-10 扩展 GPU 侧）。

历史手搓阈值搬家集中——**首轮只搬家不改值**：每个数都是实测
事故标定的（依据见各常量注），调档/按硬件分档只动本文件；消费方
一律 import，禁止再写魔法数。

搬家清单（2026-09-09，搬家后原处改 import）：
  PAINT_QWEN_RAM_FLOOR_GB   22.0  ← api/draw.py（qwen 双语底座路由闸）
  QWEN_GGUF_RAM_FLOOR_GB    15.5  ← inference/gen_router.py（GGUF 常驻线）
  VLLM_RAM_HEADROOM_GB       3.0  ← engines/vllm_service.py（装载余量）
  PAINT_LOW_VRAM_FALLBACK_GB 4.0  ← inference/paint_engine.py（降级线）

GPU 侧搬家清单（显存调度机制批1，2026-09-10，方案真源=
docs/显存调度机制方案-2026-09-10.md §3.2）：
  VLLM_UTIL_DEFAULT          0.85 ← backends/vllm_backend.py（小权档案）
  VLLM_UTIL_LARGE_WEIGHTS    0.87 ← backends/vllm_backend.py（≥8GB 档）
  VLLM_UTIL_MTP              0.92 ← backends/vllm_backend.py（MTP 轻载档）
  VLLM_FLOOR_OVERHEAD_GB     4.2  ← backends/vllm_backend.py（让档下限）
  VLLM_MTP_EXTRA_GB          1.3  ← backends/vllm_backend.py（MTP 加成）
  VLLM_ADMISSION_FACTOR      0.98 ← engines/vllm_service.py（准入系数）
  TRAINING_MIN_FREE_GB      10.0  ← lora_training_service.py（训练闸）
  COMFY_IDLE_SHUTDOWN_S    300.0  ← inference/comfy_proc.py（空闲关默认）
  WAKE_DEBOUNCE_S           10.0  ← image_queue.py（唤醒去抖窗）

口径备注：config.yaml 已单源的项（gpu_vram_* 三档、
comfyui.idle_shutdown_seconds、dialog_idle_unload_seconds）不在此
重复定义——配置真源在 config.yaml，本文件只收编**代码里的硬编码
字面量**；COMFY_IDLE_SHUTDOWN_S 是 config 缺失时的代码默认值。

对拍防线：tests/unit/test_vram_policy.py 逐值锁定历史字面量——改值
必先过那道测试（提醒改的人想清楚是不是真要动实测标定值）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DialogTier:
    """对话档位（D2=A 常态共存档，批4 2026-09-10）。

    vram_gb=装载需求（实测口径，供准入选档/回退链判定）；
    mode=推理通道：vllm（py313 子进程）/ llama（llama-server 子进程，
    GGUF）/ inproc（py310 进程内 transformers）。
    """

    model_id: str
    vram_gb: float
    mode: str
    note: str


# 对话档位表（显存调度机制批4，方案 §3.5；数值依据见 §1.3 供需表）
# 2026-09-10 用户令删除 qwen35-9b-gguf-q4km（Q4 量化思考退化，
# 权重已清 5.5G）——共存档随之移除；恢复方法见 dialog_engine 候选表注
DIALOG_TIERS: tuple[DialogTier, ...] = (
    DialogTier("qwen35-9b-w4a16", 14.9, "vllm",
               "主力独占档（权重10.95+MTP0.49+KV+激活，需近乎空卡）"),
    DialogTier("qwen3-vl-8b-awq", 14.8, "vllm",
               "次选（vllm_bench 实测峰值 14.8GB）"),
    DialogTier("qwen3-vl-4b", 9.0, "inproc",
               "兜底（驻留实测 9GB）"),
)

PAINT_QWEN_RAM_FLOOR_GB = 22.0
"""draw.py qwen 双语底座路由闸（2026-08-23 实测定界）：qwen GGUF
峰值 17.3GB（权重 12.31+开销 3+生成 2），pageable 换页边界效应显著
——20/20.9GB 起步两次 device mismatch 失败、22.5GB 成功，22 是稳定
下界；低于线走翻译兜底+提示词工程（四轮对照实验定论）。"""

QWEN_GGUF_RAM_FLOOR_GB = 15.5
"""gen_router qwen-image GGUF 路由线：权重常驻 RAM ~12.31GB + 推理
开销 ~3GB（2026-08-23 OOM 死亡事故实测边界），可用 RAM 低于此线跳过。"""

VLLM_RAM_HEADROOM_GB = 3.0
"""vllm_service 装载 RAM 余量：权重体积 + 3GB（子进程 Python/CUDA
开销口径）；不足等 60s 后诚实拒绝（RADAR_PRE_LEAK_64 静默死亡取证
驱动，2026-09-02）。"""

PAINT_LOW_VRAM_FALLBACK_GB = 4.0
"""paint_engine 低显存降级线：空闲显存 ≥ 此值时以
sequential_cpu_offload 降级加载（速度换可用性），低于则诚实拒绝。"""


def vllm_ram_needed_gb(weights_gb: float) -> float:
    """vLLM 装载所需 RAM（权重 + 余量，vllm_service 准入闸口径）。"""
    return float(weights_gb) + VLLM_RAM_HEADROOM_GB


# ══ GPU 侧常量（显存调度机制批1，2026-09-10 搬家集中）═══════════

VLLM_UTIL_DEFAULT = 0.85
"""vLLM gpu_memory_utilization 默认档（小权重 <8GB，8K 上下文）。"""

VLLM_UTIL_LARGE_WEIGHTS = 0.87
"""权重 ≥8GB 档：util 0.85 装载后 KV 仅剩 ~1.0GB 而 8K 上下文需
1.5GB 启动失败（2026-08-25 W4A16 14B 实测）→ 该档降 4K 上下文 +
util 提至 0.87（16GB 卡装载后 KV ~1.5GB，4K 需 0.75GB，余量足）。"""

VLLM_UTIL_MTP = 0.92
"""MTP + 大权重轻载窗口档（V6 2026-09-09 冒烟实测：9B+MTP+前缀
缓存 util 0.86 时 KV=-0.81 起不来、0.92 过线）。"""

VLLM_FLOOR_OVERHEAD_GB = 4.2
"""vLLM 装载可行下限加成：权重 + 4.2（= 运行开销 3.4 + 4K KV
预算 0.8，2026-08-25 实测口径）——vllm_backend 让档判定下限。"""

VLLM_MTP_EXTRA_GB = 1.3
"""MTP 草稿层 + 图画像额外显存（V6 冒烟实测）——准入线同步抬高，
宁可早拒不让 vLLM 装到一半才死。"""

VLLM_ADMISSION_FACTOR = 0.98
"""vLLM 准入闸安全系数：预分配需求 = 整卡 × util × 0.98
（vllm_service._vram_admission_wait，2026-09-02 自 start() 抽取）。"""

TRAINING_MIN_FREE_GB = 10.0
"""QLoRA 训练前空闲显存下限（lora_training_service 训练闸）。"""

COMFY_IDLE_SHUTDOWN_S = 300.0
"""ComfyUI 空闲自动关闭默认值（秒）；config.yaml
comfyui.idle_shutdown_seconds 可覆盖（0=禁用进程常驻热启动）。"""

WAKE_DEBOUNCE_S = 10.0
"""队列排空 → 唤醒 vLLM 去抖窗（V9-β 2026-09-09）：客户端逐个
提交时任务间隙即排空，排空即唤醒会被紧邻下一任务撞死（15:37:04
实测 booting 被功能锁门禁拒绝且无人重试，state 卡 unloaded）。
到点仍空闲才真唤醒。"""
