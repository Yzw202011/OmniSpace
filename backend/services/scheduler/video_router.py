"""OmniSpace AI v2.1 视频模型路由模块（规格 §5.2 视频路由表）。

根据可用显存从 VIDEO_ROUTING_TABLE 中选择最合适的视频模型，
并返回该模型的生成参数。
"""

from __future__ import annotations

from ...config import LTX2_MAX_AUDIO_SYNC, VIDEO_MAX_DURATION
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
                code=30005,
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
            VideoModel.LTX2: {
                "max_resolution": "1080p",
                "max_fps": 30,
                "max_duration_seconds": VIDEO_MAX_DURATION,
                "precision": "bf16",
                "supports_audio_sync": True,
                "max_audio_sync_seconds": LTX2_MAX_AUDIO_SYNC,
                "vram_required_gb": 24,
            },
            VideoModel.WAN21_14B_FP8: {
                "max_resolution": "1080p",
                "max_fps": 24,
                "max_duration_seconds": VIDEO_MAX_DURATION,
                "precision": "fp8",
                "supports_audio_sync": True,
                "max_audio_sync_seconds": LTX2_MAX_AUDIO_SYNC,
                "vram_required_gb": 16,
            },
            VideoModel.WAN21_14B_INT4: {
                "max_resolution": "720p",
                "max_fps": 24,
                "max_duration_seconds": VIDEO_MAX_DURATION,
                "precision": "int4",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 12,
            },
            VideoModel.WAN21_1_3B: {
                "max_resolution": "720p",
                "max_fps": 24,
                "max_duration_seconds": VIDEO_MAX_DURATION,
                "precision": "fp16",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 8,
            },
            VideoModel.WAN22_TI2V_5B: {
                # Wan2.2-TI2V-5B：单 ckpt 原生双条件（umT5 文本 + 首帧
                # 图），I2V 与文+图生视频统一底座（2026-08-23 混合架构）。
                # split 布局 16GB 卡可跑；720p 预设实际映射 1024x576
                # （引擎 Wan 家族对齐），激活余量充裕。官方片段 5s
                # （121 帧 @24fps）。
                "max_resolution": "720p",
                "max_fps": 24,
                "max_duration_seconds": 5,
                "precision": "bf16",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 13,
            },
            VideoModel.LTX_VIDEO_095: {
                # LTX-Video 0.9.5 2B：768x512@24fps 原生，
                # T5 int8 量化 + CPU offload 兜底，16GB 可跑 10s 片段
                "max_resolution": "768x512",
                "max_fps": 24,
                "max_duration_seconds": 10,
                "precision": "int8",
                "supports_audio_sync": False,
                "max_audio_sync_seconds": 0,
                "vram_required_gb": 8,
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
                code=60001,
                message=f"视频时长超出上限: {request.duration_seconds}s > {VIDEO_MAX_DURATION}s",
                suggestion=f"请将时长限制在 {VIDEO_MAX_DURATION} 秒以内",
            )

        # 模型上限
        if request.duration_seconds > max_duration:
            raise ApiError(
                code=60001,
                message=f"当前模型 {model.value} 最大支持 {max_duration} 秒",
                suggestion="请缩短时长或选择更高规格的模型",
            )

        # 音画同步限制（规格 §10.1: LTX-2 音画同步最长 10 秒）
        if request.audio_path and params["supports_audio_sync"]:
            if request.duration_seconds > params["max_audio_sync_seconds"]:
                raise ApiError(
                    code=60002,
                    message=f"音画同步模式最长支持 {params['max_audio_sync_seconds']} 秒",
                    suggestion="请缩短视频时长或移除音频",
                )
        elif request.audio_path and not params["supports_audio_sync"]:
            raise ApiError(
                code=60001,
                message=f"当前模型 {model.value} 不支持音画同步",
                suggestion="请选择支持音画同步的模型（LTX-2 或 Wan2.1-14B-FP8）",
            )
