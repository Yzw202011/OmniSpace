@echo off
cd /d E:\OmniSpace\tools\ComfyUI_windows_portable
E:\OmniSpace\tools\ComfyUI_windows_portable\python_embeded\python.exe -s ComfyUI/main.py --windows-standalone-build --deterministic --listen 127.0.0.1 --port 8189 --output-directory E:\OmniSpace\data\comfyui\output --input-directory E:\OmniSpace\data\comfyui\input --temp-directory E:\OmniSpace\data\comfyui\temp --user-directory E:\OmniSpace\data\comfyui\user --use-sage-attention
