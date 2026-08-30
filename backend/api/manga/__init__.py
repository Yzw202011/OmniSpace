"""漫剧 API 包（TASK-P2-01：自单文件 manga.py 按路由域拆分）。

原 manga.py（4521 行 / 95 路由）拆为 8 模块，router 在此聚合——
main.py 经 importlib 动态导入 api.manga 并挂载 mod.router，对外零变化。

模块划分：
  common       共享层（状态兜底 / 行辅助 / 引擎单例 / 出图规格铁律，无路由）
  storyboard   分镜表 + 情绪检测
  keyframe     关键帧（生成/批量/回滚/故事生图）
  video        视频生成 + 媒体回读 + 叙事 + 模型清单
  voice        音色
  comic        漫画项目 + 剧本导入 + 场景 + 导出包
  comic_asset  资产库与资产 CRUD
  comic_gen    资产生成管线（四视图 / 批量 / 重生成）

（director 3D 导演台已于 2026-08-29 按用户裁定整链路剔除，
 director_* DB 表保留为历史死表——schema 历史组禁改只许追加。）
"""
from __future__ import annotations

from fastapi import APIRouter

from . import comic, comic_asset, comic_gen, keyframe, storyboard, video, voice

router = APIRouter()
router.include_router(storyboard.router)
router.include_router(keyframe.router)
router.include_router(video.router)
router.include_router(voice.router)
router.include_router(comic.router)
router.include_router(comic_asset.router)
router.include_router(comic_gen.router)

__all__ = ["router"]
