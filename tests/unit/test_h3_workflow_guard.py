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
