"""帧规整递归展平离线单测（模拟 VACE [B][F][H][W][C] uint8 输出）。"""
import sys

sys.path.insert(0, r'e:\OmniSpace')
import numpy as np
from PIL import Image
from PIL import Image as _FrameImage


def _to_pil_frames(node, out):
    if hasattr(node, "save"):
        out.append(node)
    elif isinstance(node, np.ndarray):
        if node.ndim == 3 and node.shape[-1] in (1, 3):
            if node.dtype != np.uint8:
                node = (np.clip(node, 0.0, 1.0) * 255).astype(np.uint8)
            if node.shape[-1] == 1:
                node = np.repeat(node, 3, axis=2)
            out.append(_FrameImage.fromarray(node))
        elif node.ndim == 2:
            if node.dtype != np.uint8:
                node = (np.clip(node, 0.0, 1.0) * 255).astype(np.uint8)
            out.append(_FrameImage.fromarray(node).convert("RGB"))
        elif node.size:
            for sub in node:
                _to_pil_frames(sub, out)
    elif isinstance(node, (list, tuple)):
        for sub in node:
            _to_pil_frames(sub, out)


cases = {
    "BFWC uint8": np.zeros((1, 4, 8, 8, 3), dtype=np.uint8),
    "BFWC float32": np.random.rand(1, 3, 8, 8, 3).astype(np.float32),
    "FWC uint8": np.zeros((4, 8, 8, 3), dtype=np.uint8),
    "FWC float32": np.random.rand(3, 8, 8, 3).astype(np.float32),
    "list of PIL": [Image.new("RGB", (8, 8)) for _ in range(3)],
    "batch list ndarray": [np.zeros((3, 8, 8, 3), dtype=np.uint8)],
    "gray HW": np.zeros((8, 8), dtype=np.uint8),
}
for name, data in cases.items():
    out = []
    _to_pil_frames(data, out)
    ok = all(isinstance(f, Image.Image) for f in out) and len(out) > 0
    print(f"{name}: {len(out)} frames, all PIL={ok}")
    assert ok
print("ALL PASS")
