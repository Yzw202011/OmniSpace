"""vLLM 启动 RAM 提交余量闸门回归（2026-09-02 静默死亡取证修复）。

事故链（18:49，WER 实证）：RAM 86%+ 冷启动 vLLM（deepseek-r1-14b-w4a16
9.2GB 权重 mmap 装载）→ 系统提交推过顶 → 后端原生层硬死（无 traceback，
Windows Error Reporting 记录 RADAR_PRE_LEAK_64 / OmniSpace-Backend.exe）
→ boot 看门狗补位重启。修复 = vllm_service.start 在显存闸门前增加
RAM 余量闸门（60s 短等重试，仍不足诚实拒绝）。

本文件钉两件事：
  1. _estimate_model_dir_gb：权重扩展名累计 + 缓存 + 1GB 下限；
  2. start() 源内存在 RAM 闸门块（防回退），且语义要点齐备
     （psutil.available、+3.0GB 开销、60s 短等、诚实拒绝 return False）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
# 2026-09-15 审计地雷拆除：原第二行把仓库根 pydeps/ 插到 sys.path 首
# 位——pydeps 里的 pydantic_core 源码垫片（无 py312 原生扩展）会劫持
# 后续任何 pydantic_core 导入，窄子集跑测必炸（全量靠导入顺序庇护）。
# 本测试所需 backend 包只需仓库根（首行），pydeps 实无必要。
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent))

from backend.engines.vllm_service import _estimate_model_dir_gb  # noqa: E402
from backend.services.vram_policy import VLLM_RAM_HEADROOM_GB  # noqa: E402

VLLM_PY = (Path(__file__).resolve().parents[2]
           / "engines" / "vllm_service.py")


def test_estimate_model_dir_gb_counts_weights_only(tmp_path) -> None:
    (tmp_path / "model.safetensors").write_bytes(b"x" * (2 * 2 ** 30 // 2))
    (tmp_path / "model-00002.safetensors").write_bytes(b"x" * (2 ** 30 // 2))
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "tokenizer.txt").write_text("t" * 1024)
    gb = _estimate_model_dir_gb(str(tmp_path))
    # 1.0 + 0.5 GB 权重 → ~1.5GB（json/txt 不计）
    assert 1.4 <= gb <= 1.6, f"权重体量估算应只计权重文件，got {gb}"


def test_estimate_model_dir_gb_floor_and_cache(tmp_path) -> None:
    (tmp_path / "config.json").write_text("{}")
    assert _estimate_model_dir_gb(str(tmp_path)) == 1.0  # 无权重 → 下限
    from backend.engines.vllm_service import _DIR_SIZE_CACHE
    assert str(tmp_path) in _DIR_SIZE_CACHE  # 结果已缓存


def test_start_has_ram_gate_block() -> None:
    src = VLLM_PY.read_text(encoding="utf-8")
    assert "RAM 提交余量闸门" in src, "start() 的 RAM 闸门块被移除"
    assert "RADAR_PRE_LEAK_64" in src, "取证注释被删（事故史锚点）"
    assert "_estimate_model_dir_gb" in src
    # 2026-09-09 V9 尾款①：+3.0 字面量随阈值搬至 services/vram_policy.py
    # （对拍锁定见 test_vram_policy.py），此处改锚调用点+单源值双保险
    assert "vllm_ram_needed_gb(_w_gb)" in src, (
        "RAM 需求 = 权重 + 3GB 余量（vram_policy.vllm_ram_needed_gb）语义不得改")
    assert VLLM_RAM_HEADROOM_GB == 3.0, "余量 3GB 单源值被改"
    assert "virtual_memory().available" in src, "必须量 available（非 total）"
    # 闸门必须先 RAM 后显存（RAM 检查更便宜，先拒先省）。2026-09-02
    # 热切换时序修复：显存闸门抽取为 _vram_admission_wait 复用（helper
    # 物理位置在 start() 之前，旧「文本序」锚过时）——改锚 start() 内
    # 的准入调用点，守卫语义不变：RAM 闸门先于显存准入执行。
    assert "_vram_admission_wait" in src, "显存准入复测 helper 被删"
    assert (src.index("RAM 提交余量闸门")
            < src.index("self._vram_admission_wait(gpu_memory_utilization)"))


# ── 收养态口径（2026-09-07 is_running/_kill_locked 修复哨兵）──────

def test_adopted_state_is_running_and_stop_safe():
    """收养态（_state=ready 懒创建、_proc=None）：is_running=True；
    stop 不因 _state 缺失炸 AttributeError（未收养实例同样安全）。"""
    import threading

    from backend.engines.vllm_service import VLLMService

    def bare_svc():
        s = VLLMService.__new__(VLLMService)  # 裸实例：无懒创建属性
        s._proc = None
        s._lock = threading.Lock()
        s._cancel_requested = False
        s._log_fh = None
        return s

    svc = bare_svc()
    # 未收养（无 _state）：is_running=False，stop 安全
    assert svc.is_running() is False
    assert svc.stop(timeout_s=0.5) is True  # 不抛 AttributeError

    # 收养态：懒创建 _state=ready → is_running=True
    svc2 = bare_svc()
    svc2._state = "ready"
    assert svc2.is_running() is True
