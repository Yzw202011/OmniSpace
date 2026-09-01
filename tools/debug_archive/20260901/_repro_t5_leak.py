"""bnb int8 T5 显存泄漏定位实验（2026-08-22）。

del + gc + empty_cache 后 allocated 不降（20GB 级），定位持引用者。
"""
import gc
import sys

import torch


def alloc_gb() -> float:
    return torch.cuda.memory_allocated() / 2**30


def main() -> int:
    from transformers import BitsAndBytesConfig, UMT5EncoderModel

    te_dir = r"models\video_gen\wan21-vace-1.3b\text_encoder"
    qcfg = BitsAndBytesConfig(load_in_8bit=True)
    te = UMT5EncoderModel.from_pretrained(
        te_dir, quantization_config=qcfg, torch_dtype=torch.float16)
    print(f"loaded: alloc={alloc_gb():.2f}GB", flush=True)

    # tokenizer 走 slow T5 路径报错，直接手工构造 batch=2/seq=512
    torch.manual_seed(0)
    input_ids = torch.randint(1000, 50000, (2, 512)).to("cuda")
    attn = torch.ones(2, 512, dtype=torch.long).to("cuda")
    with torch.no_grad():
        out = te(input_ids=input_ids, attention_mask=attn)
    print(f"forward done: out={tuple(out.last_hidden_state.shape)} "
          f"alloc={alloc_gb():.2f}GB", flush=True)

    # 释放三件套
    del out, input_ids, attn
    del te
    gc.collect()
    torch.cuda.empty_cache()
    print(f"after del+gc+empty: alloc={alloc_gb():.2f}GB", flush=True)

    if alloc_gb() > 1.0:
        # 泄漏：扫描大 CUDA 张量的引用者
        print("--- leak hunt ---", flush=True)
        seen = set()
        for obj in gc.get_objects():
            try:
                if (torch.is_tensor(obj) and obj.is_cuda
                        and obj.untyped_storage().data_ptr() not in seen):
                    mb = obj.untyped_storage().nbytes() / 2**20
                    if mb > 200:
                        seen.add(obj.untyped_storage().data_ptr())
                        refs = gc.get_referrers(obj)
                        ref_desc = []
                        for r in refs[:4]:
                            d = type(r).__name__
                            if isinstance(r, dict):
                                keys = [k for k in r.keys()
                                        if not isinstance(k, (int, str))] \
                                       or list(r.keys())[:3]
                                d += f"{str(keys)[:80]}"
                            elif isinstance(r, (list, tuple)):
                                d += f"len={len(r)}"
                            ref_desc.append(d)
                        print(f"CUDA tensor {mb:.0f}MB {tuple(obj.shape)} "
                              f"dtype={obj.dtype} <- refs: {ref_desc}",
                              flush=True)
            except Exception:
                pass
        print(f"total big-tensor storages: {len(seen)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
