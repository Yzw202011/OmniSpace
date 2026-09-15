"""tsr 包独立验证：加载 TripoSR 权重 -> 合成图 -> 网格。"""
import os
import sys
import time

os.environ["HF_HUB_OFFLINE"] = "1"
sys.path.insert(0, r"e:\OmniSpace\pydeps\triposr_src")

import torch

# 引擎同款 DINO 离线补丁
from backend.services.inference.triposr_engine import (
    _patch_dino_offline_fallback,
    _write_dino_config_cache,
)
from PIL import Image, ImageDraw

_write_dino_config_cache()
_patch_dino_offline_fallback()

from tsr.system import TSR  # noqa: E402 - 前置 DINO 离线补丁必须先于导入

t0 = time.time()
model = TSR.from_pretrained(
    r"e:\OmniSpace\models\3d\TripoSR",
    config_name="config.yaml", weight_name="model.ckpt")
print(f"[load] from_pretrained {time.time()-t0:.1f}s")

device = "cuda" if torch.cuda.is_available() else "cpu"
model.to(device)
if device == "cuda":
    model = model.half()
model.renderer.set_chunk_size(65536)
print(f"[load] device={device}")

# 合成测试图：灰底中央彩色球体（近似官方预处理后的输入分布）
img = Image.new("RGB", (512, 512), (128, 128, 128))
d = ImageDraw.Draw(img)
d.ellipse((156, 106, 356, 306), fill=(200, 60, 40))
d.ellipse((190, 140, 240, 190), fill=(240, 150, 130))

t0 = time.time()
scene_codes = model([img], device=device)
print(f"[forward] scene_codes {tuple(scene_codes.shape)} "
      f"{time.time()-t0:.1f}s dtype={scene_codes.dtype}")

t0 = time.time()
meshes = model.extract_mesh(scene_codes, has_vertex_color=True,
                            resolution=128)
mesh = meshes[0]
print(f"[extract_mesh] {time.time()-t0:.1f}s verts={len(mesh.vertices)} "
      f"faces={len(mesh.faces)} colors={mesh.visual.kind}")
assert len(mesh.vertices) > 100, "顶点数过少，密度场提取异常"
out = r"e:\OmniSpace\data\generated\3d\_tsr_selftest.glb"
os.makedirs(os.path.dirname(out), exist_ok=True)
mesh.export(out)
print(f"[export] {out} {os.path.getsize(out)//1024}KB")
print("SELFTEST OK")
