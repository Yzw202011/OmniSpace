"""对话引擎显存降级哨兵（2026-09-02 用户拍板）。

契约：冷引擎且目标模型显存装不下时，ensure_loaded 自动改用引擎默认
小模型继续生成（不许只报错甩给用户），并广播大白话事件 model_fallback
告知降级（不偷偷降质）；降级来源经 get_status().degraded_from 可查；
重新加载原模型/新加载意图自动清降级旗标；降级模型也装不下才走诚实
报错路径。触发条件收紧：失败原因须含「显存」且目标 ≠ 默认小模型。
"""
from __future__ import annotations

from pathlib import Path

ENGINE_PY = (Path(__file__).resolve().parents[2]
             / "services" / "inference" / "dialog_engine.py")
SRC = ENGINE_PY.read_text(encoding="utf-8")


def test_degrade_branch_exists_in_ensure_loaded() -> None:
    assert "model_fallback" in SRC, "降级大白话事件被删"
    assert "自动降级" in SRC, "降级分支被删"
    assert 'self.load_model(_fallback_id)' in SRC, "降级装载调用被删"
    # 触发条件必须收紧在显存类失败（其他失败不得静默换模型）
    assert '"显存" in _reason' in SRC, "触发条件失去显存判定"


def test_degrade_flag_lifecycle() -> None:
    assert "self._degraded_from: str = \"\"" in SRC, "旗标初始化被删"
    assert SRC.count("self._degraded_from = \"\"") >= 3, (
        "load_model 新意图清旗标被删")
    assert '"degraded_from": self._degraded_from' in SRC, (
        "get_status 不再暴露降级来源")


def test_degrade_registers_ledger() -> None:
    """降级装载成功必须补登记台账（08-23 显存锚定事故根修同源）。"""
    assert "降级加载台账补登记" in SRC
    assert "register_external_load" in SRC
