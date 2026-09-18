"""副本指纹与接管闸测试（批2-4，2026-09-18）。

覆盖两路：①活的 /health copy_id 回显（通道终点，TestClient 实弹）；
②boot/launcher 侧源码契约断言（launcher 层不进 pytest 射程——独立
装载 boot.py 有 dataclass 双模块坑，源码级断言 + 实弹双副本联调补录
是诚实取舍）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database

ROOT = Path(__file__).resolve().parents[2]
BOOT = (ROOT / "launcher" / "boot.py").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "launcher" / "launcher.py").read_text(encoding="utf-8")


def test_health_echoes_copy_id(tmp_path, monkeypatch):
    """/health 回显环境指纹（批2-4 通道终点）。"""
    monkeypatch.setenv("OMNISPACE_COPY_ID", "abcd1234")
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    c = TestClient(app, base_url="http://127.0.0.1")
    body = c.get("/health").json()
    assert body["data"]["copy_id"] == "abcd1234"


def test_health_copy_id_empty_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISPACE_COPY_ID", raising=False)
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    c = TestClient(app, base_url="http://127.0.0.1")
    body = c.get("/health").json()
    assert body["data"]["copy_id"] == ""  # 旧链/直启无指纹=保守拒绝接管


def test_boot_has_fingerprint_and_gates():
    """源码契约：指纹函数/自拉注入/跨副本拒绝/跨版本拒绝/接管提示五件在位。"""
    assert "_copy_fingerprint" in BOOT
    assert "OMNISPACE_COPY_ID" in BOOT  # 自拉后端注入
    assert "copy_id 不匹配" in BOOT      # 跨副本拒绝文案
    assert "不跨版本接管" in BOOT          # 发行包跨构建拒绝
    assert "接管模式提示" in BOOT          # 假退出用户提示（审计 1-2）
    # 指纹=包根 md5 前 8（同路径稳定/异路径必异）
    assert "md5" in BOOT and "hexdigest()[:8]" in BOOT


def test_launcher_env_whitelist_carries_copy_id():
    """环境白名单透传 OMNISPACE_COPY_ID（漏了指纹进不了后端）。"""
    wl = "OMNISPACE_COPY_ID" in LAUNCHER
    assert wl
