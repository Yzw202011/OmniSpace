"""外部模型包登记（2026-09-03 体验流 2b：跨盘降级的后端侧）。

跨盘拖入（通道 A 把别的盘的 models 拖到启动图标）时，boot 会写
data/models_external.json 标记 + 生成 ComfyUI 跨盘 yaml；对话/漫剧
引擎按「注册表 file_path → 磁盘扫描 → 登记表兜底」解析路径
（model_manager.resolve_model_path），本模块在**后端启动期**按外部
models_manifest.json 把外部绝对路径幂等回填进 models 表——激活门禁
在 boot 阶段拦着 /api/v1，登记做不成 API，只能随启动跑一次。

口径：
  - 行形状完全照 importer 语义（api/models.py 导入端点同款）；
  - 同 id 既有行只回填物理字段（file_path/size/status），身份字段
    （category/purpose）保持登记原值——与导入端点撞名回填同一规矩；
  - 盘上不存在的外部条目跳过（与 registry_entry「条目失效返回
    None」语义一致，不写死路径）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...data.database import Database

import json
import logging
from pathlib import Path

logger = logging.getLogger("omnispace.models.external")

# manifest type → ModelCategory（data/models.py 枚举值）
_TYPE_TO_CATEGORY = {
    "dialog": "dialog",
    "image_gen": "vision",
    "video_gen": "video",
    "asr": "voice",
    "embedding": "auxiliary",
    "segmentation": "auxiliary",
    "auxiliary": "auxiliary",
}


def read_marker(marker_path: Path) -> str | None:
    """读 boot 写的外部模型标记，返回根目录（无效/缺省返回 None）。"""
    try:
        data = json.loads(marker_path.read_text("utf-8"))
        root = str(data.get("root") or "")
        return root if root and Path(root).is_dir() else None
    except (OSError, ValueError):
        return None


def external_rows_from_manifest(root: Path) -> list[dict]:
    """外部根的 manifest → 注册表行（纯函数，盘上存在性过滤）。

    大小直接采用 manifest 声称值——importer 的 rglob 实测对 260G 外部
    目录要跑数分钟，启动期等不起；空壳防线在 readiness 侧另有判据。
    """
    manifest_path = Path(root) / "models_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        entries: dict = manifest.get("models") or {}
    except (OSError, ValueError):
        return []
    rows: list[dict] = []
    for mid, meta in entries.items():
        if not isinstance(meta, dict):
            continue
        rel = str(meta.get("path") or mid)
        target = Path(root) / rel
        if not target.is_dir():
            continue  # 盘上没有：不写死路径（registry_entry 语义）
        size = float(meta.get("size_gb") or 0.0)
        rows.append({
            "id": mid,
            "name": str(meta.get("name") or mid),
            "category": _TYPE_TO_CATEGORY.get(str(meta.get("type") or ""),
                                              "auxiliary"),
            "purpose": "外部模型包（跨盘）",
            "size_gb": size,
            "params": "",
            "min_vram_gb": round(size * 1.2, 2) if size > 0 else 0.0,
            "associated_features": [],
            "status": "ready",
            "file_path": str(target.resolve()),
            "sha256": "",
        })
    return rows


def sync_external_models(db: Database, rows: list[dict]) -> dict:
    """幂等回填：同 id 只更新物理字段，新 id 插入（db=get_db_safe() 句柄）。"""
    result = {"inserted": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        try:
            existing = db.query_one(
                "SELECT id, file_path FROM models WHERE id = ?",
                (row["id"],))
            if existing is None:
                db.insert("models", row)
                result["inserted"] += 1
            elif (existing.get("file_path") or "") != row["file_path"]:
                db.update(
                    "models",
                    {"size_gb": row["size_gb"],
                     "min_vram_gb": row["min_vram_gb"],
                     "status": row["status"],
                     "file_path": row["file_path"],
                     "sha256": row["sha256"]},
                    "id = ?", (row["id"],))
                result["updated"] += 1
            else:
                result["unchanged"] += 1
        except Exception:  # noqa: BLE001 - 单条失败不拖垮其余登记
            logger.warning("外部模型登记失败：%s", row.get("id"), exc_info=True)
    return result


def bootstrap_external_models() -> dict | None:
    """后端启动期入口（main.py lifespan 调用）：标记 → manifest → 幂等回填。"""
    from ...config import DATA_DIR
    from ...data.database import get_db_safe

    root = read_marker(DATA_DIR / "models_external.json")
    if root is None:
        return None
    rows = external_rows_from_manifest(Path(root))
    db = get_db_safe()
    if db is None or not rows:
        return {"skipped": True, "rows": len(rows)}
    result = sync_external_models(db, rows)
    logger.info("外部模型包登记（%s）：%s", root, result)
    return result
