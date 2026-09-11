"""多角色 comfy 参考装配哨兵（2026-09-10 一致性路由扩展 Step A）。

UAT 实测：chars=2 分格被 `len(char_assets)==1` 条件拦进 diffusers 无锁
路线 → 画风漂移+角色不符。修复=路由扩至 chars≤2（comfy 多参考软锁：
双角色立绘锚全量入 ReferenceLatent，face 特写仍排除——R5 单人面部图在
多人镜头诱发复制病理），chars≥3 维持 diffusers。

_comfy_ref_entries 对既有三条路径（PuLID off/pos、无 PuLID 单角色）行为
逐分支等价，新增多角色锚定分支。
"""
from __future__ import annotations

from backend.api.manga.keyframe import (
    _comfy_ref_entries,
    _dual_pulid_faces,
)

A_PORTRAIT = "lin_portrait.png"
A_FACE = "lin_face.png"
B_PORTRAIT = "chen_portrait.png"
B_FACE = "chen_face.png"
SCENE = "corridor.png"

REFS = [
    {"kind": "character", "name": "林小满", "image": A_PORTRAIT},
    {"kind": "face", "name": "", "image": A_FACE},
    {"kind": "character", "name": "陈默", "image": B_PORTRAIT},
    {"kind": "scene", "name": "走廊", "image": SCENE},
]


def test_pulid_off_returns_empty() -> None:
    """PuLID+off：全裸（身份归 attention，R2 裁定）。"""
    refs, mps = _comfy_ref_entries(REFS, pulid_img=True,
                                   reflat_hint="off", multi_anchor=False)
    assert refs == [] and mps == []


def test_pulid_pos_drops_face_keeps_both_chars() -> None:
    """PuLID+pos：face/lora 不入，双角色立绘+场景保留，角色 1.0MP。"""
    refs, mps = _comfy_ref_entries(REFS, pulid_img=True,
                                   reflat_hint="pos", multi_anchor=False)
    assert refs == [A_PORTRAIT, B_PORTRAIT, SCENE]
    assert mps == [1.0, 1.0, None]


def test_no_pulid_single_char_keeps_face() -> None:
    """无 PuLID 单角色：旧行为全量（face 入 latent 作唯一一致性通道）。"""
    refs, _ = _comfy_ref_entries(REFS, pulid_img=False,
                                 reflat_hint="", multi_anchor=False)
    assert refs == [A_PORTRAIT, A_FACE, B_PORTRAIT, SCENE]


def test_no_pulid_multi_anchor_drops_face() -> None:
    """chars=2 多角色锚定：face 不入（多人镜头复制病理），双立绘保留。"""
    refs, mps = _comfy_ref_entries(REFS, pulid_img=False,
                                   reflat_hint="", multi_anchor=True)
    assert refs == [A_PORTRAIT, B_PORTRAIT, SCENE]
    assert mps == [1.0, 1.0, None]


def test_lora_never_enters_latent() -> None:
    refs, _ = _comfy_ref_entries(
        [{"kind": "lora", "name": "x", "image": "l.safetensors"}],
        pulid_img=False, reflat_hint="", multi_anchor=False)
    assert refs == []


# ── 双 PuLID 转正（Step B）：双 face 齐备才上双锁，否则回落软锁 ──────

def test_dual_pulid_faces_both_present() -> None:
    """chars=2 且双 face 齐备 → (faceA, faceB)，序=绑定序（P0-3 对齐）。"""
    assert _dual_pulid_faces(2, [A_FACE, B_FACE]) == (A_FACE, B_FACE)


def test_dual_pulid_faces_missing_face_falls_back() -> None:
    """任一 face 缺失 → None（回落多参考软锁，诚实降级）。"""
    assert _dual_pulid_faces(2, [A_FACE, None]) is None
    assert _dual_pulid_faces(2, [None, B_FACE]) is None
    assert _dual_pulid_faces(2, []) is None


def test_dual_pulid_faces_count_guard() -> None:
    """非 chars=2（单角色/三角色）一律不启用双锁。"""
    assert _dual_pulid_faces(1, [A_FACE]) is None
    assert _dual_pulid_faces(3, [A_FACE, B_FACE, "c_face"]) is None
# 本项目仅供学习使用，商业授权请+Q 3559331368
