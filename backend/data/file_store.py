"""OmniSpace AI v2.1 文件存储管理（规格 §4.1 文件存储层 / §14 约束1）。

管理生成图片、视频、音频文件的存储路径，统一命名与目录结构。
提供 save_file / get_path / delete_file 方法。

规格引用：
  - §4.1 分层架构：文件存储层
  - §14 约束1：所有生成内容本地存储
  - §3.2 VideoGenResult.file_path / DrawResponse.images
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ..config import DATA_DIR

log = logging.getLogger("omnispace.filestore")

# 生成文件根目录
GENERATED_DIR = DATA_DIR / "generated"

# 子目录分类
SUBDIRS = {
    "image": GENERATED_DIR / "images",
    "video": GENERATED_DIR / "videos",
    "audio": GENERATED_DIR / "audio",
    "model": GENERATED_DIR / "models",
    "temp": GENERATED_DIR / "temp",
    "export": GENERATED_DIR / "exports",
}

# 文件类型 → 子目录映射
_TYPE_MAP = {
    "image": "image",
    "images": "image",
    "img": "image",
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "webp": "image",
    "gif": "image",
    "video": "video",
    "videos": "video",
    "mp4": "video",
    "mov": "video",
    "avi": "video",
    "webm": "video",
    "audio": "audio",
    "wav": "audio",
    "mp3": "audio",
    "flac": "audio",
    "ogg": "audio",
    "export": "export",
    "zip": "export",
    "temp": "temp",
}

# 各类型允许的扩展名白名单（防止路径穿越）
_ALLOWED_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "video": {".mp4", ".mov", ".avi", ".webm", ".mkv"},
    "audio": {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"},
    "model": {".safetensors", ".pt", ".pth", ".ckpt", ".onnx", ".bin", ".gguf"},
    "export": {".zip", ".json", ".md"},
    "temp": {".tmp", ".bin", ".dat"},
}


class FileStore:
    """文件存储管理器。

    目录结构：
      data/generated/images/   — 生成的图片
      data/generated/videos/   — 生成的视频
      data/generated/audio/    — 生成的音频
      data/generated/models/   — 导入/导出的模型文件
      data/generated/temp/     — 临时文件
      data/generated/exports/  — 导出的项目/分镜
    """

    def __init__(self) -> None:
        for subdir in SUBDIRS.values():
            subdir.mkdir(parents=True, exist_ok=True)
        log.info("文件存储目录就绪: %s", GENERATED_DIR)

    # ── 路径生成 ──────────────────────────────────────────────

    def get_path(self, file_type: str, filename: str | None = None,
                 ext: str = "") -> Path:
        """获取指定类型的文件存储路径。

        Args:
            file_type: 文件类型（image/video/audio/model/temp/export）
            filename: 可选文件名，不提供则自动生成 UUID
            ext: 扩展名（含点，如 .png），filename 为空时使用

        Returns:
            完整文件路径（尚未创建）
        """
        category = _TYPE_MAP.get(file_type.lower(), "temp")
        subdir = SUBDIRS[category]

        if filename is None or filename == "":
            name = uuid.uuid4().hex[:16]
            if ext and not ext.startswith("."):
                ext = "." + ext
            filename = f"{name}{ext}"
        else:
            # 防止路径穿越：仅取文件名部分
            filename = Path(filename).name

        return subdir / filename

    # ── 保存文件 ──────────────────────────────────────────────

    def save_file(self, file_type: str, data: bytes,
                  filename: str | None = None, ext: str = "") -> str:
        """将二进制数据保存为文件，返回相对路径。

        Args:
            file_type: 文件类型
            data: 文件内容字节
            filename: 可选文件名
            ext: 扩展名

        Returns:
            相对于 DATA_DIR 的路径字符串
        """
        category = _TYPE_MAP.get(file_type.lower(), "temp")
        # 扩展名校验
        if ext:
            if not ext.startswith("."):
                ext = "." + ext
            ext_lower = ext.lower()
            allowed = _ALLOWED_EXTS.get(category, set())
            if allowed and ext_lower not in allowed:
                log.warning("文件扩展名 %s 不在白名单(%s)，仍保存", ext_lower, category)

        path = self.get_path(file_type, filename, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.debug("文件已保存: %s (%d bytes)", path, len(data))

        # 返回相对路径（相对于 DATA_DIR，便于数据库存储与前端拼接）
        try:
            rel = path.relative_to(DATA_DIR)
        except ValueError:
            rel = path
        return str(rel).replace("\\", "/")

    def save_stream(self, file_type: str, src_path: str | Path,
                    filename: str | None = None) -> str:
        """将本地源文件复制到存储目录，返回相对路径。"""
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(f"源文件不存在: {src}")
        ext = src.suffix
        path = self.get_path(file_type, filename, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(path))
        try:
            rel = path.relative_to(DATA_DIR)
        except ValueError:
            rel = path
        return str(rel).replace("\\", "/")

    # ── 读取文件 ──────────────────────────────────────────────

    def resolve(self, rel_path: str) -> Path:
        """将相对路径解析为绝对路径（防止路径穿越）。"""
        base = DATA_DIR.resolve()
        target = (DATA_DIR / rel_path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(f"非法路径访问: {rel_path}")
        return target

    def read_file(self, rel_path: str) -> bytes:
        """读取文件内容，返回字节。"""
        path = self.resolve(rel_path)
        return path.read_bytes()

    def file_exists(self, rel_path: str) -> bool:
        """检查文件是否存在。"""
        try:
            return self.resolve(rel_path).exists()
        except ValueError:
            return False

    def file_size(self, rel_path: str) -> int:
        """返回文件大小（字节），不存在返回 0。"""
        try:
            path = self.resolve(rel_path)
            return path.stat().st_size if path.exists() else 0
        except (ValueError, OSError):
            return 0

    # ── 删除文件 ──────────────────────────────────────────────

    def delete_file(self, rel_path: str) -> bool:
        """删除文件，返回是否删除成功。"""
        try:
            path = self.resolve(rel_path)
            if path.exists() and path.is_file():
                path.unlink()
                log.debug("文件已删除: %s", path)
                return True
            return False
        except (ValueError, OSError) as exc:
            log.warning("删除文件失败 %s: %s", rel_path, exc)
            return False

    # ── 清理 ──────────────────────────────────────────────────

    def cleanup_temp(self, max_age_seconds: int = 3600) -> int:
        """清理临时目录中超过指定时长的文件，返回删除数。"""
        temp_dir = SUBDIRS["temp"]
        now = time.time()
        deleted = 0
        if not temp_dir.exists():
            return 0
        for item in temp_dir.iterdir():
            try:
                if item.is_file() and (now - item.stat().st_mtime) > max_age_seconds:
                    item.unlink()
                    deleted += 1
            except OSError:
                continue
        if deleted:
            log.info("清理临时文件 %d 个", deleted)
        return deleted

    def list_files(self, file_type: str, limit: int = 100) -> list[dict]:
        """列出指定类型的文件信息。"""
        category = _TYPE_MAP.get(file_type.lower(), "temp")
        subdir = SUBDIRS[category]
        items = []
        if not subdir.exists():
            return items
        for item in sorted(subdir.iterdir(), key=lambda p: p.stat().st_mtime,
                           reverse=True)[:limit]:
            if item.is_file():
                try:
                    rel = item.relative_to(DATA_DIR)
                except ValueError:
                    continue
                items.append({
                    "path": str(rel).replace("\\", "/"),
                    "name": item.name,
                    "size": item.stat().st_size,
                    "modified": item.stat().st_mtime,
                })
        return items


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_filestore_instance: Optional[FileStore] = None
_filestore_lock = threading.Lock()


def get_file_store() -> FileStore:
    """获取全局文件存储管理器单例。"""
    global _filestore_instance
    if _filestore_instance is None:
        with _filestore_lock:
            if _filestore_instance is None:
                _filestore_instance = FileStore()
    return _filestore_instance
