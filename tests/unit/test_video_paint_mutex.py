"""互斥增强单测（2026-10-05 用户令）：H3 视频链（video_gen 本地道）
运行时，本地 paint 任务（关键帧/资产图）排队让位——防 RAM/显存双
大户叠跑卡死（云道任务不受此限）。"""
from __future__ import annotations

from src.services.image_queue import ImageTaskQueue
from src.services.video_queue import VideoTaskQueue


def test_video_local_busy_reads_host_flag(monkeypatch) -> None:
    """_video_local_busy 读取 video 队列宿主 _lock_held 旗标。"""
    q = ImageTaskQueue.__new__(ImageTaskQueue)  # 免初始化（只测该方法）
    vq = VideoTaskQueue.__new__(VideoTaskQueue)
    monkeypatch.setattr(
        "src.services.video_queue.get_video_queue", lambda: vq)
    vq._lock_held = True
    assert q._video_local_busy() is True
    vq._lock_held = False
    assert q._video_local_busy() is False


def test_thermal_paused_includes_video_busy(monkeypatch) -> None:
    """_thermal_paused 组合判定：热保护 OR video 本地道占用。"""
    q = ImageTaskQueue.__new__(ImageTaskQueue)
    vq = VideoTaskQueue.__new__(VideoTaskQueue)
    monkeypatch.setattr(
        "src.services.video_queue.get_video_queue", lambda: vq)

    # get_thermal_guard 是函数内延迟导入——patch 源模块
    import src.services.thermal_guard as tg
    monkeypatch.setattr(
        tg, "get_thermal_guard",
        lambda: type("G", (), {"is_paused": staticmethod(lambda: False)})())

    vq._lock_held = True
    assert q._thermal_paused() is True, "video 本地占用应暂停 paint 准入"
    vq._lock_held = False
    assert q._thermal_paused() is False
