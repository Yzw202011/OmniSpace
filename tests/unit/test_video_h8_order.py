"""H8 幽灵 pending 真修钉子（2026-10-24 终结书 P0）。

R3-H3 实锤：video_generate 云端首帧校验曾排在 video_tasks 落库之后
（注释自称「确保 raise 在 insert 之前」与代码相反）——云端无首帧拒绝
后 pending 幽灵行永久卡死该行（连点查重只认 pending/generating）。
源码顺序哨兵钉住修复不回退（源码扫描测试先例：test_gpu_budget
TestIdleConsumerWiring）。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2]
        / "src" / "api" / "manga" / "video.py").read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    m = re.search(rf"async def {name}\(.*?(?=\nasync def |\Z)", _SRC, re.S)
    assert m, f"函数不存在: {name}"
    return m.group(0)


def test_h8_cloud_validation_precedes_insert():
    """云端首帧校验 raise 必须前置于 video_tasks 落库。"""
    fn = _fn_body("video_generate")
    raise_pos = fn.find("该分镜行还没有当前关键帧")
    insert_pos = fn.find('db.insert, "video_tasks"')
    assert raise_pos > 0, "云端首帧校验块丢失"
    assert insert_pos > 0, "video_tasks 落库点丢失"
    assert raise_pos < insert_pos, (
        f"H8 回退：校验({raise_pos}) 又排到了落库({insert_pos}) 之后")


def test_h8_validation_precedes_flow_creation():
    """校验须在 flow 建立之前——raise 不泄漏未收口的 flow_trace。"""
    fn = _fn_body("video_generate")
    raise_pos = fn.find("该分镜行还没有当前关键帧")
    flow_pos = fn.find("start_flow(")
    assert flow_pos > 0
    assert raise_pos < flow_pos, "校验后移到了 flow 建立之后（会漏 flow）"


def test_h8_h3_chain_path_already_clean():
    """链式批量路径本就全前置校验（对照组，防误改）。"""
    fn = _fn_body("video_generate_h3_chain")
    last_check = fn.find("VIDEO_TASK_DUPLICATE")
    insert_pos = fn.find('db.insert, "video_tasks"')
    assert 0 < last_check < insert_pos, "链式路径校验顺序被动过"
