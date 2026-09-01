"""六测产出 VL 客观取证（独立进程，qwen3-vl-4b）。"""
import sys
from pathlib import Path

import torch


def ask(model, proc, img_path: str, question: str) -> str:
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    msgs = [{"role": "user", "content": [
        {"type": "image", "image": img},
        {"type": "text", "text": question}]}]
    text = proc.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=True)
    inputs = proc(text=[text], images=[img], return_tensors="pt").to(
        model.device)
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=120,
                             do_sample=False, temperature=None)
    return proc.decode(out[0][inputs.input_ids.shape[1]:],
                       skip_special_tokens=True)


def main() -> int:
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
    mdir = r"models\qwen3-vl-4b"
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        mdir, dtype=torch.bfloat16, device_map="cuda")
    proc = AutoProcessor.from_pretrained(mdir)
    frames = [
        ("F1首帧", r"logs\_frames\six\frame_01.jpg",
         "用一句话客观描述画面内容和人物姿态动作"),
        ("F4后段", r"logs\_frames\six\frame_04.jpg",
         "用一句话客观描述画面内容和人物姿态，说明与站立静止照片是否有可见区别（如肢体位置变化）"),
        ("F5尾帧", r"logs\_frames\six\frame_05.jpg",
         "用一句话客观描述画面内容和人物姿态动作"),
    ]
    for tag, p, q in frames:
        if not Path(p).is_file():
            print(f"{tag}: 文件缺失 {p}", flush=True)
            continue
        try:
            print(f"{tag}: {ask(model, proc, p, q)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{tag}: 取证失败 {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
