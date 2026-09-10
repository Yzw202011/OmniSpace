"""角色 LoRA 一键训练脚本（LoRA 训练路线封装，2026-09-10）。

流程（对应 09-10 手工验证通过的全链）：
  ① 数据集生成：经后端 draw/img2img 以 portrait 为锚生成 8 张多角度图
  ② 组装数据集目录 + 触发词标签
  ③ Musubi Tuner：缓存 latents（VAE）→ 缓存文本编码（Qwen3-4B）
  ④ 训练 400 步（klein-base-4b，fp8+gc，约 18 分钟）
  ⑤ 产物拷入角色目录 lora.safetensors（关键帧管线自动检测挂载）

用法：
  runtime/py310/python.exe scripts/make_character_lora.py \
      --char-dir "E:/OmniSpace/data/comic_assets/<proj>/characters/夏沐沐" \
      --trigger "xia mumu"

前置：后端运行中（步骤①要 API）；Musubi venv 已建（tools/musubi-tuner/venv）；
底座三件在 models/uno/（flux-2-klein-4b / flux2_ae / qwen_3_4b）。
纪律：GPU 活动门——训练占 GPU 约 20 分钟，跑前核查后端无在飞任务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / "tools" / "musubi-tuner" / "venv" / "Scripts" / "python.exe"
MUSUBI_SRC = ROOT / "tools" / "musubi-tuner" / "src" / "musubi_tuner"
VAE_PATH = ROOT / "models" / "uno" / "flux2_ae.safetensors"
TE_PATH = ROOT / "models" / "uno" / "qwen_3_4b.safetensors"
DIT_PATH = ROOT / "models" / "uno" / "flux-2-klein-4b.safetensors"
BACKEND = "http://127.0.0.1:5800"

VIEWS = [
    "full body front view standing pose, head to toe",
    "full body side profile view standing",
    "upper body portrait, gentle smile",
    "upper body portrait, serious expression",
    "three quarter view walking, dynamic pose",
    "back view, hair detail visible",
    "close-up face, looking at viewer",
    "upper body portrait, gentle smile",
]


def _api_post(path: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        f"{BACKEND}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _api_get(path: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(f"{BACKEND}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def _wait_task(task_id: str, timeout_s: int = 600) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            d = _api_get(f"/api/v1/draw/result/{task_id}").get("data") or {}
            if d.get("status") in ("done", "error"):
                return d
        except Exception:  # noqa: BLE001 - 轮询
            pass
        time.sleep(10)
    return {"status": "timeout"}


def step_generate(trigger: str, base_b64: str) -> list[Path]:
    """① 生成多角度数据集（串行提交防内存叠载）。"""
    out: list[Path] = []
    for i, desc in enumerate(VIEWS):
        d = _api_post("/api/v1/draw/img2img", {
            "prompt": f"anime illustration, Chinese teenage girl {trigger}, "
                      f"{desc}, consistent character, clean background",
            "init_image": base_b64, "strength": 0.55,
            "model": "flux2-klein-9b", "width": 832, "height": 832,
            "steps": 20, "seed": 100 + i}, timeout=60)
        tid = (d.get("data") or {}).get("task_id")
        if not tid:
            print(f"  [{i + 1}/{len(VIEWS)}] 提交失败: {str(d)[:120]}")
            continue
        result = _wait_task(tid)
        fp = result.get("file_path", "")
        if result.get("status") == "done" and fp:
            out.append(Path(fp))
            print(f"  [{i + 1}/{len(VIEWS)}] done → {Path(fp).name}")
        else:
            print(f"  [{i + 1}/{len(VIEWS)}] {result.get('status')} 失败")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="角色 LoRA 一键训练")
    parser.add_argument("--char-dir", required=True,
                        help="角色资产目录（含 portrait.png）")
    parser.add_argument("--trigger", default="",
                        help="触发词（默认 char_<md5-6>，建议易读拼音）")
    parser.add_argument("--steps", type=int, default=400)
    parser.add_argument("--skip-generate", action="store_true",
                        help="跳过①，复用数据集目录已有图片")
    parser.add_argument("--no-install", action="store_true",
                        help="不拷贝产物到角色目录（仅训练）")
    args = parser.parse_args()

    char_dir = Path(args.char_dir)
    if not (char_dir / "portrait.png").is_file():
        print(f"[abort] {char_dir} 无 portrait.png")
        return 2
    dir_hash = hashlib.md5(str(char_dir).encode()).hexdigest()[:6]
    trigger = args.trigger or f"char_{dir_hash}"
    ds_dir = ROOT / "data" / "lora_dataset" / f"char_{dir_hash}"
    out_dir = ds_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[lora] 角色={char_dir.name} 触发词={trigger!r} 数据集={ds_dir}")

    # ① 数据集生成
    images: list[Path] = []
    if args.skip_generate:
        images = sorted(ds_dir.glob("img_*.png"))
        print(f"[1] 复用已有图片 {len(images)} 张")
    else:
        print(f"[1] 生成 {len(VIEWS)} 张多角度数据集（约 6 分钟）…")
        import base64
        with open(char_dir / "portrait.png", "rb") as f:
            b64 = f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"
        images = step_generate(trigger, b64)
        if len(images) < 5:
            print(f"[abort] 有效图不足 5 张（{len(images)}），放弃训练")
            return 1

    # ② 组装目录 + 标签
    ds_dir.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(images):
        dst = ds_dir / f"img_{i:02d}.png"
        if img.resolve() != dst.resolve():
            shutil.copy(img, dst)
        (ds_dir / f"img_{i:02d}.txt").write_text(
            f"{trigger}, anime illustration, consistent character",
            encoding="utf-8")
    n = len(list(ds_dir.glob("img_*.png")))
    print(f"[2] 数据集就绪: {n} 图 + {n} 标签")

    # ③ dataset TOML
    toml_path = ds_dir / "dataset.toml"
    ds_escaped = str(ds_dir).replace("\\", "/")
    toml_path.write_text(
        f"[general]\nresolution = [1024, 1024]\nbatch_size = 1\n"
        f"enable_bucket = true\ncaption_extension = \".txt\"\n\n"
        f"[[datasets]]\nimage_directory = \"{ds_escaped}\"\n"
        f"num_repeats = 10\n", encoding="utf-8")

    # ③a 缓存 latents（klein 架构）
    print("[3a] 缓存 VAE latents…")
    r = subprocess.run(
        [str(VENV_PY), str(MUSUBI_SRC / "flux_2_cache_latents.py"),
         "--dataset_config", str(toml_path), "--vae", str(VAE_PATH),
         "--model_version", "klein-base-4b"],
        capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"[abort] latents 缓存失败: {r.stdout[-300:]}{r.stderr[-300:]}")
        return 1
    # ③b 缓存文本编码
    print("[3b] 缓存文本编码（Qwen3-4B）…")
    env = {**os.environ, "HF_ENDPOINT": "https://hf-mirror.com"}
    r = subprocess.run(
        [str(VENV_PY), str(MUSUBI_SRC / "flux_2_cache_text_encoder_outputs.py"),
         "--dataset_config", str(toml_path), "--text_encoder", str(TE_PATH),
         "--model_version", "klein-base-4b"],
        capture_output=True, text=True, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        print(f"[abort] TE 缓存失败: {r.stdout[-300:]}{r.stderr[-300:]}")
        return 1

    # ④ 训练
    print(f"[4] 训练 {args.steps} 步（约 {args.steps * 3 // 60} 分钟）…")
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    r = subprocess.run(
        [str(VENV_PY), str(MUSUBI_SRC / "flux_2_train_network.py"),
         "--mixed_precision=bf16", f"--dit={DIT_PATH}",
         f"--vae={VAE_PATH}", f"--text_encoder={TE_PATH}",
         f"--dataset_config={toml_path}", "--model_version=klein-base-4b",
         "--sdpa", "--timestep_sampling=flux2_shift",
         "--weighting_scheme=none", "--optimizer_type=adamw8bit",
         "--learning_rate=1e-4", "--network_module=networks.lora_flux_2",
         "--network_dim=16", "--network_alpha=16",
         f"--max_train_steps={args.steps}",
         "--max_data_loader_n_workers=2", "--persistent_data_loader_workers",
         "--gradient_checkpointing", "--fp8_base", "--fp8_scaled",
         "--fp8_text_encoder", f"--output_dir={out_dir}",
         f"--output_name={char_dir.name}_v1"],
        capture_output=True, text=True, cwd=str(ROOT), env=env)
    lora_file = out_dir / f"{char_dir.name}_v1.safetensors"
    if r.returncode != 0 or not lora_file.is_file():
        print(f"[abort] 训练失败: {r.stdout[-400:]}{r.stderr[-200:]}")
        return 1
    print(f"[4] 训练完成 → {lora_file}")

    # ⑤ 产物入角色目录
    if not args.no_install:
        dst = char_dir / "lora.safetensors"
        shutil.copy(lora_file, dst)
        print(f"[5] 已安装 → {dst}（关键帧管线将自动检测挂载）")
    print("[done] 全流程完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
