"""立项①诊断：VL-4B+知识LoRA 卸载后显存是否即时回池（2026-10-25 事故）。

事故（backend.log 80735-80784）：热切换「先卸载切换」同步走了
backend.unload + 3×gc + empty_cache + ipc_collect，但设备空闲读数
此后 4 分钟纹丝不动 5.0GB（~9.5GB 未回池），把 9B 准入与降级链
候选同时饿死。静态排查已排除 backend 双引用/全局注册表，本脚本在
独立进程按事故同款装载形态实弹复现，二选一定谳：
  A) 回池 → 泄漏在应用层上下文（发送线程/异常帧/事件引用），往
     线程与引用方向再查；
  B) 不回池 → backend/引擎层引用泄漏实锤，按 referrer 链所指修。

用法（GPU 空闲窗口手动执行；占 ~9GB 显存约 1-2 分钟）：
  runtime\\py312\\python.exe tools/diag_vram_release_probe.py
"""
from __future__ import annotations

import gc
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _free_gb() -> tuple[float, float]:
    """(设备空闲 GB, 本进程 torch 已分配 GB)。"""
    import torch
    free_b, _total_b = torch.cuda.mem_get_info(0)
    return free_b / 2**30, torch.cuda.memory_allocated() / 2**30


def _gpu_procs() -> str:
    """外部进程视角快照（区分本进程泄漏 vs 他人占用）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "(空)"
    except Exception as exc:  # noqa: BLE001
        return f"(nvidia-smi 失败: {exc})"


def _hunt_cuda_holders() -> None:
    """仍不回池时：找驻留 CUDA tensor 的 referrer 链（定谳输出）。"""
    import torch
    tensors = [o for o in gc.get_objects()
               if isinstance(o, torch.Tensor) and o.is_cuda]
    tensors.sort(key=lambda t: t.numel() * t.element_size(), reverse=True)
    total = sum(t.numel() * t.element_size() for t in tensors) / 2**30
    print(f"  驻留 CUDA tensor: {len(tensors)} 个 / 共 {total:.2f}GB")
    for t in tensors[:5]:
        sz = t.numel() * t.element_size() / 2**30
        print(f"  - tensor {tuple(t.shape)} {t.dtype} {sz:.2f}GB")
        seen: set[int] = set()
        frontier: list[object] = [t]
        for depth in range(3):
            nxt: list[object] = []
            for obj in frontier:
                for ref in gc.get_referrers(obj):
                    if id(ref) in seen:
                        continue
                    seen.add(id(ref))
                    nxt.append(ref)
            if nxt:
                names: list[str] = []
                for n in nxt[:4]:
                    if isinstance(n, dict):
                        keys = [k for k in n.keys() if isinstance(k, str)][:3]
                        names.append(f"dict{keys}")
                    elif isinstance(n, (list, tuple)):
                        names.append(f"{type(n).__name__}(len={len(n)})")
                    else:
                        names.append(type(n).__name__
                                     + getattr(n, "__name__", ""))
                print(f"      引用层{depth}: {' / '.join(names)}")
            frontier = nxt


def main() -> int:
    print("== 立项①显存回池诊断（2026-10-25 事故复现）==")
    print(f"GPU 进程快照(前):\n  {_gpu_procs().replace(chr(10), chr(10) + '  ')}")

    from src.config import MODELS_DIR
    from src.services.inference.backends.transformers_backend import (
        TransformersBackend,
    )

    model_dir = MODELS_DIR / "qwen3-vl-4b"
    if not model_dir.is_dir():
        print(f"✗ 模型目录不存在: {model_dir}")
        return 2

    base_free, _ = _free_gb()
    print(f"[0] 装载前设备空闲: {base_free:.2f}GB")

    backend = TransformersBackend("vl")
    t0 = time.monotonic()
    if not backend.load("qwen3-vl-4b", model_dir, 9.0):
        print(f"✗ 装载失败: {backend.last_error()}")
        return 2
    loaded_free, loaded_alloc = _free_gb()
    print(f"[1] 装载成功({time.monotonic() - t0:.0f}s) 空闲: "
          f"{loaded_free:.2f}GB（Δ{base_free - loaded_free:.2f}GB，"
          f"本进程分配 {loaded_alloc:.2f}GB）")

    # 事故同款卸载序列：backend.unload + 引擎 _release_cuda_memory
    backend.unload()
    from src.services.inference.dialog_engine import _release_cuda_memory
    _release_cuda_memory()

    released = False
    for wait_s in (0, 3, 10, 30, 60):
        if wait_s:
            time.sleep(wait_s if wait_s != 3 else 3)
        free, alloc = _free_gb()
        back = base_free - free
        print(f"[2+{wait_s:>2}s] 空闲: {free:.2f}GB（未回池 {back:.2f}GB，"
              f"本进程分配 {alloc:.2f}GB）")
        if back <= 1.0:
            released = True
            break

    print(f"GPU 进程快照(后):\n  {_gpu_procs().replace(chr(10), chr(10) + '  ')}")
    if released:
        print("✅ 定谳 A：独立进程回池正常——泄漏在应用层上下文"
              "（发送线程/异常帧/事件引用方向再查）")
        return 0
    print("⚠️ 定谳 B：独立进程也不回池——backend/引擎层引用泄漏实锤，"
          "referrer 链如下：")
    _hunt_cuda_holders()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
