"""OmniSpace AI v2.1 模型导入模块（规格 §5.4 模型导入流程）。

模型导入流程:
  1. 校验路径有效性
  2. 自动分类（ModelClassifier）
  3. 计算 SHA256（ModelValidator）
  4. 提取元信息（大小、参数量等）
  5. 返回 ImportResult
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...data.models import ModelCategory
from ...middleware.error_handler import ApiError
from .classifier import ModelClassifier
from .validator import ModelValidator

log = logging.getLogger("omnispace.model_manager.importer")


@dataclass
class ImportResult:
    """模型导入结果。"""

    success: bool = False
    model_id: str = ""
    name: str = ""
    category: ModelCategory = ModelCategory.AUXILIARY
    size_gb: float = 0.0
    sha256: str = ""
    file_path: str = ""
    error: str = ""
    warnings: list = field(default_factory=list)


class ModelImporter:
    """模型导入器——自动分类 + SHA256 校验 + 元信息提取。"""

    def __init__(self) -> None:
        self.classifier = ModelClassifier()
        self.validator = ModelValidator()

    def import_model(self, path: str, *, max_sha_gb: float = 1.0) -> ImportResult:
        """导入模型文件或目录。

        规格 §5.4 流程:
          1. 校验路径
          2. 自动分类
          3. SHA256 计算（超过 max_sha_gb 的大文件跳过——全量哈希
             数十 GB 权重会把导入请求卡住数分钟，需要时走校验按钮）
          4. 提取大小信息
          5. 生成 model_id

        Args:
            path: 模型文件或目录的路径
            max_sha_gb: 单文件超过该 GB 数时跳过 SHA256（0 = 一律跳过）

        Returns:
            ImportResult 导入结果

        Raises:
            ApiError: 路径无效时
        """
        result = ImportResult()
        file_path = Path(path)

        # 1. 校验路径
        if not file_path.exists():
            raise ApiError(
                code=30001,
                message=f"模型文件未找到: {path}",
                suggestion="请检查路径是否正确",
            )

        result.file_path = str(file_path.resolve())

        # 2. 自动分类
        try:
            category = self.classifier.classify(path)
            result.category = category
            log.info("模型分类: %s -> %s", file_path.name, category.value)
        except Exception as e:
            result.error = f"分类失败: {e}"
            result.category = ModelCategory.AUXILIARY
            log.warning("模型分类失败，默认 AUXILIARY: %s", e)

        # 3. 提取名称
        result.name = file_path.stem if file_path.is_file() else file_path.name

        # 4. 计算大小
        try:
            result.size_gb = self._compute_size(file_path)
        except Exception as e:
            result.warnings.append(f"大小计算失败: {e}")
            result.size_gb = 0.0

        # 5. 计算 SHA256（仅对文件；大文件跳过防导入卡死）
        if file_path.is_file():
            if file_path.stat().st_size > max_sha_gb * (1024 ** 3):
                result.warnings.append(
                    f"文件超过 {max_sha_gb:.0f}GB，跳过导入时 SHA256"
                    "（可在模型管理页用「校验」按需计算）")
            else:
                try:
                    result.sha256 = self.validator.compute_sha256(str(file_path))
                    log.info("SHA256: %s...", result.sha256[:16])
                except Exception as e:
                    result.warnings.append(f"SHA256 计算失败: {e}")
        else:
            result.warnings.append("目录模型跳过 SHA256 校验")

        # 6. 生成 model_id
        result.model_id = self._generate_model_id(result.name, result.category)

        # 7. 最终校验
        if not result.error:
            result.success = True
            log.info(
                "模型导入成功: %s (类型=%s, 大小=%.2fGB)",
                result.name,
                result.category.value,
                result.size_gb,
            )

        return result

    def classify(self, path: str) -> ModelCategory:
        """对模型路径进行分类（委托给 ModelClassifier）。

        Args:
            path: 模型文件或目录路径

        Returns:
            ModelCategory 枚举值
        """
        return self.classifier.classify(path)

    def _compute_size(self, path: Path) -> float:
        """计算文件或目录大小（GB）。"""
        if path.is_file():
            return round(path.stat().st_size / (1024 ** 3), 3)

        # 目录：递归求和
        total = 0
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return round(total / (1024 ** 3), 3)

    def _generate_model_id(self, name: str, category: ModelCategory) -> str:
        """生成模型唯一 ID。

        格式: {category}_{name}_{sha256前8位或时间戳}
        """
        import time
        timestamp = hex(int(time.time()))[2:10]
        safe_name = name.replace(" ", "_").replace(".", "_").lower()[:32]
        return f"{category.value}_{safe_name}_{timestamp}"
