"""OmniSpace AI v2.3.1 后台服务优先级定义（文档 §8.4.2 资源调度协调）。

优先级队列（数值越小优先级越高）：
  P0（用户操作）> P1（模型预加载）> P2（LoRA训练）> P3（浏览器学习）
  > P4（行为学习记录）> P5（知识库清理）

统一在此定义，供学习调度门控（learning_scheduler）、LoRA 训练队列
（lora_training_service）、风格训练队列（style_lora_service）等模块引用，
避免各模块散落魔数导致优先级语义漂移。
"""
from __future__ import annotations

from enum import IntEnum


class Priority(IntEnum):
    """后台任务优先级（文档 §8.4.2）。"""

    P0_USER = 0           # 用户操作（前台交互，最高优先级）
    P1_PRELOAD = 1        # 模型预加载
    P2_TRAINING = 2       # LoRA 训练（知识/风格）
    P3_BROWSER_LEARN = 3  # 浏览器学习
    P4_BEHAVIOR = 4       # 行为学习记录
    P5_CLEANUP = 5        # 知识库清理（最低优先级）

    @property
    def label(self) -> str:
        """中文标签（日志/状态展示用）。"""
        return _LABELS[self]


_LABELS: dict[Priority, str] = {
    Priority.P0_USER: "用户操作",
    Priority.P1_PRELOAD: "模型预加载",
    Priority.P2_TRAINING: "LoRA训练",
    Priority.P3_BROWSER_LEARN: "浏览器学习",
    Priority.P4_BEHAVIOR: "行为学习记录",
    Priority.P5_CLEANUP: "知识库清理",
}
# 本项目仅供学习使用，商业授权请+Q 3559331368

# ── 训练队列相对优先级映射（B5 步4 单源收敛 2026-09-14）──────────────
# 原先 lora_training_service 与 style_lora_service 各自克隆同形 dict
# （克隆漂移前夜）——收敛于此。数值为**队列内相对级**（小者先），
# 与 Priority 枚举的全局档位（§8.4.2，训练=P2）是两套坐标，勿混用。
LEVEL_BY_NAME: dict[str, int] = {"high": 0, "medium": 1, "low": 2,
                                 "background": 3}
