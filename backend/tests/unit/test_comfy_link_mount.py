"""comfy_mount 挂接器单测（2026-09-02 体验流：拖入→启动→首启激活→即用）。

覆盖幂等判据（同 inode）、孤儿清理（源删门牌留）、外来文件冲突保守
跳过、跨卷检测（winerror 17 模拟）、缺源计数与拖入根识别。全部在
tmp_path 内做真实硬链接（NTFS 同卷），不触真实引擎目录。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MOD_PATH = ROOT / "scripts" / "comfy_link" / "comfy_mount.py"


@pytest.fixture(scope="module")
def mount():
    spec = importlib.util.spec_from_file_location("_cm_under_test", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先进 sys.modules 再 exec：dataclass 解析字符串注解时按模块名
    # 取 sys.modules[name].__dict__，未注册会 AttributeError
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lab(tmp_path, mount):
    """最小沙盘：中央 1 文件 + 映射表 1 条（checkpoints/a.bin ← image_gen/foo/a.bin）。"""
    central = tmp_path / "models"
    central.mkdir()
    comfy = tmp_path / "comfy_models"
    comfy.mkdir()
    (central / "image_gen" / "foo").mkdir(parents=True)
    (central / "image_gen" / "foo" / "a.bin").write_bytes(b"x" * 32)
    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps(
        {"version": 1, "map": {"checkpoints/a.bin": "image_gen/foo/a.bin"}},
        ensure_ascii=False), encoding="utf-8")
    return mount, central, comfy, map_file


def _ensure(lab, **kw):
    mount, central, comfy, map_file = lab
    return mount.ensure_mounted(models_root=central, comfy_models=comfy,
                                map_file=map_file, **kw)


def test_fresh_link_creates_hardlink(lab):
    mount, central, comfy, _ = lab
    report = _ensure(lab)
    dst = comfy / "checkpoints" / "a.bin"
    assert dst.is_file()
    assert report.linked == 1 and report.ok == 0 and report.total == 1
    # 硬链铁证：同一 inode
    assert (os.stat(dst).st_ino
            == os.stat(central / "image_gen" / "foo" / "a.bin").st_ino)


def test_idempotent_same_inode(lab):
    _ensure(lab)
    report = _ensure(lab)   # 二跑：同 inode 幂等跳过
    assert report.linked == 0 and report.ok == 1
    assert not report.details


def test_orphan_cleaned_when_source_deleted(lab):
    mount, central, comfy, _ = lab
    _ensure(lab)
    (central / "image_gen" / "foo" / "a.bin").unlink()
    report = _ensure(lab)
    dst = comfy / "checkpoints" / "a.bin"
    assert not dst.exists()          # 死链门牌已清
    assert report.orphan_cleaned == 1


def test_orphan_with_foreign_partner_not_cleaned(lab):
    """目标仍有其他硬链伙伴（源删后 nlink>=2 不该出现，但防御性验证不误删）。"""
    mount, central, comfy, _ = lab
    _ensure(lab)
    dst = comfy / "checkpoints" / "a.bin"
    # 制造第二个伙伴：对 dst 再挂一条链（模拟用户自行整理出的关联）
    mate = comfy / "checkpoints" / "a_mate.bin"
    os.link(dst, mate)
    (central / "image_gen" / "foo" / "a.bin").unlink()
    report = _ensure(lab)
    assert dst.exists()              # 有伙伴＝外来整理，保守不动
    assert report.orphan_cleaned == 0 and report.conflict == 1
    mate.unlink()


def test_conflict_foreign_file_never_deleted(lab):
    """目标位置被外来真实文件占用：跳过且绝不删用户数据。"""
    mount, central, comfy, _ = lab
    dst = comfy / "checkpoints" / "a.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"foreign-data")
    report = _ensure(lab)
    assert report.conflict == 1 and report.linked == 0
    assert dst.read_bytes() == b"foreign-data"


def test_cross_volume_detected(lab, monkeypatch):
    """跨卷硬链失败（winerror 17）计入 cross_volume 而非 errors。

    注意 OSError 四参构造才携带 winerror 属性（errno 由 Windows 映射）；
    三参构造 winerror=None——与挂接器的真实抛出路径（4 参）对齐。"""
    mount, central, comfy, _ = lab

    def _boom(src, dst):
        raise OSError(0, "跨卷", str(dst), mount.ERROR_NOT_SAME_DEVICE)

    monkeypatch.setattr(mount, "_hardlink", _boom)
    report = _ensure(lab)
    assert report.cross_volume == 1 and report.errors == 0


def test_missing_src_and_dst_counted(lab):
    mount, central, comfy, _ = lab
    (central / "image_gen" / "foo" / "a.bin").unlink()
    report = _ensure(lab)
    assert report.missing_src == 1 and report.linked == 0


def test_unreadable_map_reports_error(lab):
    mount, central, comfy, map_file = lab
    map_file.write_text("not-json{", encoding="utf-8")
    report = _ensure(lab)
    assert report.errors == 1 and report.total == 0


def test_validate_models_root(lab, tmp_path):
    mount, *_ = lab
    with_manifest = tmp_path / "m1"
    with_manifest.mkdir()
    (with_manifest / "models_manifest.json").write_text("{}", encoding="utf-8")
    ok, why = mount.validate_models_root(with_manifest)
    assert ok and "manifest" in why

    with_cat = tmp_path / "m2"
    (with_cat / "image_gen").mkdir(parents=True)
    ok, _ = mount.validate_models_root(with_cat)
    assert ok

    junk = tmp_path / "m3"
    junk.mkdir()
    (junk / "随便.txt").write_text("x", encoding="utf-8")
    ok, _ = mount.validate_models_root(junk)
    assert not ok


# ── 跨卷降级（2b）：yaml 追加 + 小件拷贝 + 无解清单 ────────────────

def _fallback_lab(tmp_path):
    """三类条目沙盘：平同名（yaml）/平改名（拷贝）/深子目录（拷贝）。"""
    central = tmp_path / "ext"          # 假想外部盘的 models 根
    comfy = tmp_path / "comfy_models"
    central.mkdir()
    comfy.mkdir()
    # 平同名：h3 布局（类型目录就在自家树下）
    d1 = central / "video_gen" / "h3" / "diffusion_models"
    d1.mkdir(parents=True)
    (d1 / "minimax_nvfp4.safetensors").write_bytes(b"h3" * 64)
    # 平改名：klein vae（diffusion_pytorch_model → flux2-vae）
    d2 = central / "paint" / "klein9b" / "vae"
    d2.mkdir(parents=True)
    (d2 / "diffusion_pytorch_model.safetensors").write_bytes(b"v" * 80)
    # 深子目录形状：insightface/models/antelopev2
    d3 = central / "face" / "insightface" / "antelopev2"
    d3.mkdir(parents=True)
    (d3 / "1k3d68.onnx").write_bytes(b"o" * 50)
    map_file = tmp_path / "map2.json"
    map_file.write_text(json.dumps({"version": 1, "map": {
        "diffusion_models/minimax_nvfp4.safetensors":
            "video_gen/h3/diffusion_models/minimax_nvfp4.safetensors",
        "vae/flux2-vae.safetensors":
            "paint/klein9b/vae/diffusion_pytorch_model.safetensors",
        "insightface/models/antelopev2/1k3d68.onnx":
            "face/insightface/antelopev2/1k3d68.onnx",
        "loras/huge-renamed.safetensors": "loras/huge_other_name.safetensors",
    }}, ensure_ascii=False), encoding="utf-8")
    return central, comfy, map_file


def test_fallback_yaml_and_copy(tmp_path):
    import importlib.util as ilu
    central, comfy, map_file = _fallback_lab(tmp_path)
    # 超限改名件：上限 100 字节——vae(80)/insightface(50) 可拷，
    # huge(200) 走无解（真实场景=超大文件该移盘）
    (central / "loras").mkdir(exist_ok=True)
    (central / "loras" / "huge_other_name.safetensors").write_bytes(b"L" * 200)
    spec = ilu.spec_from_file_location("_cm_fb", MOD_PATH)
    mod = ilu.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yaml_path = tmp_path / "extra_model_paths.yaml"
    rep = mod.cross_volume_fallback(
        models_root=central, comfy_models=comfy, map_file=map_file,
        yaml_path=yaml_path, copy_limit_bytes=100)
    # 平同名 → yaml（1 条）；改名 vae → 拷贝；insightface → 拷贝；huge → 无解
    assert rep.yaml_entries == 1 and rep.yaml_types == ["diffusion_models"]
    assert sorted(rep.copied) == ["insightface/models/antelopev2/1k3d68.onnx",
                                  "vae/flux2-vae.safetensors"]
    assert rep.unresolvable == ["loras/huge-renamed.safetensors"]
    # 拷贝件落到引擎门牌位（改名件用 comfy 名）
    assert (comfy / "vae" / "flux2-vae.safetensors").read_bytes() == b"v" * 80
    assert (comfy / "insightface" / "models" / "antelopev2"
            / "1k3d68.onnx").exists()
    # yaml 格式对齐 extra_config.py：类型值=换行分隔路径串
    import yaml
    data = yaml.safe_load(yaml_path.read_text("utf-8"))
    assert list(data) == [mod.YAML_SECTION]
    assert (data[mod.YAML_SECTION]["diffusion_models"]
            == str(central / "video_gen" / "h3" / "diffusion_models"))


def test_fallback_multidir_block_scalar(tmp_path):
    """同类型多目录：块标量写法还原为换行串（extra_config split('\\n')）。"""
    import yaml
    central, comfy, map_file = _fallback_lab(tmp_path)
    (central / "paint" / "loras").mkdir(parents=True, exist_ok=True)
    (central / "paint" / "loras" / "consis.safetensors").write_bytes(b"c" * 8)
    map_file.write_text(json.dumps({"version": 1, "map": {
        "diffusion_models/minimax_nvfp4.safetensors":
            "video_gen/h3/diffusion_models/minimax_nvfp4.safetensors",
        "loras/consis.safetensors": "paint/loras/consis.safetensors",
    }}, ensure_ascii=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_cm_fb2", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yaml_path = tmp_path / "extra_model_paths.yaml"
    mod.cross_volume_fallback(models_root=central, comfy_models=comfy,
                               map_file=map_file, yaml_path=yaml_path)
    data = yaml.safe_load(yaml_path.read_text("utf-8"))
    sec = data[mod.YAML_SECTION]
    assert len(sec) == 2
    assert sec["loras"] == str(central / "paint" / "loras")  # 单目录双引号串


def test_fallback_already_mounted_and_cleanup(tmp_path):
    """目标已在位（同卷已挂）→ 跳过；空降级 → 删除陈旧 yaml。"""
    central, comfy, map_file = _fallback_lab(tmp_path)
    spec = importlib.util.spec_from_file_location("_cm_fb3", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    dst = comfy / "diffusion_models" / "minimax_nvfp4.safetensors"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"already")
    yaml_path = tmp_path / "extra_model_paths.yaml"
    yaml_path.write_text("stale: true\n", encoding="utf-8")
    rep = mod.cross_volume_fallback(
        models_root=central, comfy_models=comfy, map_file=map_file,
        yaml_path=yaml_path, copy_limit_bytes=1)
    assert rep.already >= 1
    assert not yaml_path.exists()      # 无 yaml 条目（全被占/无解）→ 清陈旧
