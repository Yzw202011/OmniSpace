"""VACE split 布局 OOM 复现实验（2026-08-22 e2e 四测验尸）。

独立进程复现 video_engine 的 split 编排链：装载(DiT+VAE 常驻) →
T5 编码窗口(DiT 下卡/T5 int8 上卡/释放/回卡) → VACE I2V 全量生成，
逐步打点 allocated/peak/free，定位 28.41GiB 的真实构成。
"""
import gc
import sys
import time

import torch

MODELS = r"models\video_gen\wan21-vace-1.3b"
IMG = r"logs\_frames\user_input.png"


def free_gb() -> float:
    return torch.cuda.mem_get_info()[0] / 2**30


def mark(tag: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {tag}: "
          f"alloc={torch.cuda.memory_allocated()/2**30:.2f}GB "
          f"peak={torch.cuda.max_memory_allocated()/2**30:.2f}GB "
          f"free={free_gb():.2f}GB", flush=True)


def main() -> int:
    from diffusers import WanVACEPipeline
    from PIL import Image
    from transformers import BitsAndBytesConfig, UMT5EncoderModel

    mark("start")
    pipe = WanVACEPipeline.from_pretrained(
        MODELS, torch_dtype=torch.float16, text_encoder=None)
    pipe.transformer.to("cuda")
    pipe.vae.to("cuda")
    print(f"vae dtype={pipe.vae.dtype} transformer dtype="
          f"{pipe.transformer.dtype}", flush=True)
    mark("resident(DiT+VAE on cuda)")
    torch.cuda.reset_peak_memory_stats()

    # ── T5 编码窗口（DiT 下卡 → T5 int8 → 编码 → 释放 → DiT 回卡）──
    pipe.transformer.to("cpu")
    gc.collect()
    torch.cuda.empty_cache()
    mark("DiT off to cpu")
    qcfg = BitsAndBytesConfig(load_in_8bit=True)
    te = UMT5EncoderModel.from_pretrained(
        rf"{MODELS}\text_encoder", quantization_config=qcfg,
        torch_dtype=torch.float16)
    pipe.text_encoder = te
    mark("T5 int8 loaded")
    pe = pipe.encode_prompt(
        prompt="a girl dancing beside a villa swimming pool, "
               "graceful movements, sunny day",
        negative_prompt="",
        do_classifier_free_guidance=True,
        max_sequence_length=512,
        device=torch.device("cuda"),
        dtype=pipe.transformer.dtype)
    mark("T5 encoded")
    pipe.text_encoder = None
    del te
    gc.collect()
    torch.cuda.empty_cache()
    pipe.transformer.to("cuda")
    mark("T5 released, DiT back to cuda")

    # ── VACE I2V 生成（同 e2e 参数：832x464 / 81 帧 / 30 步）──
    w, h, n = 832, 464, 81
    first = Image.open(IMG).convert("RGB").resize((w, h))
    blank = Image.new("RGB", (w, h), (0, 0, 0))
    video = [first] + [blank] * (n - 1)
    mask = ([Image.new("L", (w, h), 0)]
            + [Image.new("L", (w, h), 255)] * (n - 1))

    def cb(_p, i, _t, kw):
        if i % 5 == 0 or i < 2:
            mark(f"denoise step {i}")
        return kw

    t0 = time.time()
    try:
        out = pipe(prompt_embeds=pe[0], negative_prompt_embeds=pe[1],
                   video=video, mask=mask, height=h, width=w,
                   num_frames=n, num_inference_steps=30,
                   guidance_scale=6.5,
                   callback_on_step_end=cb,
                   callback_on_step_end_tensor_inputs=[])
        n_frames = len(out.frames[0])
        print(f"DONE frames={n_frames} elapsed={time.time()-t0:.0f}s",
              flush=True)
        mark("final")
        return 0
    except torch.cuda.OutOfMemoryError as exc:
        print(f"OOM: {exc}", flush=True)
        mark("OOM site")
        return 1


if __name__ == "__main__":
    sys.exit(main())
