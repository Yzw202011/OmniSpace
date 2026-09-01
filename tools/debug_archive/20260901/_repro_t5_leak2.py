"""T5 窗口显存膨胀决定性实验 v2（2026-08-22）。

对照数据：
  独立实验（_repro_t5_leak.py）：裸 forward 后 del+gc+empty 完美回收 0.01GB
  主实验  （_repro_vace_split.py）：pipe.encode_prompt 后 +10.7GB 且
           del+gc+empty 一字节不释放（19.75GB 残留），alloc 19.99GB >
           物理 15.92GB = sysmem fallback 铁证

本实验复刻主实验全链（pipeline + text_encoder 挂载 + encode_prompt），
细分打点定位 +10.7GB 发生步，泄漏扫描输出引用者。
"""
import gc
import sys
import time

import torch

MODELS = r"models\video_gen\wan21-vace-1.3b"


def stats(tag: str) -> None:
    alloc = torch.cuda.memory_allocated() / 2**30
    peak = torch.cuda.max_memory_allocated() / 2**30
    free = torch.cuda.mem_get_info()[0] / 2**30
    print(f"[{time.strftime('%H:%M:%S')}] {tag}: alloc={alloc:.2f}GB "
          f"peak={peak:.2f}GB free={free:.2f}GB", flush=True)


def leak_hunt(min_mb: float = 200.0) -> None:
    """扫描大 CUDA 张量并输出引用者类型。"""
    print("--- leak hunt ---", flush=True)
    seen: set[int] = set()
    found = 0
    for obj in gc.get_objects():
        try:
            if (torch.is_tensor(obj) and obj.is_cuda
                    and obj.untyped_storage().data_ptr() not in seen):
                mb = obj.untyped_storage().nbytes() / 2**20
                if mb > min_mb:
                    seen.add(obj.untyped_storage().data_ptr())
                    found += 1
                    refs = gc.get_referrers(obj)
                    ref_desc = []
                    for r in refs[:5]:
                        d = type(r).__name__
                        if isinstance(r, dict):
                            keys = [k for k in r.keys()
                                    if not isinstance(k, (int, str))] \
                                or list(r.keys())[:4]
                            d += f"keys={str(keys)[:100]}"
                        elif isinstance(r, (list, tuple)):
                            d += f"len={len(r)}"
                        elif isinstance(r, torch.nn.Module):
                            d += f"[{type(r).__module__}.{d}]"
                        ref_desc.append(d)
                    print(f"CUDA tensor {mb:.0f}MB shape={tuple(obj.shape)} "
                          f"dtype={obj.dtype} <- refs: {ref_desc}",
                          flush=True)
        except Exception:
            pass
    print(f"big-tensor storages: {found}", flush=True)


def main() -> int:
    from diffusers import WanVACEPipeline
    from transformers import BitsAndBytesConfig, UMT5EncoderModel

    stats("start")
    pipe = WanVACEPipeline.from_pretrained(
        MODELS, torch_dtype=torch.float16, text_encoder=None)
    pipe.transformer.to("cuda")
    pipe.vae.to("cuda")
    stats("resident(DiT+VAE on cuda)")

    # ── T5 编码窗口：复刻主实验 ──
    pipe.transformer.to("cpu")
    gc.collect()
    torch.cuda.empty_cache()
    stats("DiT off to cpu")
    qcfg = BitsAndBytesConfig(load_in_8bit=True)
    te = UMT5EncoderModel.from_pretrained(
        rf"{MODELS}\text_encoder", quantization_config=qcfg,
        torch_dtype=torch.float16)
    pipe.text_encoder = te
    torch.cuda.reset_peak_memory_stats()
    stats("T5 int8 loaded")

    # 细分 1：绕过 pipeline，直接用真实 tokenizer mask 裸 forward
    tok = pipe.tokenizer
    ti = tok(["a girl dancing beside a villa swimming pool, graceful "
              "movements, sunny day", ""],
             padding="max_length", max_length=512, truncation=True,
             return_attention_mask=True, return_tensors="pt")
    ids, mask = ti.input_ids, ti.attention_mask
    print(f"real mask sums: {mask.sum(dim=1).tolist()}", flush=True)
    stats("tokenized")
    with torch.no_grad():
        out = te(input_ids=ids.to("cuda"), attention_mask=mask.to("cuda"))
    stats("raw forward with real mask")
    del out, ids, mask
    gc.collect()
    torch.cuda.empty_cache()
    stats("raw forward released")

    # 细分 2：走完整 encode_prompt（CFG 两次 batch=1 调用）
    pe = pipe.encode_prompt(
        prompt="a girl dancing beside a villa swimming pool, "
               "graceful movements, sunny day",
        negative_prompt="",
        do_classifier_free_guidance=True,
        max_sequence_length=512,
        device=torch.device("cuda"),
        dtype=pipe.transformer.dtype)
    stats("encode_prompt done")
    print(f"pe type={type(pe).__name__}", flush=True)
    if isinstance(pe, tuple):
        for i, p in enumerate(pe):
            print(f"  pe[{i}]: {tuple(p.shape)} {p.dtype} "
                  f"{p.numel()*p.element_size()/2**20:.1f}MB", flush=True)

    # ── 释放链：逐步打点 ──
    pipe.text_encoder = None
    stats("pipe.text_encoder=None")
    del te
    stats("del te")
    del pe
    gc.collect()
    stats("gc.collect")
    torch.cuda.empty_cache()
    stats("empty_cache")

    if torch.cuda.memory_allocated() / 2**30 > 1.0:
        leak_hunt()
    stats("end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
