# 本项目仅供学习使用，商业授权请+Q 3559331368
"""H3 工作流模板防呆闸测试（问题总账 #30 / 清偿计划 Q6，2026-09-19）。

_render_graph 按魔法节点编号注入参数（1/2/3/4/110/1700/1701/1706/1961）；
模板 JSON 被手改致编号漂移时，旧实现会静默跳过注入→错片。本闸：
模板完整→正常渲染；缺任一必要节点→fail-closed 拒绝并给出还原指引。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.middleware.error_handler import ApiError
from src.services.inference import h3_chain_engine as eng

_TEMPLATE = Path(eng.__file__).parent / "h3_chain_workflow_api.json"


def _template() -> dict:
    return json.loads(_TEMPLATE.read_text(encoding="utf-8"))


def test_on_disk_template_has_all_required_nodes() -> None:
    """仓库内模板必须包含全部必要节点（防模板自身漂移的哨兵）。"""
    wf = _template()
    missing = eng._REQUIRED_NODES - wf.keys()
    assert not missing, f"模板缺必要节点: {sorted(missing)}"


def test_render_graph_rejects_template_missing_node() -> None:
    """模板缺任一必要节点 → fail-closed（H3_WORKFLOW_TEMPLATE_BROKEN）。"""
    wf = _template()
    del wf["1700"]  # 模拟手改后编号漂移
    with pytest.raises(ApiError) as ei:
        eng._render_graph(wf, plan_json="{}", run_name="t",
                          width=1344, height=768, refs=[], task_id="t")
    assert ei.value.code == "H3_WORKFLOW_TEMPLATE_BROKEN"
    assert "1700" in str(ei.value.message)


def test_render_graph_passes_with_intact_template() -> None:
    """完整模板 → 正常渲染出图（不因防呆闸误伤正常链路）。"""
    graph = eng._render_graph(_template(), plan_json="{}", run_name="t",
                              width=1344, height=768, refs=[], task_id="t")
    assert set(eng._REQUIRED_NODES) <= graph.keys()


def test_two_stage_sweet_spot_wiring() -> None:
    """两阶段甜点接线（2026-10-17 用户拍板唯一产链）：

    720p 非 turbo → 480p 基座采样 + 精修 dn0.45 + 尾段+3 + 音频锁回，
    精修吃尾段调度；直出链（two_stage=False）不得出现精修节点。
    """
    graph = eng._render_graph(_template(), plan_json="{}", run_name="t",
                              width=1344, height=768, refs=[], task_id="t",
                              turbo=False, two_stage=True)
    # 基座降 480p 采样（循环计划宽高走 864×480）
    assert graph["1700"]["inputs"]["width"] == 864
    assert graph["1700"]["inputs"]["height"] == 480
    # 精修 sigmas = 甜点 denoise 0.45 → 尾段+3 → RefineHandoff
    assert graph["V1sigmas"]["inputs"]["denoise"] == 0.45
    tail = graph["V1tail2"]
    assert tail["class_type"] == "MiniMaxH3AVTailDetailScheduleT8Advanced"
    assert tail["inputs"]["extra_tail_steps"] == 3
    assert tail["inputs"]["sigmas"] == ["V1sigmas", 0]
    assert tail["inputs"]["profile"] == "custom_strict"
    refine = graph["V1refine"]
    assert refine["inputs"]["sigmas"] == ["V1tail2", 0]
    assert refine["inputs"]["latent"] == ["124", 0]
    assert refine["inputs"]["lock_audio"] is True
    assert refine["inputs"]["width"] == 1344
    assert refine["inputs"]["height"] == 768
    # 解码改吃精修输出
    assert graph["130"]["inputs"]["samples"] == ["V1refine", 0]
    assert graph["131"]["inputs"]["samples"] == ["V1refine", 0]


def test_direct_chain_has_no_refine_nodes() -> None:
    """直出链（two_stage=False，如 480p 档）不得混入两阶段节点。"""
    graph = eng._render_graph(_template(), plan_json="{}", run_name="t",
                              width=864, height=480, refs=[], task_id="t",
                              turbo=False, two_stage=False)
    assert "V1refine" not in graph
    assert "V1tail2" not in graph
    assert "V1sigmas" not in graph
