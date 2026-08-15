"""OmniSpace AI v2.1 模型校验模块（规格 §5.4 SHA256 校验）。

提供模型文件完整性校验功能，使用 SHA256 哈希值验证模型文件未被篡改。
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from ...data.models import ModelInfo, ModelStatus

logger = logging.getLogger("omnispace.model_manager.validator")

# SHA256 分块读取大小（8MB，平衡内存与速度）
_CHUNK_SIZE = 8 * 1024 * 1024


class ModelValidator:
    """模型校验器——SHA256 完整性校验。"""

    def compute_sha256(self, path: str) -> str:
        """计算文件的 SHA256 哈希值。

        使用分块读取避免大文件内存溢出。

        Args:
            path: 文件路径

        Returns:
            64 字符的十六进制 SHA256 字符串

        Raises:
            FileNotFoundError: 文件不存在
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"模型文件不存在: {path}")

        sha256 = hashlib.sha256()
        file_size = file_path.stat().st_size
        processed = 0

        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                processed += len(chunk)
                if processed % (_CHUNK_SIZE * 16) == 0:  # 每 128MB 记录一次
                    pct = processed / file_size * 100 if file_size > 0 else 100
                    logger.debug("SHA256 计算进度: %.1f%%", pct)

        return sha256.hexdigest()

    def verify_model(self, model: ModelInfo) -> bool:
        """校验模型文件的完整性。

        规格 §5.4: 导入时计算 SHA256 并存储，后续加载时重新计算并比对。

        Args:
            model: 模型信息（含 file_path 和 sha256）

        Returns:
            True 如果校验通过，False 如果不匹配或文件不存在
        """
        if not model.file_path:
            logger.warning("模型 %s 未设置文件路径", model.id)
            return False

        file_path = Path(model.file_path)
        if not file_path.exists():
            logger.error("模型文件不存在: %s", model.file_path)
            return False

        # 如果没有存储的哈希值，计算并返回 True
        if not model.sha256:
            model.sha256 = self.compute_sha256(model.file_path)
            logger.info("模型 %s 首次计算 SHA256: %s", model.id, model.sha256[:16] + "...")
            return True

        # 计算当前哈希并比对
        current_hash = self.compute_sha256(model.file_path)
        if current_hash == model.sha256:
            logger.debug("模型 %s SHA256 校验通过", model.id)
            return True
        else:
            logger.error(
                "模型 %s SHA256 校验失败: 期望 %s, 实际 %s",
                model.id,
                model.sha256[:16] + "...",
                current_hash[:16] + "...",
            )
            return False

    def verify_path(self, path: str, expected_sha256: Optional[str] = None) -> tuple[bool, str]:
        """校验文件路径的完整性。

        Args:
            path: 文件路径
            expected_sha256: 期望的 SHA256 值（可选）

        Returns:
            (是否通过, 实际的 SHA256 值)
        """
        actual_hash = self.compute_sha256(path)
        if expected_sha256 is None:
            return True, actual_hash
        return actual_hash == expected_sha256, actual_hash
