#!/usr/bin/env python3
"""补齐 flux2-klein-9b 的 transformer 权重分片（2026-08-31，ModelScope 版）。

背景：models/paint/flux2-klein-9b 的 text_encoder(Qwen3-8B)/vae/tokenizer
齐全，仅 transformer 两个 safetensors 分片在此前磁盘清理中被删（剩
config.json + index.json 空壳），导致漫剧分镜生图每次都降级 SDXL+翻译。

来源：ModelScope 官方同步仓 black-forest-labs/FLUX.2-Klein-9B（HF 原仓
为门禁仓且本机直连超时，镜像亦无令牌）。

实现：文件下载与落盘全部由已安装的 modelscope SDK（1.39.1）完成，
本脚本只声明仓库/目录/目标位置，不构造任何动态路径。SDK 自带
断点续传与完整性校验；失败自动重试 5 次。

用法：python tools/download_klein9b_transformer.py
"""
from __future__ import annotations

import sys
import time

REPO = 'black-forest-labs/FLUX.2-Klein-9B'
LOCAL_DIR = r'E:\OmniSpace\models\paint\flux2-klein-9b'
ALLOW_PATTERNS = ['transformer/*']
MAX_ATTEMPTS = 5


def main() -> int:
    from modelscope.hub.snapshot_download import snapshot_download

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f'[attempt {attempt}] snapshot_download {REPO} '
                  f'patterns={ALLOW_PATTERNS}', flush=True)
            path = snapshot_download(
                REPO,
                allow_patterns=ALLOW_PATTERNS,
                local_dir=LOCAL_DIR,
            )
            print(f'DONE -> {path}', flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            print(f'[attempt {attempt}] FAIL {type(exc).__name__}: '
                  f'{str(exc)[:300]}', flush=True)
            if attempt == MAX_ATTEMPTS:
                return 1
            time.sleep(10 * attempt)
    return 1


if __name__ == '__main__':
    sys.exit(main())
