"""P-12 防复发（2026-09-20 用户拍板 A）单测：PuLID EVA-CLIP 缓存自愈。

锁定 ensure_pulid_eva_cache 六路径：命中零动作 / 缺失回灌（refs/main
+sha256 校验）/ 缓存损坏回灌 / 双缺诚实 False / 播种备份 / 备份损坏
拒绝回灌。常量经 monkeypatch 缩到字节级（真件 816MB 不进测试）。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.services.inference import comfy_proc


@pytest.fixture()
def eva(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """缩水版常量 + 注入式路径：cache/backup 各自独立 tmp 目录。

    repo/sha 一并缩名——pytest 真实 temp 基路径下全真名会超 Windows
    MAX_PATH 260（真机 HF_HOME 路径短，无此问题）。
    """
    payload = b"EVA-CLIP-FAKE-WEIGHTS"  # 20 字节
    monkeypatch.setattr(comfy_proc, "_PULID_EVA_BYTES", len(payload))
    monkeypatch.setattr(comfy_proc, "_PULID_EVA_SHA256",
                        hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(comfy_proc, "_PULID_EVA_REPO_DIR", "models--fake")
    monkeypatch.setattr(comfy_proc, "_PULID_EVA_SHA", "cafe5ha")
    cache = tmp_path / "hub" / "models--fake" / "snapshots" \
        / "cafe5ha" / comfy_proc._PULID_EVA_FILE
    backup = tmp_path / "models" / "embed" / "pulid_eva_clip" \
        / comfy_proc._PULID_EVA_FILE
    return {"cache": cache, "backup": backup, "payload": payload}


def test_cache_hit_zero_action(eva) -> None:
    """缓存健康 → True；备份已存在时不重复播种（内容不被覆盖）。"""
    e = eva
    e["cache"].parent.mkdir(parents=True)
    e["cache"].write_bytes(e["payload"])
    e["backup"].parent.mkdir(parents=True)
    e["backup"].write_bytes(b"KEEP-ME")
    assert comfy_proc.ensure_pulid_eva_cache(
        cache_file=e["cache"], backup_file=e["backup"]) is True
    assert e["backup"].read_bytes() == b"KEEP-ME"


def test_cache_hit_seeds_backup(eva) -> None:
    """缓存健康 + 备份缺失 → 反向播种（自舉，此后可自愈）。"""
    e = eva
    e["cache"].parent.mkdir(parents=True)
    e["cache"].write_bytes(e["payload"])
    assert comfy_proc.ensure_pulid_eva_cache(
        cache_file=e["cache"], backup_file=e["backup"]) is True
    assert e["backup"].read_bytes() == e["payload"]


def test_missing_cache_healed_from_backup(eva) -> None:
    """缓存缺失 + 备份健康 → 回灌快照文件 + refs/main 写 commit sha。"""
    e = eva
    e["backup"].parent.mkdir(parents=True)
    e["backup"].write_bytes(e["payload"])
    assert comfy_proc.ensure_pulid_eva_cache(
        cache_file=e["cache"], backup_file=e["backup"]) is True
    assert e["cache"].read_bytes() == e["payload"]
    refs_main = e["cache"].parents[2] / "refs" / "main"
    assert refs_main.read_text() == "cafe5ha"
    assert not e["cache"].with_name(
        e["cache"].name + ".healing").exists()  # 原子替换无残件


def test_corrupt_cache_healed(eva) -> None:
    """缓存存在但大小不符（半截文件）→ 视同缺失，回灌覆盖。"""
    e = eva
    e["cache"].parent.mkdir(parents=True)
    e["cache"].write_bytes(b"truncated")
    e["backup"].parent.mkdir(parents=True)
    e["backup"].write_bytes(e["payload"])
    assert comfy_proc.ensure_pulid_eva_cache(
        cache_file=e["cache"], backup_file=e["backup"]) is True
    assert e["cache"].stat().st_size == len(e["payload"])


def test_both_missing_honest_false(eva) -> None:
    """双缺 → 诚实 False（不抛异常不造空文件）。"""
    e = eva
    assert comfy_proc.ensure_pulid_eva_cache(
        cache_file=e["cache"], backup_file=e["backup"]) is False
    assert not e["cache"].exists()


def test_corrupt_backup_refused(eva) -> None:
    """备份大小符但 sha256 不符 → 拒绝回灌（防把坏件写进缓存）。"""
    e = eva
    e["backup"].parent.mkdir(parents=True)
    e["backup"].write_bytes(b"x" * len(e["payload"]))  # 大小对内容错
    assert comfy_proc.ensure_pulid_eva_cache(
        cache_file=e["cache"], backup_file=e["backup"]) is False
    assert not e["cache"].exists()
