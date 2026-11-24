"""H3 社区技术 A/B 测试矩阵 runner（测试工具，不入生产链）。

用途：同分镜行(row_v7_01)+同参考图+同秒数(5s) → seed 由 md5(row_id|seconds)
天然同源，A/B 只差被测变量。产物落 data/videos/h3_ab/<test_id>.mp4，
逐条结果追加 results.jsonl（含耗时/显存峰值/成败/引擎标签），失败如实记录不中断。

换装原理：进程内 monkeypatch h3_chain_engine._CHAIN_H3_FILES（unet/clip/turbo_lora），
生产码零改动，进程退出即散。

用法（必须用 runtime/py312，禁裸 python）：
  runtime/py312/python.exe -X utf8 tools/testbed/h3_ab/run_matrix.py --group A
  runtime/py312/python.exe -X utf8 tools/testbed/h3_ab/run_matrix.py --only A0,U0
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(r"E:/OmniSpace")
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data/videos/h3_ab"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS = OUT_DIR / "results.jsonl"

ROW_ID = "row_v7_01"          # 宣传v7 分镜行（ABC 格式描述 + 多绑定资产，只读复用）
SECONDS = 5.0

# ── 矩阵定义（Phase A 零下载：引擎原生参数组合）──────────────────────
# steps_override/turbo_override/refine_pass 均为 run_h3_chain_task 原生参数
MATRIX: dict[str, dict] = {
    # 480p 组
    "A0": {"quality": "480p", "steps": 10, "turbo": False, "refine": False,
           "note": "基线=现行质量档(非turbo 10步)"},
    "A1": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
           "note": "现行速度档(turbo 6步+低σ精修)"},
    "A2": {"quality": "480p", "steps": 4, "turbo": True, "refine": False,
           "note": "turbo 4步下限"},
    "A3": {"quality": "480p", "steps": 8, "turbo": True, "refine": False,
           "note": "turbo 8步上限"},
    "A4": {"quality": "480p", "steps": 16, "turbo": False, "refine": False,
           "note": "非turbo 16步 sweep"},
    # 720p 组（720p 恒两阶段）
    "U0": {"quality": "720p", "steps": 10, "turbo": False, "refine": False,
           "note": "基线=现行质量档(two-stage)"},
    "U1": {"quality": "720p", "steps": 6, "turbo": True, "refine": False,
           "note": "现行速度档(turbo2s 6+2)"},
    "U2": {"quality": "720p", "steps": 8, "turbo": True, "refine": False,
           "note": "turbo2s 8+2 sweep"},
    # Phase B：LoRA 换装（对照=同步数的 A 组条目）
    "B1": {"quality": "480p", "steps": 8, "turbo": True, "refine": False,
           "note": "lightx2v ref2v v1.0 8步(对照A3)",
           "files": {"turbo_lora": "minimax_h3_ref2v_turbo_8step_v1.0_768p_bf16.safetensors"}},
    "B2": {"quality": "480p", "steps": 4, "turbo": True, "refine": False,
           "note": "larryvrh ckpt850 ema 4步(对照A2)",
           "files": {"turbo_lora": "minimax_h3_turbo_4step_ema_ckpt850.safetensors"}},
    # Phase C：基模换装（Singularity v1.3）
    "C1": {"quality": "480p", "steps": 6, "turbo": True, "refine": False,
           "note": "Singularity W4A8基模+咱turbo6(作者推荐配对形态)",
           "files": {"unet": "Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.safetensors"}},
    "C1n": {"quality": "480p", "steps": 10, "turbo": False, "refine": False,
            "note": "Singularity W4A8裸基模非turbo10(纯基模画质,对照A0)",
            "files": {"unet": "Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.safetensors"}},
    "U3": {"quality": "720p", "steps": 6, "turbo": True, "refine": False,
           "note": "Singularity W4A8 turbo2s 720p(对照U1)",
           "files": {"unet": "Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.safetensors"}},
    "C2": {"quality": "480p", "steps": 6, "turbo": True, "refine": False,
           "note": "Singularity GGUF Q5_K_M(turbo6)",
           "files": {"unet": "Minimax-H3-Singularity-Q5_K_M.gguf"},
           "node_patches": {"1": {"class_type": "UnetLoaderGGUF",
                                   "inputs": {"unet_name": "Singularity_Q5_K_M.gguf"}}}},
    # Phase D：960p ×2 闸解锁重测（进程内翻 manga.h3_two_pass_x2，不碰生产码）
    "D1": {"quality": "960p", "steps": 6, "turbo": True, "refine": False,
           "x2_gate": True,
           "note": "960p ×2 latent放大闸重测(turbo2p形态,16G两次崩点)"},
    # 批1（转义层升级）：远景行走注入 A/B + 回归（质量档配方）
    "P1on": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
             "row_id": "row_p1_far_01", "flags": {"h3_distant_walk": True},
             "note": "远景行走 注入开(质量档配方)"},
    "P1off": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
              "row_id": "row_p1_far_01",
              "note": "远景行走 对照臂(注入关)"},
    "P1reg": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
              "flags": {"h3_distant_walk": True},
              "note": "回归 row_v7_01(注入开,无远景应零注入)"},
    # 批2：动作链+VFX 四要素 A/B（弱描述=孤立动词标签行）
    "P2on": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
             "row_id": "row_p2_action_01",
             "flags": {"h3_action_chains": True, "h3_vfx_expand": True},
             "note": "动作链+VFX 注入开(质量档配方)"},
    "P2off": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
              "row_id": "row_p2_action_01",
              "note": "动作链+VFX 对照臂(注入关)"},
    # 批3：运镜五要素 A/B（时间码格式行，硬闸=进度单调性）
    "P3on": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
             "row_id": "row_p3_camera_01", "flags": {"h3_camera_expand": True},
             "note": "运镜五要素展开开(质量档配方)"},
    "P3off": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
              "row_id": "row_p3_camera_01",
              "note": "运镜对照臂(标签堆原样,=R18现状)"},
    # 批6：B段对照实验（零代码，config 开关 h3_keep_b_section 双臂×2 题材）
    "P6a-on": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
               "row_id": "row_p6_bheavy_01", "flags": {"h3_keep_b_section": True},
               "note": "客栈题材 B段保留(旧行为)"},
    "P6a-off": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
                "row_id": "row_p6_bheavy_01",
                "note": "客栈题材 B段吞除(现行)"},
    "P6b-on": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
               "row_id": "row_p6_bheavy_02", "flags": {"h3_keep_b_section": True},
               "note": "科幻题材 B段保留(旧行为)"},
    "P6b-off": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
                "row_id": "row_p6_bheavy_02",
                "note": "科幻题材 B段吞除(现行)"},
    # 批7：Rewriter 编译产物走 plain 路径渲染（模型路线 vs 咱转义层）
    "P7a": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
            "row_id": "row_p7_rw_inn",
            "note": "客栈 Rewriter 编译prompt(对照=P6a-off吞除现行)"},
    "P7b": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
            "row_id": "row_p7_rw_far",
            "note": "远景 Rewriter 编译prompt(对照=P1off)"},
    # 混合路线实弹 + 全局回归（四开关已翻默认）
    "REG1": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
             "note": "全局回归：四开关默认全开(row_v7_01)"},
    "P8distill": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
                  "row_id": "row_p6_bheavy_01",
                  "flags": {"h3_b_distill": True},
                  "note": "B段蒸馏臂(混合路线实弹, llama-server 用完即杀)"},
    "P8fallback": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
                   "row_id": "row_p6_bheavy_01",
                   "flags": {"h3_b_distill": True, "b_distill_port": 1},
                   "note": "降级臂(端口1不可用→熔断→回退吞除)"},
    "P8fb2": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
              "row_id": "row_p6_bheavy_01",
              "flags": {"h3_b_distill": True},
              "note": "降级臂v2(适配器临时改名→权重缺失短路→秒级回退吞除)"},
    # 身份闸补验（对抗版）：B 段含「掌柜的」诱因 + C 段主角=绑定主持人
    # + 蒸馏模板人物排除硬化——三重下验证 subject 锚定不被人设顶替
    "P9id": {"quality": "480p", "steps": 6, "turbo": True, "refine": True,
             "row_id": "row_p9_identity_01",
             "flags": {"h3_b_distill": True},
             "note": "身份闸对抗臂(B含掌柜诱因+C=绑定主持人+模板硬化)"},
}


def vram_peak_sampler(stop: threading.Event, out: dict) -> None:
    """后台线程每 2s 采一次 GPU 全卡已用显存（MiB），记峰值。"""
    while not stop.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            mb = int(r.stdout.strip().splitlines()[0])
            out["peak"] = max(out.get("peak", 0), mb)
        except Exception:  # noqa: BLE001 - 采样失败不影响主流程
            pass
        stop.wait(2.0)


def run_one(test_id: str, cfg: dict) -> None:
    from src.services.inference import h3_chain_engine as eng

    rec = {"test_id": test_id, "cfg": cfg, "started": time.strftime("%F %T")}
    # 换装（Phase B/C 用；Phase A 不触发）
    if cfg.get("files"):
        orig = dict(eng._CHAIN_H3_FILES)
        eng._CHAIN_H3_FILES.update(cfg["files"])
        rec["files_override"] = cfg["files"]
        print(f"[{test_id}] 换装: {cfg['files']}", flush=True)
        try:
            _execute(test_id, cfg, rec)
        finally:
            eng._CHAIN_H3_FILES.clear()
            eng._CHAIN_H3_FILES.update(orig)
    else:
        _execute(test_id, cfg, rec)


def _execute(test_id: str, cfg: dict, rec: dict) -> None:
    from src.services.inference.h3_chain_engine import run_h3_chain_task

    # 通用 config 旗标（批1 flags）+ x2 闸：进程内翻，跑完恢复
    x2 = bool(cfg.get("x2_gate"))
    flags = dict(cfg.get("flags") or {})
    if x2:
        flags["h3_two_pass_x2"] = True
    saved_flags: dict = {}
    if flags:
        from src import config as _cfgmod
        _manga = _cfgmod._cfg.setdefault("manga", {})
        saved_flags = {k: _manga.get(k) for k in flags}
        _manga.update(flags)

    # 节点补丁（Phase C GGUF 换 loader / Phase D 换采样器）：
    # 包一层 _render_graph，出图后改 class_type/inputs，再交提交
    node_patches = cfg.get("node_patches") or {}
    if node_patches:
        import src.services.inference.h3_chain_engine as eng_mod

        orig_render = eng_mod._render_graph

        def patched_render(*a, **kw):
            g = orig_render(*a, **kw)
            for nid, patch in node_patches.items():
                if "class_type" in patch:
                    g[nid]["class_type"] = patch["class_type"]
                g[nid]["inputs"].update(patch.get("inputs", {}))
            return g

        eng_mod._render_graph = patched_render
        print(f"[{test_id}] 节点补丁: {list(node_patches)}", flush=True)

    stop = threading.Event()
    sampler_out: dict = {}
    th = threading.Thread(target=vram_peak_sampler, args=(stop, sampler_out), daemon=True)
    th.start()
    t0 = time.time()
    last_report = [0.0]
    frac_hist: list[float] = []  # 批3：进度单调性硬闸取样（全量回写序列）

    def cb(frac: float, stage: str = "") -> None:
        frac_hist.append(float(frac))
        if frac - last_report[0] >= 0.25 or frac >= 1.0:
            last_report[0] = frac
            print(f"[{test_id}] {int(frac*100)}% {stage}", flush=True)

    try:
        result = run_h3_chain_task(
            task_id=f"ab_{test_id}",
            row_ids=[cfg.get("row_id") or ROW_ID], seconds=SECONDS,
            quality=cfg["quality"], progress_cb=cb,
            turbo_override=cfg.get("turbo"),
            steps_override=cfg.get("steps"),
            refine_pass=cfg.get("refine"))
        elapsed = time.time() - t0
        # 归档到测试目录（引擎产出在 data/videos/<row16>/ab_<id>.mp4）
        src = Path(result["file_path"])
        dst = OUT_DIR / f"{test_id}.mp4"
        if src.exists() and src != dst:
            dst.unlink(missing_ok=True)
            src.rename(dst)
        rec.update({
            "status": "done", "elapsed_s": round(elapsed, 1),
            "vram_peak_gb": round(sampler_out.get("peak", 0) / 1024, 2),
            "engine_label": result.get("engine_label", ""),
            "resolution": result.get("resolution", ""),
            "duration_s": result.get("duration_seconds", 0),
            "file": str(dst),
            # 批3 硬闸：进度回写最大倒退幅（0=严格单调；允许 1e-6 浮点噪声）
            "progress_max_drop": round(max(
                (p - c for p, c in zip(frac_hist, frac_hist[1:], strict=False)), default=0.0), 6),
            "progress_samples": len(frac_hist)})
        print(f"[{test_id}] DONE {elapsed:.0f}s peak={rec['vram_peak_gb']}GB "
              f"label={rec['engine_label']}", flush=True)
    except Exception as exc:  # noqa: BLE001 - 单测失败如实记录不中断矩阵
        rec.update({
            "status": "error", "elapsed_s": round(time.time() - t0, 1),
            "vram_peak_gb": round(sampler_out.get("peak", 0) / 1024, 2),
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "trace_tail": traceback.format_exc()[-800:]})
        print(f"[{test_id}] FAIL {rec['error']}", flush=True)
    finally:
        stop.set()
        if saved_flags:
            try:
                from src import config as _cfgmod
                _manga = _cfgmod._cfg.setdefault("manga", {})
                for _k, _v in saved_flags.items():
                    if _v is None:
                        _manga.pop(_k, None)
                    else:
                        _manga[_k] = _v
            except Exception:  # noqa: BLE001
                pass
        if node_patches:
            try:
                eng_mod._render_graph = orig_render
            except Exception:  # noqa: BLE001
                pass
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["A", "U", "B", "C", "D"], default=None)
    ap.add_argument("--only", default=None, help="逗号分隔 test_id")
    args = ap.parse_args()

    todo = {k: v for k, v in MATRIX.items()
            if (args.only and k in args.only.split(","))
            or (args.group and k.startswith(args.group))}
    if not todo:
        print("no tests matched", flush=True)
        return
    print(f"matrix: {list(todo)}", flush=True)
    for tid, cfg in todo.items():
        print(f"=== {tid}: {cfg['note']} ===", flush=True)
        run_one(tid, cfg)
    print("matrix finished", flush=True)


if __name__ == "__main__":
    main()
