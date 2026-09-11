"""OmniSpace AI v2.1 模型自动选择模块（规格 §5.3 模型自动选择）。

根据功能需求和可用显存，从路由表中自动选择最合适的模型。
覆盖对话、绘画、视频、语音四大功能。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import logging

from ...data.models import (
    DIALOG_ROUTING_TABLE,
    PAINT_ROUTING_TABLE,
    VIDEO_ROUTING_TABLE,
    ActiveFeature,
    VideoModel,
)
from .classifier import ModelClassifier

logger = logging.getLogger("omnispace.model_manager.selector")


class ModelSelector:
    """模型自动选择器——按功能 + 显存 + 已下载状态选模型（规格 §5.3）。

    仅推荐本地已下载的模型：路由表从高到低遍历，跳过未下载条目，
    确保推荐结果可直接加载（预加载/预测不失败）。
    """

    def __init__(self) -> None:
        self._classifier = ModelClassifier()

    def _get_downloaded_ids(self) -> set[str]:
        """获取本地已下载的模型 id 集合（从 ModelManager 缓存/扫描）。"""
        try:
            from . import get_model_manager  # 延迟导入避免循环
            mgr = get_model_manager()
            scanned = mgr.scan_downloaded_models()
            return set(scanned.keys())
        except Exception:  # noqa: BLE001 - 扫描失败不过滤，保守返回空集
            return set()

    def select_for_feature(
        self,
        feature: str,
        available_vram_gb: float,
    ) -> str:
        """为指定功能自动选择最合适的模型。

        规格 §5.3 路由逻辑:
          - dialog -> DIALOG_ROUTING_TABLE
          - paint  -> PAINT_ROUTING_TABLE
          - video_gen -> VIDEO_ROUTING_TABLE
          - voice  -> 语音模型路由

        Args:
            feature: 功能名（dialog/paint/video_gen/voice/training）
            available_vram_gb: 可用显存（GB）

        Returns:
            模型标识字符串
        """
        feature_lower = feature.lower().strip()

        if feature_lower in ("dialog", "chat"):
            return self._select_from_table(DIALOG_ROUTING_TABLE, available_vram_gb)

        elif feature_lower in ("paint", "image", "draw"):
            return self._select_from_table(PAINT_ROUTING_TABLE, available_vram_gb)

        elif feature_lower in ("video_gen", "video", "video_generation"):
            # 修复：原误写为 self._select_video_model（不存在该方法），
            # 运行时抛 AttributeError；公开方法为 select_video_model。
            return self.select_video_model(available_vram_gb).value

        elif feature_lower in ("voice", "tts", "voice_synthesis"):
            return self.select_voice(available_vram_gb)

        elif feature_lower in ("training", "train", "lora"):
            # 训练使用与对话相同的模型路由表
            return self._select_from_table(DIALOG_ROUTING_TABLE, available_vram_gb)

        else:
            logger.warning("未知功能 '%s'，使用对话模型兜底", feature)
            return self._select_from_table(DIALOG_ROUTING_TABLE, available_vram_gb)

    def select_video_model(self, available_vram_gb: float) -> VideoModel:
        """专门为视频功能选择模型（已下载优先，未下载回退原始行为）。

        Args:
            available_vram_gb: 可用显存（GB）

        Returns:
            VideoModel 枚举值
        """
        downloaded = self._get_downloaded_ids()

        # 第一遍：显存满足 + 已下载
        for entry in VIDEO_ROUTING_TABLE:
            if available_vram_gb < entry["min_vram_gb"]:
                continue
            model = entry["model"]
            if model.value in downloaded:
                return model

        # 第二遍：全部未下载时回退（仅按显存）
        for entry in VIDEO_ROUTING_TABLE:
            if available_vram_gb >= entry["min_vram_gb"]:
                return entry["model"]

        return VideoModel.COGVIDEOX_2B_CPU

    def select_voice(self, available_vram_gb: float) -> str:
        """选择语音合成模型。

        语音模型路由:
          - 8GB+  -> CosyVoice3（高质量）
          - 4GB+  -> ChatTTS（中等）
          - 0GB+  -> 内置轻量 TTS（CPU 降级）

        Args:
            available_vram_gb: 可用显存（GB）

        Returns:
            语音模型标识
        """
        if available_vram_gb >= 8:
            return "cosyvoice3"
        elif available_vram_gb >= 4:
            return "chattts"
        else:
            return "pymarble-tts-cpu"  # CPU 降级

    def select_by_feature_enum(
        self,
        feature: ActiveFeature,
        available_vram_gb: float,
    ) -> str:
        """使用 ActiveFeature 枚举选择模型。

        Args:
            feature: ActiveFeature 枚举值
            available_vram_gb: 可用显存（GB）

        Returns:
            模型标识字符串
        """
        return self.select_for_feature(feature.value, available_vram_gb)

    def _select_from_table(self, table: list, available_vram_gb: float) -> str:
        """从路由表中选择第一个满足显存要求且已下载的模型。

        遍历路由表（从高到低），跳过未下载条目；全部未下载时
        回退到不检查下载状态的原始行为（保持向后兼容）。

        Args:
            table: 路由表（按显存从高到低排序）
            available_vram_gb: 可用显存（GB）

        Returns:
            模型标识字符串
        """
        downloaded = self._get_downloaded_ids()

        # 第一遍：显存满足 + 已下载
        for entry in table:
            if available_vram_gb < entry["min_vram_gb"]:
                continue
            model_val = entry["model"]
            mid = model_val.value if hasattr(model_val, "value") else str(model_val)
            if mid in downloaded:
                logger.debug(
                    "选择模型（已下载）: %s (需要 >= %dGB, 实际 %.1fGB)",
                    mid, entry["min_vram_gb"], available_vram_gb,
                )
                return mid

        # 第二遍：全部未下载时回退原始行为（仅按显存）
        for entry in table:
            if available_vram_gb >= entry["min_vram_gb"]:
                model_val = entry["model"]
                mid = model_val.value if hasattr(model_val, "value") else str(model_val)
                logger.debug(
                    "选择模型（未下载，回退）: %s (需要 >= %dGB, 实际 %.1fGB)",
                    mid, entry["min_vram_gb"], available_vram_gb,
                )
                return mid

        # 兜底：返回最后一个（最低规格）
        last = table[-1]["model"]
        return last.value if hasattr(last, "value") else str(last)
