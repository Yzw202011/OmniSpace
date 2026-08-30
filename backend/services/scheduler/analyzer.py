"""OmniSpace AI v2.1 瓶颈分析模块（规格 §5.1 分析层）。

根据硬件监控数据判断当前应进入哪种协同模式。
规格 §5.1 定义了 6 种协同模式（SynergyMode）:
  1. GPU_PRIMARY      — GPU 主力，资源充裕
  2. CPU_ASSIST       — GPU 压力，CPU 辅助
  3. GPU_ASSIST_CPU   — GPU 受限，CPU 为主
  4. MEMORY_PRESSURE  — 内存压力，需压缩缓存
  5. ALL_TENSE        — 全面紧张，强制降级
  6. ALL_IDLE         — 全部空闲，可预加载
"""

from __future__ import annotations

import time

from ...config import THRESHOLDS
from ...data.models import SynergyMode


class BottleneckAnalyzer:
    """瓶颈分析器——按 6 个场景判断最优协同模式。

    文档B §4.1：GPU 利用率 >95% 需持续才判定为临界（降低生成质量），
    瞬时尖峰不触发——用 _gpu_util_high_since 跟踪持续起点。
    P2 分级降参：越线持续 5s → 轻度降参（level=1），15s → 深度降档
    （level=2）。单档 10s 偏长，超高频生成空转体验差。
    """

    def __init__(self) -> None:
        # GPU 利用率越临界线的持续起点（time.monotonic 时间戳，0=未越线）
        self._gpu_util_high_since: float = 0.0
        # S4 滞回：低于临界线的持续起点（0=当前在阈值之上）。短暂回
        # 落未连续满 gpu_util_reset_grace_s 不清零 high_since，避免
        # 步间/镜间空隙的单采样抖动把已累积持续窗口打回零。
        self._gpu_util_low_since: float = 0.0
        # 最近一次 analyze 的持续越线判定结果：
        #   last_gpu_util_critical（bool，供 scheduler tick 同步质量总督旗标）
        #   last_gpu_util_level（0/1/2，0=正常 1=轻度降参 2=深度降档，
        #                      驱动在途生成任务分级降参）
        self.last_gpu_util_critical: bool = False
        self.last_gpu_util_level: int = 0

    def analyze(
        self,
        gpu: dict,
        cpu: dict,
        mem: dict,
        power: str,
    ) -> SynergyMode:
        """分析硬件状态，返回推荐的协同模式。

        Args:
            gpu: GPU 监控数据 {vram_used_mb, vram_total_mb, util_percent, temp_celsius}
            cpu: CPU 监控数据 {usage_percent, temp_celsius, cores, threads}
            mem: 内存监控数据 {available_gb, total_gb, used_percent}
            power: 电源状态 'ac' | 'battery'

        Returns:
            SynergyMode 枚举值
        """
        # ── 计算各项比率 ──────────────────────────────────────────
        gpu_vram_ratio = 0.0
        if gpu.get("vram_total_mb", 0) > 0:
            gpu_vram_ratio = gpu["vram_used_mb"] / gpu["vram_total_mb"]

        gpu_util = gpu.get("util_percent", 0.0) / 100.0
        gpu_temp = gpu.get("temp_celsius", 0.0)
        cpu_util = cpu.get("usage_percent", 0.0) / 100.0
        cpu_temp = cpu.get("temp_celsius", 0.0)
        # 可用内存比率 = available / total
        mem_available_ratio = 0.0
        if mem.get("total_gb", 0) > 0:
            mem_available_ratio = mem["available_gb"] / mem["total_gb"]

        # ── 阈值常量 ──────────────────────────────────────────────
        gpu_vram_warn = THRESHOLDS["gpu_vram_warning"]
        gpu_vram_crit = THRESHOLDS["gpu_vram_critical"]
        gpu_vram_force = THRESHOLDS["gpu_vram_force_unload"]
        gpu_util_warn = THRESHOLDS["gpu_util_warning"]
        gpu_util_crit = THRESHOLDS["gpu_util_critical"]
        gpu_temp_warn = THRESHOLDS["gpu_temp_warning"]
        gpu_temp_crit = THRESHOLDS["gpu_temp_critical"]
        cpu_util_warn = THRESHOLDS["cpu_util_warning"]
        cpu_util_crit = THRESHOLDS["cpu_util_critical"]
        cpu_temp_warn = THRESHOLDS["cpu_temp_warning"]
        mem_warn_ratio = THRESHOLDS["mem_warning_ratio"]
        mem_crit_ratio = THRESHOLDS["mem_critical_ratio"]

        # ── 判断标志位 ────────────────────────────────────────────
        gpu_vram_critical = gpu_vram_ratio >= gpu_vram_crit
        gpu_vram_warning = gpu_vram_ratio >= gpu_vram_warn
        gpu_util_warning = gpu_util >= gpu_util_warn
        # 文档B §4.1 + P2 分级降参：GPU 利用率 >95% 持续 5s 轻度降参、
        # 持续 15s 深度降档（瞬时尖峰忽略）。
        gpu_util_mild_s = float(
            THRESHOLDS.get("gpu_util_mild_sustained_s", 5))
        gpu_util_deep_s = float(
            THRESHOLDS.get("gpu_util_deep_sustained_s", 15))
        gpu_util_grace_s = float(
            THRESHOLDS.get("gpu_util_reset_grace_s", 4))
        # S5 进程级归因（2026-08-28 V77 根修）：持续越线判定改用外部
        # 归因利用率（整卡 − 本进程树）——自身生成跑满 GPU 是正常工
        # 况而非争抢，V77 批量关键帧 36 步满血采样被总督自伤降至
        # 14 步即源于此；仅外部进程（游戏/浏览器/孤儿 worker）争抢
        # 算力才降参。NVML 进程级采样不可用时回退整卡口径。协同模式
        # 分类（warning/idle 等）仍用整卡 gpu_util——无论谁占用，
        # GPU 忙就是忙。
        gpu_ext_util = gpu.get("external_util_percent")
        gpu_util_gov = (float(gpu_ext_util) / 100.0
                        if gpu_ext_util is not None else gpu_util)
        if gpu_util_gov >= gpu_util_crit:
            self._gpu_util_low_since = 0.0
            if self._gpu_util_high_since == 0.0:
                self._gpu_util_high_since = time.monotonic()
            elapsed = time.monotonic() - self._gpu_util_high_since
            # 轻度降参会先于深度降档命中（5s vs 15s）
            gpu_util_critical = elapsed >= gpu_util_mild_s
            level = 2 if elapsed >= gpu_util_deep_s else (1 if gpu_util_critical else 0)
        else:
            # S4 滞回：短暂回落未连续满 grace 秒不清零窗口（步间/镜间
            # 空隙的单采样抖动不应把已累积 5s/15s 打回零）；回落持续
            # 满 grace 才判定高负载窗口真正结束。宽限内窗口视为未中
            # 断，沿用已累积时长继续判定。
            if self._gpu_util_high_since != 0.0:
                if self._gpu_util_low_since == 0.0:
                    self._gpu_util_low_since = time.monotonic()
                elif (time.monotonic() - self._gpu_util_low_since
                        >= gpu_util_grace_s):
                    self._gpu_util_high_since = 0.0
                    self._gpu_util_low_since = 0.0
            if self._gpu_util_high_since != 0.0:
                elapsed = time.monotonic() - self._gpu_util_high_since
                gpu_util_critical = elapsed >= gpu_util_mild_s
                level = 2 if elapsed >= gpu_util_deep_s else (
                    1 if gpu_util_critical else 0)
            else:
                gpu_util_critical = False
                level = 0
        # F-10 / P2：暴露持续越线判定，供 scheduler tick 同步质量总督
        # 分级降参旗标（level 0/1/2）
        self.last_gpu_util_critical = gpu_util_critical
        self.last_gpu_util_level = level
        gpu_temp_critical = gpu_temp >= gpu_temp_crit
        gpu_temp_warning = gpu_temp >= gpu_temp_warn
        cpu_critical = cpu_util >= cpu_util_crit or cpu_temp >= cpu_temp_warn
        mem_critical = mem_available_ratio <= mem_crit_ratio
        mem_warning = mem_available_ratio <= mem_warn_ratio

        # ── 场景 5: ALL_TENSE — 全面紧张 ─────────────────────────
        # 审计 P0-4 修复（规格 §5.1 场景5 定义）：
        #   条件1：GPU 紧张（显存/利用率危险线）且 CPU 或 内存 任一同时
        #           紧张 → 全面紧张（多资源共振，单资源紧张另有专属场景）；
        #   条件2：GPU 温度达临界值单独成立 → 直接全面紧张
        #          （热保护最高优先，防止硬件过热损伤）。
        gpu_tense = gpu_vram_critical or gpu_util_critical
        if gpu_tense and (cpu_critical or mem_critical):
            return SynergyMode.ALL_TENSE
        if gpu_temp_critical:
            return SynergyMode.ALL_TENSE

        # ── 场景 4: MEMORY_PRESSURE — 内存压力 ───────────────────
        # 可用内存低于 critical 线
        if mem_critical:
            return SynergyMode.MEMORY_PRESSURE

        # ── 场景 3: GPU_ASSIST_CPU — GPU 严重受限，切到 CPU ──────
        # 显存或温度达到 force_unload / critical
        if gpu_vram_ratio >= gpu_vram_force or gpu_temp_critical:
            return SynergyMode.GPU_ASSIST_CPU

        # ── 场景 2: CPU_ASSIST — GPU 有压力，CPU 辅助 ────────────
        # GPU 显存或利用率达到 warning 线
        if gpu_vram_warning or gpu_util_warning or gpu_temp_warning:
            return SynergyMode.CPU_ASSIST

        # ── 场景 6: ALL_IDLE — 全部空闲 ──────────────────────────
        # GPU 利用率低 + CPU 利用率低 + 内存充裕
        if (
            gpu_util < gpu_util_warn * 0.5
            and cpu_util < cpu_util_warn * 0.5
            and not mem_warning
        ):
            return SynergyMode.ALL_IDLE

        # ── 场景 1: GPU_PRIMARY — 默认 GPU 主力 ──────────────────
        return SynergyMode.GPU_PRIMARY
