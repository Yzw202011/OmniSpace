"""FaceRefine 挂死根修 E2E：中途杀 ComfyUI → 验证快速检出+自愈重试。

流程：启动 FaceRefine → KILL_AFTER_S 后 taskkill ComfyUI 整树 → 观察。
通过判据：
  HEALED   = 检出任务丢失→自动重拉 ComfyUI→重试出片（返回 _fr 路径）
  FALLBACK = 重试仍败但限时内干净回退原片（降级铁律保持）
  HANG     = 卡死不动（旧病复发，修复无效）
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

ROW_ID = "0f67d2d85e024ea2aeff2bdbaafc8427"
SRC = Path(r"E:\OmniSpace\data\videos\0f67d2d85e024ea2\h3v_2s_d60.mp4")
KILL_AFTER_S = 100.0  # 提交后 100s 杀 ComfyUI（FaceRefine 链全程 ~450s）


def kill_comfy(cm) -> None:
    time.sleep(KILL_AFTER_S)
    pid = cm.pid()
    if pid is None:
        print("[kill] 无 pid 可杀（ComfyUI 已不在管）", flush=True)
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, check=False)
    print(f"[kill] 已 taskkill ComfyUI pid={pid} @ +{KILL_AFTER_S:.0f}s",
          flush=True)


def main() -> int:
    import src.services.inference.h3_chain_engine as hce
    from src.data.database import get_db_safe
    from src.services.inference.h3_face_refine import _comfy_manager, refine_video

    cm = _comfy_manager()
    assert cm is not None
    cm.spawn("comfyui_h3.log")
    print(f"[setup] ComfyUI pid={cm.pid()}", flush=True)

    # 等 ComfyUI 就绪（冷启动 20-60s；否则 facerefine_ready 探活失败即回退）
    import urllib.request
    for i in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:8189/object_info",
                                   timeout=5).read()
            print(f"[setup] ComfyUI 就绪（{i * 2}s）", flush=True)
            break
        except Exception:
            time.sleep(2)
    else:
        print("VERDICT: SETUP-FAIL（ComfyUI 120s 未就绪）")
        return 1

    db = get_db_safe()
    r = db.query_one(
        "SELECT description, characters, scene, props, asset_ids "
        "FROM storyboard_rows WHERE id=?", (ROW_ID,))
    r = {**r, "id": ROW_ID}
    refs = hce._collect_refs(r, ROW_ID)
    refs = hce._prepare_refs("frheal", refs)
    prompt = hce._six_section(r["description"], refs, 10.0)
    ref_pils = []
    for ref in refs:
        if ref.get("kind") != "character":
            continue
        fp = Path(hce._COMFY_INPUT) / str(ref.get("image") or "")
        if fp.is_file():
            from PIL import Image as _Img
            ref_pils.append(_Img.open(fp).convert("RGB"))
        if len(ref_pils) >= 2:
            break
    print(f"[setup] ref_pils={len(ref_pils)}", flush=True)

    result: dict = {}

    def run():
        t0 = time.time()
        out = refine_video(SRC, prompt=prompt, ref_images=ref_pils)
        result["out"] = out
        result["secs"] = time.time() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    killer = threading.Thread(target=kill_comfy, args=(cm,), daemon=True)
    killer.start()

    deadline = time.time() + 30 * 60
    th.join(timeout=max(1.0, deadline - time.time()))
    out = result.get("out")
    secs = result.get("secs")
    if out is None:
        print("VERDICT: HANG（线程 30min 未返回，修复无效）")
        return 1
    print(f"[result] secs={secs:.0f} out={out}", flush=True)
    healed = str(out).endswith("_fr.mp4")
    print(f"VERDICT: {'HEALED（自愈重试出片）' if healed else 'FALLBACK（干净回退）'}"
          f" in {secs:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
