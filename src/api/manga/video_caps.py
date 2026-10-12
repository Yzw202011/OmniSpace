"""漫剧视频模型档位真源（2026-10-03 用户令：时长/分辨率分档要具体——
「时长 ↔ 可选分辨率」精确配对表驱动，下拉选项与后端校验同源，禁止
模糊的大小比较散落在生成链里）。

- 前端下拉：模型选谁列谁的档（/manga/models/available 每项附 tiers）；
- 后端校验：显式分辨率必须在该时长档的可选清单里（非法组合带允许
  清单拒绝，不静默钳制）；未指定=该档默认（清单首位）；
- 非档时长（链式引擎内部窗口如 10.0s）：按档位表声明的档边界落档
  （数据驱动，无散落比较式）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import bisect

# 档位表（具体配对；2026-10-12 用户令解锁 5/10/15s 档——720p×10s 经
# 16G 实测通过（峰值 15.6G/43.7min，2026-10-12 探针），720p×15s 撞
# H3 单镜 10.1s 结构上限（VIDEO_RUN_GRID 顶格 243 帧）保持锁定，
# 链式分段另议。720p≤8s 旧口径（2026-08-31）作废）
VIDEO_MODEL_TIERS: dict[str, dict] = {
    "minimax-h3": {
        "label": "minimax-h3",
        "tiers": [
            {"duration": 5, "qualities": ["720p", "480p"]},
            {"duration": 10, "qualities": ["720p", "480p"]},
            {"duration": 15, "qualities": ["480p"]},
        ],
    },
}
DEFAULT_VIDEO_MODEL = "minimax-h3"


def tiers_for(model_id: str) -> list[dict]:
    """模型的档位表；空/未知=默认模型档位，云端端点（cloud::）=空表
    （云端视频模型档位未知，前端回退静态兜底清单）。"""
    key = (model_id or "").strip()
    if key.startswith("cloud::"):
        return []
    entry = VIDEO_MODEL_TIERS.get(key) \
        or VIDEO_MODEL_TIERS[DEFAULT_VIDEO_MODEL]
    return entry["tiers"]


def _tier_for(tiers: list[dict], seconds: float) -> dict:
    """时长落档：精确档优先，非档时长按声明档边界落「不小于它的最近
    档」（低于首档=首档，高于末档=末档——调用方已钳 3~15s）。"""
    durations = [int(t["duration"]) for t in tiers]
    idx = durations.index(int(seconds)) if int(seconds) in durations \
        else min(bisect.bisect_left(durations, int(seconds)), len(tiers) - 1)
    return tiers[idx]


def resolve_quality(model_id: str, seconds: float, quality: str = "") -> str:
    """精确配对解析：显式 quality 不在该时长档的可选清单里 → ApiError
    带允许清单（具体到档，不猜不钳）；空=该档默认（清单首位）。"""
    from ...middleware.error_handler import ApiError

    tier = _tier_for(tiers_for(model_id), seconds)
    allowed = [str(q) for q in tier.get("qualities") or []]
    wanted = (quality or "").strip()
    if not wanted:
        return allowed[0]
    if wanted not in allowed:
        raise ApiError(
            "VIDEO_QUALITY_TIER_INVALID",
            f"{int(tier['duration'])} 秒档不支持 {wanted}（该档可选："
            f"{'、'.join(allowed)}）",
            suggestion="请从确认弹窗的分档下拉里选择该时长支持的分辨率")
    return wanted
