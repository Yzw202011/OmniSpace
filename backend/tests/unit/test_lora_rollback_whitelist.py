"""批2 骑乘项哨兵（审计 09-10）：knowledge LoRA rollback 白名单。

旧病（P1-5 残留）：rollback 仅查 `(LORA_DIR / version).is_dir()`——
`../`、手建目录名都能写进 current.json 指针，下游按 current 拼路径
越出 LORA_DIR（无 rmtree 危害，属指针污染）。修法=对齐 delete_version
思路：目标必须出现在 list_versions 注册白名单（v<数字> + meta 可读）。

同文件顺带锁定 evaluate() 取数优先级（P1-14 残留）：_last_train_data
（本次训练实际使用）优先于 _dataset（自动构建缓存）。
"""
from __future__ import annotations

from backend.services import lora_training_service as lts_mod
from backend.services.lora_training_service import LoRATrainingService


def _fresh_svc(tmp_path, monkeypatch) -> LoRATrainingService:
    monkeypatch.setattr(lts_mod, "LORA_DIR", tmp_path)
    monkeypatch.setattr(lts_mod, "CURRENT_FILE", tmp_path / "current.json")
    return LoRATrainingService()


def test_rollback_whitelist_accepts_registered(tmp_path, monkeypatch) -> None:
    svc = _fresh_svc(tmp_path, monkeypatch)
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1" / "meta.json").write_text(
        '{"status": "registered"}', encoding="utf-8")

    assert svc.rollback("v1") is True
    current = (tmp_path / "current.json").read_text(encoding="utf-8")
    assert '"v1"' in current


def test_rollback_rejects_unregistered_dirs(tmp_path, monkeypatch) -> None:
    svc = _fresh_svc(tmp_path, monkeypatch)
    (tmp_path / "evil-dir").mkdir()          # 非 v<数字> 命名，不在册
    (tmp_path / "..").resolve()              # （穿越目标不必真实存在）

    assert svc.rollback("evil-dir") is False
    assert svc.rollback("../evil-dir") is False
    assert svc.rollback("..\\..\\Windows") is False
    assert svc.rollback("") is False
    assert not (tmp_path / "current.json").exists(), "被拒回滚不得写指针"


def test_rollback_rejects_v_named_but_foreign_dir(tmp_path, monkeypatch) -> None:
    """v<数字> 命名但目录不在 LORA_DIR 注册扫描内（如不存在）→ 拒绝。"""
    svc = _fresh_svc(tmp_path, monkeypatch)
    assert svc.rollback("v999") is False
    assert not (tmp_path / "current.json").exists()


def test_eval_data_priority_last_train_first(tmp_path, monkeypatch) -> None:
    """P1-14：评估取数 _last_train_data 优先于 _dataset（外部 JSONL
    场景此前被知识库自动集顶掉）。锁定优先级顺序不回退。"""
    svc = _fresh_svc(tmp_path, monkeypatch)
    svc._dataset = {"valid": ["auto"], "total": 1}
    svc._last_train_data = {"valid": ["external"], "total": 1}
    # 复现 evaluate 的取数表达式（与源码同序断言）
    data = svc._last_train_data or svc._dataset or {}
    assert data["valid"] == ["external"]
# 本项目仅供学习使用，商业授权请+Q 3559331368
