"""HiDream-O1-Image fp8 人像美感 POC（2026-09-25，D 线第二候选）。

8B UiT 像素级统一架构（无 VAE/TE 分件），checkpoint 单文件直载；
MIT 可商用；Artificial Analysis 开源榜曾第一。fp8_scaled 8.07GB。
工作流 = CheckpointLoaderSimple + CLIPLoader(hidream_o1 sentinel) 依
comfy 官方 hidream_o1 示例（详见 comfy examples/hidden 目录——实际
以 object_info 契约为准，本 POC 先探节点）。
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8190"

# 先探节点：CheckpointLoaderSimple 能否识别 checkpoint 内嵌 TE/VAE
oi = json.load(urllib.request.urlopen(BASE + "/object_info", timeout=15))
print("CLIPLoader types 有无 hidream_o1 专用:", "hidream" in json.dumps(
    oi["CLIPLoader"]["input"]["required"].get("type", [])))
# 官方 hidream_o1 走 CheckpointLoaderSimple（checkpoint 含 TE+VAE sentinel）
wf = {
    "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {
        "ckpt_name": "hidream_o1_image_dev_fp8_scaled.safetensors"}},
    "pos": {"class_type": "CLIPTextEncode", "inputs": {
        "clip": ["ckpt", 1], "text": ""}},
    "neg": {"class_type": "CLIPTextEncode", "inputs": {
        "clip": ["ckpt", 1], "text": ""}},
}
req = urllib.request.Request(
    BASE + "/prompt", data=json.dumps({"prompt": wf, "client_id": "hd"}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.load(r)
    print("probe ok?", bool(resp.get("prompt_id")))
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print("probe 400:", body[:400])
