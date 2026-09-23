"""klein 人脸精修 POC（2026-09-23，FaceDetailer 替代方案——Impact-Subpack
装不上，用 insightface + img2img denoise 自研等效 Pass）。

流程：demo_9b.png（全身像）→ insightface 检测主脸 bbox → 裁剪放大至
~768px → LoadImage 落盘 → klein img2img（VAEEncode 像素初始化 +
denoise 0.5，脸部特写提示词 + 美学词块主导）→ 精修脸图 → PIL 椭圆
羽化贴回全身图 → face_refined.png
"""
import json
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

BASE = "http://127.0.0.1:8190"
SRC = Path(r"E:\OmniSpace\logs\four_models\demo_9b.png")
OUT = Path(r"E:\OmniSpace\logs\four_models\demo_9b_facedetail.png")
INPUT_DIR = Path(r"E:\OmniSpace\data\comfyui\input")

FACE_PROMPT = ("3D游戏CG风格，写实二次元游戏质感，杰作，最佳画质，精致唯美的"
               "东亚年轻少女面部特写，清秀鹅蛋脸，大而明亮的杏眼，清澈有神的"
               "眼神，皮肤细腻白皙水光感，精致小巧的鼻子，自然柔和的嘴唇，乌黑"
               "低马尾发丝，额前轻薄碎发，平静纯净的眼神，电影级人像打光，锐利"
               "细节")
NEG = "丑陋，畸形五官，呆滞眼神，皮肤粗糙，塑料感，模糊，低质量"


def detect_face(img: Image.Image):
    """insightface scrfd 检测最大脸 bbox（x1,y1,x2,y2）。"""
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="antelopev2",
                       root=r"C:\Users\Administrator\.insightface",
                       providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))
    arr = np.array(img.convert("RGB"))[:, :, ::-1]  # RGB→BGR
    faces = app.get(arr)
    if not faces:
        return None
    main = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    return [int(v) for v in main.bbox]


def crop_face_region(img: Image.Image, bbox, pad=1.9, target=768):
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    side = int(max(w, h) * pad)
    half = side // 2
    left, top = max(0, cx - half), max(0, cy - half)
    right, bottom = min(img.width, left + side), min(img.height, top + side)
    left, top = max(0, right - side), max(0, bottom - side)
    crop = img.crop((left, top, right, bottom)).resize((target, target), Image.LANCZOS)
    return crop, (left, top, right, bottom)


def refine_face(crop: Image.Image) -> Image.Image:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    crop.save(INPUT_DIR / "face_refine_src.png", format="PNG")
    wf = {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "flux-2-klein-4b-fp8.safetensors",
            "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen_3_4b.safetensors",
            "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {
            "vae_name": "flux2-vae.safetensors"}},
        "load": {"class_type": "LoadImage", "inputs": {
            "image": "face_refine_src.png"}},
        "encode": {"class_type": "VAEEncode", "inputs": {
            "pixels": ["load", 0], "vae": ["vae", 0]}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": FACE_PROMPT}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": NEG}},
        "guidance": {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["pos", 0], "guidance": 4.0}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {
            "model": ["unet", 0], "scheduler": "simple",
            "steps": 20, "denoise": 0.5}},
        "guider": {"class_type": "CFGGuider", "inputs": {
            "model": ["unet", 0], "positive": ["guidance", 0],
            "negative": ["neg", 0], "cfg": 4.0}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["encode", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0], "filename_prefix": "face_refine"}},
    }
    req = urllib.request.Request(
        BASE + "/prompt", data=json.dumps(
            {"prompt": wf, "client_id": "facepo"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        pid = json.load(r)["prompt_id"]
    print("refine prompt_id:", pid, flush=True)
    while True:
        time.sleep(4)
        with urllib.request.urlopen(BASE + "/history/" + pid, timeout=10) as r:
            hist = json.load(r)
        if pid not in hist:
            continue
        st = hist[pid].get("status", {})
        if st.get("status_str") == "error":
            raise RuntimeError(json.dumps(st.get("messages", []))[-300:])
        for node_out in hist[pid].get("outputs", {}).values():
            for img in node_out.get("images", []):
                if img.get("type") == "output":
                    p = (Path(r"E:\OmniSpace\data\comfyui\output")
                         / img.get("subfolder", "") / img["filename"])
                    return Image.open(p).convert("RGB")


def paste_back(base: Image.Image, face: Image.Image, box) -> Image.Image:
    left, top, right, bottom = box
    face = face.resize((right - left, bottom - top), Image.LANCZOS)
    # 椭圆羽化蒙版（中心实、边缘虚）
    mask = Image.new("L", face.size, 0)
    from PIL import ImageDraw
    d = ImageDraw.Draw(mask)
    w, h = face.size
    d.ellipse((w * 0.08, h * 0.08, w * 0.92, h * 0.92), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(24))
    out = base.convert("RGB").copy()
    out.paste(face, (left, top), mask)
    return out


def main():
    img = Image.open(SRC).convert("RGB")
    bbox = detect_face(img)
    print("face bbox:", bbox, flush=True)
    if bbox is None:
        print("NO FACE")
        return
    crop, box = crop_face_region(img, bbox)
    crop.save(r"E:\OmniSpace\logs\four_models\face_crop.png")
    refined = refine_face(crop)
    refined.save(r"E:\OmniSpace\logs\four_models\face_refined.png")
    out = paste_back(img, refined, box)
    out.save(OUT)
    print("saved:", OUT, flush=True)


if __name__ == "__main__":
    main()
