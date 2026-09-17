"""插件安检三补丁回归测试（2026-09-17 B1）。

①npz 解压炸弹闸：loader 与内核 pkg 双路，未压缩尺寸超限在物化前拒绝；
②ast.parse 语法错误收口：坏语法源码 → PLUGIN_SOURCE_INVALID 语义错误
（此前直穿 500）。
"""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.plugins import _scan_source_violations  # noqa: E402
from src.middleware.error_handler import ApiError  # noqa: E402
from src.services.plugin_runtime import loader  # noqa: E402
from src.services.plugin_runtime.loader import PluginLoadError  # noqa: E402


def _pkg_with_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add("manifest.json", b'{"name": "bomb-test"}')
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        add("weights/weights.npz", buf.getvalue())


def test_osp_loader_rejects_decompression_bomb(tmp_path: Path) -> None:
    # 全零数组压缩比 ~1000:1：500MB 未压缩 → 压缩后仅 ~500KB（绕过
    # 64MB 压缩上限），必须在物化前被未压缩尺寸闸拒绝
    bomb = {"w": np.zeros(500 * 1024 * 1024 // 8, dtype=np.float64)}
    pkg_path = tmp_path / "bomb.CuteMamen"
    _pkg_with_npz(pkg_path, bomb)

    with pytest.raises(PluginLoadError, match="解压后超限"):
        loader.read_cutemamen_pkg(pkg_path)


def test_osp_loader_passes_normal_weights(tmp_path: Path) -> None:
    ok_arrays = {"w": np.ones(1024, dtype=np.float32)}
    pkg_path = tmp_path / "ok.CuteMamen"
    _pkg_with_npz(pkg_path, ok_arrays)

    pkg = loader.read_cutemamen_pkg(pkg_path)
    assert "w" in pkg.weights


def test_kernel_pkg_rejects_decompression_bomb(tmp_path: Path) -> None:
    from src.cutemamen import pkg as ck_pkg
    bomb = {"w": np.zeros(500 * 1024 * 1024 // 8, dtype=np.float64)}
    pkg_path = tmp_path / "bomb2.CuteMamen"
    _pkg_with_npz(pkg_path, bomb)

    with pytest.raises(ValueError, match="解压后超限"):
        ck_pkg.load_pkg(str(pkg_path))


def test_syntax_error_returns_semantic_code() -> None:
    with pytest.raises(ApiError) as ei:
        _scan_source_violations("def broken(:\n    pass\n")
    assert ei.value.code == "PLUGIN_SOURCE_INVALID"
    assert "语法错误" in ei.value.message
