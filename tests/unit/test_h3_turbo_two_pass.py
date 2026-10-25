"""双采加速产线（turbo 粗采 + ×2 Latent 放大 + 0.7 低强度精修）单测。

2026-10-24 用户拍板：960p/1440p 新档 = 半分辨率 turbo 粗采（4/6/8 步）
→ LatentUpscaler3D ×2 → 低σ精修模型换 0.7 低强度 LoRA（HF 讨论#21
@ANe5s 配方）。回归锁定：现行 720p 非 turbo 两阶段路径零变化。
"""
from __future__ import annotations

import json

from src.services.inference.h3_chain_engine import (
    _QUALITY,
    _TURBO2P_BASE,
    _load_template_text,
    _render_graph,
)

_TEMPLATE = json.loads(_load_template_text())
_REFS = [{"kind": "character", "name": "少女", "image": "h3chain_t/p1.png"}]


def _render(**kw) -> dict:
    return _render_graph(json.loads(json.dumps(_TEMPLATE)), "{}", "run",
                         1728, 960, _REFS, "task1", **kw)


def test_quality_tiers_registered() -> None:
    assert _QUALITY["960p"] == (1728, 960)
    assert _QUALITY["1440p"] == (2688, 1536)
    assert _TURBO2P_BASE == {"960p": (864, 480), "1440p": (1344, 768)}


def test_two_pass_graph_structure() -> None:
    graph = _render(two_pass=True, base_dims=(864, 480))
    # 粗采保持 turbo：1961@1.0 + 1962 采样器
    assert graph["1961"]["inputs"]["strength"] == 1.0
    assert graph["124"]["inputs"]["sampler"] == ["1962", 0]
    # 精修 2 步 @ denoise 0.24（大神 Deferred Upscale 原方），尾段旁路
    assert graph["V1sigmas"]["inputs"]["steps"] == 2
    assert graph["V1sigmas"]["inputs"]["denoise"] == 0.24
    assert graph["V1refine"]["inputs"]["sigmas"] == ["V1sigmas", 0]
    # ×2 放大后放大器权重即卸（16G 卡 960p 精修窗口崩机实测）
    assert graph["V1refine"]["inputs"]["offload_after_upscale"] is True
    # 精修模型 = 全强度 turbo 模型（首跑实测：turbo 节点 bypass 不支持
    # 自身叠加，1963@0.7 二次挂载 NoneType 崩——不得再加第二颗）
    assert graph["V1refine"]["inputs"]["model"] == ["5", 0]
    assert "1963" not in graph
    # 放大目标 = 新档位全分辨率，粗采画布 = 半分辨率
    assert graph["V1refine"]["inputs"]["width"] == 1728
    assert graph["V1refine"]["inputs"]["height"] == 960
    assert graph["1700"]["inputs"]["width"] == 864
    assert graph["1700"]["inputs"]["height"] == 480


def test_two_pass_1440p_base_canvas() -> None:
    graph = _render_graph(json.loads(json.dumps(_TEMPLATE)), "{}", "run",
                          2688, 1536, _REFS, "task2",
                          two_pass=True, base_dims=(1344, 768))
    assert graph["1700"]["inputs"]["width"] == 1344
    assert graph["1700"]["inputs"]["height"] == 768
    assert graph["V1refine"]["inputs"]["width"] == 2688
    assert graph["V1refine"]["inputs"]["height"] == 1536
    # ×2 档精修窗口须卸放大器权重（16G 两次崩机实锤——大显存机专用）
    assert graph["V1refine"]["inputs"]["offload_after_upscale"] is True


def test_two_pass_portrait_swaps_base() -> None:
    graph = _render_graph(json.loads(json.dumps(_TEMPLATE)), "{}", "run",
                          960, 1728, _REFS, "task3",
                          two_pass=True, base_dims=(480, 864))
    assert graph["1700"]["inputs"]["width"] == 480
    assert graph["1700"]["inputs"]["height"] == 864
    assert graph["V1refine"]["inputs"]["width"] == 960
    assert graph["V1refine"]["inputs"]["height"] == 1728


def test_turbo_720p_two_stage_master_recipe() -> None:
    """turbo+720p 双采版（同图换配方）：粗采 turbo 节点保留、精修 2 步
    dn0.24 大神原方、尾段旁路、粗采画布仍 480p（VRAM 包络不变）。"""
    graph = _render_graph(json.loads(json.dumps(_TEMPLATE)), "{}", "run",
                          1344, 768, _REFS, "t_turbo2s",
                          turbo=True, two_stage=True)
    assert graph["1961"]["inputs"]["strength"] == 1.0
    assert graph["124"]["inputs"]["sampler"] == ["1962", 0]
    assert graph["V1sigmas"]["inputs"]["steps"] == 2
    assert graph["V1sigmas"]["inputs"]["denoise"] == 0.24
    assert graph["V1refine"]["inputs"]["sigmas"] == ["V1sigmas", 0]
    assert graph["V1refine"]["inputs"]["model"] == ["5", 0]
    assert graph["1700"]["inputs"]["width"] == 864
    assert graph["1700"]["inputs"]["height"] == 480
    assert graph["V1refine"]["inputs"]["width"] == 1344


def test_regression_nonturbo_two_stage_unchanged() -> None:
    """现行 720p 非 turbo 两阶段：13 步 dn0.45 + 尾段配方零变化。"""
    graph = _render_graph(json.loads(json.dumps(_TEMPLATE)), "{}", "run",
                          1344, 768, _REFS, "t_regress",
                          turbo=False, two_stage=True)
    assert "1961" not in graph and "1962" not in graph
    assert graph["V1sigmas"]["inputs"]["steps"] == 10
    assert graph["V1sigmas"]["inputs"]["denoise"] == 0.45
    assert graph["V1refine"]["inputs"]["sigmas"] == ["V1tail2", 0]
    assert graph["V1refine"]["inputs"]["model"] == ["5", 0]
    assert graph["124"]["inputs"]["sampler"] == ["122", 0]


def test_plain_turbo_untouched() -> None:
    """纯 turbo 单采（480p 档）：无精修节点组、无 1963。"""
    graph = _render(turbo=True)
    assert "V1refine" not in graph and "1963" not in graph
