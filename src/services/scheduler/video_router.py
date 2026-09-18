"""OmniSpace AI v2.1 视频模型路由模块（规格 §5.2 视频路由表）。

根据可用显存从 VIDEO_ROUTING_TABLE 中选择最合适的视频模型，
并返回该模型的生成参数。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

from ...config import VIDEO_MAX_DURATION
from ...data.models import VIDEO_ROUTING_TABLE, VideoGenerateRequest, VideoModel
from ...middleware.error_handler import ApiError


class VideoRouter:
    """视频模型路由器——按显存自动选模型（规格 §5.2）。"""

    def select_model(
        self,
        available_vram_gb: float,
        user_override: str | None = None,
    ) -> VideoModel:
        """根据可用显存选择视频模型。

        规格 §5.2 路由表（从高到低）:
          - 24GB+ -> LTX-2
          - 16GB+ -> Wan2.1-14B-FP8
          - 13GB+ -> Wan2.2-TI2V-5B（视频统一底座，I2V/TI2V）
          - 12GB+ -> Wan2.1-14B-INT4
          - 8GB+  -> Wan2.1-1.3B
          - 6GB+  -> CogVideoX-2B
          - 0GB+  -> CogVideoX-2B-CPU

        Args:
            available_vram_gb: 可用显存（GB）
            user_override: 用户手动指定的模型名（可选）

        Returns:
            VideoModel 枚举值

        Raises:
            ApiError: 用户指定的模型名无效时
        """
        # 用户手动指定
        if user_override:
            for model in VideoModel:
                if model.value == user_override or model.name == user_override:
                    return model
            raise ApiError(
                code="MODEL_TYPE_MISMATCH",
                message=f"未知的视频模型: {user_override}",
                suggestion="请从 VIDEO_ROUTING_TABLE 中选择有效模型",
            )

        # 按路由表自动选择
        for entry in VIDEO_ROUTING_TABLE:
            if available_vram_gb >= entry["min_vram_gb"]:
                return entry["model"]

        # 兜底：CPU 模式
        return VideoModel.COGVIDEOX_2B_CPU

    def get_generation_params(self, model: VideoModel) -> dict:
        """返回指定模型的生成参数预设。

        不同模型有不同的分辨率、帧数、精度等参数限制。
        """
        params_map = {
            VideoModel.MINIMAX_H3: {
                # MiniMax H3 33B（ComfyUI 子进程管线，2026-08-25）：
                # 画幅 768 短边（1344x768 顶格，"720p" 档映射 864x480）；
                # 24fps 17k+5 帧网格，训练范围 5~15s；NVFP4 DiT +
                # int4 convrot 编码器，DynamicVRAM 分时换载峰值 ~12GB。
                # 原生音画联合生成（32kHz 立体声），无需外部音频同步。
                "max_resolution": "1080p",
                "max_fps": 24,
                "max_duration_seconds": 15,
                "precision": "nvfp4",
                "supports_audio_sync": True,
                "max_audio_sync_seconds": 15,
                "vram_required_gb": 16,
            },
            VideoModel.COGVIDEOX_2B: {
                "max_resolution": "720p",
                "max_fps": 24,
                "max_duration_seconds": 10,
                "precision": "fp16",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 6,
            },
            VideoModel.ANIMATELCM: {
                # 随包附带的 AnimateLCM 运动模块（约 2GB）：
                # 需搭配 SD1.5 基座使用，512x512 短片段，无音画同步
                "max_resolution": "512x512",
                "max_fps": 16,
                "max_duration_seconds": 4,
                "precision": "fp16",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 2,
            },
            VideoModel.COGVIDEOX_2B_CPU: {
                "max_resolution": "480p",
                "max_fps": 16,
                "max_duration_seconds": 5,
                "precision": "fp16",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 0,
            },
        }
        return params_map.get(model, params_map[VideoModel.COGVIDEOX_2B_CPU])

    def validate_video_duration(
        self, request: VideoGenerateRequest, model: VideoModel
    ) -> None:
        """校验视频时长是否在模型允许范围内（规格 §10.1）。

        Args:
            request: 视频生成请求
            model: 选中的视频模型

        Raises:
            ApiError: 时长超限时
        """
        params = self.get_generation_params(model)
        max_duration = params["max_duration_seconds"]

        # 全局上限
        if request.duration_seconds > VIDEO_MAX_DURATION:
            raise ApiError(
                code="VIDEO_GENERATION_FAILED",
                message=f"视频时长超出上限: {request.duration_seconds}s > {VIDEO_MAX_DURATION}s",
                suggestion=f"请将时长限制在 {VIDEO_MAX_DURATION} 秒以内",
            )

        # 模型上限
        if request.duration_seconds > max_duration:
            raise ApiError(
                code="VIDEO_GENERATION_FAILED",
                message=f"当前模型 {model.value} 最大支持 {max_duration} 秒",
                suggestion="请缩短时长或选择更高规格的模型",
            )

        # 音画同步限制（规格 §10.1: LTX-2 音画同步最长 10 秒）
        if request.audio_path and params["supports_audio_sync"]:
            if request.duration_seconds > params["max_audio_sync_seconds"]:
                raise ApiError(
                    code="VIDEO_DURATION_EXCEEDED",
                    message=f"音画同步模式最长支持 {params['max_audio_sync_seconds']} 秒",
                    suggestion="请缩短视频时长或移除音频",
                )
        elif request.audio_path and not params["supports_audio_sync"]:
            raise ApiError(
                code="VIDEO_GENERATION_FAILED",
                message=f"当前模型 {model.value} 不支持音画同步",
                suggestion="请选择支持音画同步的模型（LTX-2 或 Wan2.1-14B-FP8）",
            )
