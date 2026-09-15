# -*- coding: utf-8 -*-
"""修 test_comfy_paint_zimage 的 mypy arg-type（构造真实例/真 Image）。"""
from pathlib import Path

p = Path("E:/OmniSpace/backend/tests/unit/test_comfy_paint_zimage.py")
s = p.read_text(encoding="utf-8")

s = s.replace(
    '''def _z_wf(params: dict, refs: list[str] | None = None) -> dict:
    # 纯函数构建（不触 self 状态），unbound 直调即可
    return cpe.ComfyPaintEngine._build_workflow_z_image(
        None, params, refs, "paint/t")''',
    '''def _eng() -> cpe.ComfyPaintEngine:
    # 纯函数构建不触 self 状态；__new__ 绕过重型初始化
    return cpe.ComfyPaintEngine.__new__(cpe.ComfyPaintEngine)


def _z_wf(params: dict, refs: list[str] | None = None) -> dict:
    return cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), params, refs, "paint/t")''')

s = s.replace(
    '''def test_z_mode_rejects_pulid() -> None:
    eng = cpe.ComfyPaintEngine.__new__(cpe.ComfyPaintEngine)
    with pytest.raises(cpe.ApiError) as ei:
        cpe.ComfyPaintEngine._run_locked(
            eng, {"model": "z-image-turbo", "prompt": "x",
                  "pulid_strength": 1.0},
            None, pulid_image=object())''',
    '''def test_z_mode_rejects_pulid() -> None:
    eng = _eng()
    face = Image.new("RGB", (4, 4))
    with pytest.raises(cpe.ApiError) as ei:
        cpe.ComfyPaintEngine._run_locked(
            eng, {"model": "z-image-turbo", "prompt": "x",
                  "pulid_strength": 1.0},
            None, pulid_image=face)''')

# Image 导入
if "from PIL import Image" not in s:
    s = s.replace("import pytest\n", "import pytest\n\nfrom PIL import Image\n", 1)

s = s.replace(
    '''def test_klein_default_workflow_unaffected() -> None:
    eng = cpe.ComfyPaintEngine.__new__(cpe.ComfyPaintEngine)
    wf = cpe.ComfyPaintEngine._build_workflow(
        eng,''',
    '''def test_klein_default_workflow_unaffected() -> None:
    eng = _eng()
    wf = cpe.ComfyPaintEngine._build_workflow(
        eng,''')

p.write_text(s, encoding="utf-8", newline="")
import py_compile
py_compile.compile(str(p), doraise=True)
print("test fixed")
