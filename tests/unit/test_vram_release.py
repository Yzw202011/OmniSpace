"""一键释放显存编排服务单测（2026-10-26 强制版，用户拍板方案 v2）。

全 hermetic：依赖模块一律经 sys.modules 假模块注入（不触真 GPU、
不拉 comfy/浏览器依赖——GPU 测试活动门铁律）。锁定强制版核心语义：
1. 无条件中断——图像/视频队列排队+运行+云道任务全量取消，绘画/视频
   走端点函数全语义，非绘画产线回落队列级 cancel；
2. ComfyUI 活着就杀进程树（不再 /free 保护）；进程未运行则跳过不拉起；
3. vLLM 无条件终止（dialog 锁不再保护），终止后台账幂等同步；
4. 台账模型全量卸载（无类别保护）；功能锁强拆兜泄漏；
5. 训练任务取消 + 学习会话停止（保留语义）；单步失败不阻断其余步骤。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.services import vram_release


class _FakeApiError(Exception):
    """假 ApiError（对齐 error_handler.ApiError 的可捕获异常语义）。"""


def _install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> ModuleType:
    """向 sys.modules 注入假模块（函数内 from X import Y 走 sys.modules 快路径）。"""
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _patch_vram_readings(monkeypatch: pytest.MonkeyPatch, before: float, after: float) -> None:
    readings = iter([before, after])
    monkeypatch.setattr(
        vram_release, "_vram_used_gb", lambda: next(readings))


class _World:
    """公共 fake 装配器：空闲态全默认，各用例按需覆写。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mp = self.mp = monkeypatch
        # 队列（默认空）
        self.image_cancels: list[str] = []
        self.video_cancels: list[str] = []
        self._install_queue("src.services.image_queue", self.image_cancels)
        self._install_queue("src.services.video_queue", self.video_cancels)
        # ApiError + api 层取消端点函数
        _install(mp, "src.middleware.error_handler", ApiError=_FakeApiError)
        self.paint_endpoint_calls: list[str] = []
        self.video_endpoint_calls: list[str] = []

        def _paint_cancel(tid: str) -> dict:
            if tid.startswith("kf"):
                raise _FakeApiError("DRAW_TASK_NOT_FOUND")
            self.paint_endpoint_calls.append(tid)
            return {"task_id": tid, "status": "cancelled"}

        def _video_cancel(tid: str) -> dict:
            self.video_endpoint_calls.append(tid)
            return {"task_id": tid, "status": "cancelled"}

        _install(mp, "src.api.draw", paint_task_cancel=_paint_cancel)
        _install(mp, "src.api.manga.video", video_cancel=_video_cancel)
        # 学习会话（默认无）
        _install(mp, "src.services.browser_agent_service",
                 get_browser_agent_service=lambda: SimpleNamespace(
                     active_session=lambda: None,
                     stop_session=lambda sid: SimpleNamespace(session_id=sid)))
        # 训练任务（默认无）
        _install_training(self, [])
        # ComfyUI（默认未运行）
        self.comfy_shutdowns = 0
        self._comfy_alive = False
        self._install_comfy()
        # 模型台账（默认空）
        self.unloaded: list[str] = []
        self._models: list[dict] = []
        self._install_model_manager()
        # vLLM（默认未运行）
        self.vllm_stops: list[float] = []
        self._vllm_running = False
        self._vllm_served = ""
        self._install_vllm()
        # 功能锁（默认无持有）
        self.lock_cleared: list[str] = []
        _install(mp, "src.middleware.feature_lock",
                 get_feature_lock=lambda: SimpleNamespace(
                     force_release_all=lambda: self.lock_cleared))
        # 内存释放
        self.memory_released = 0
        _install(mp, "src.engines.memory_manager",
                 get_memory_manager=lambda: SimpleNamespace(
                     release=lambda: setattr(self, "memory_released",
                                             self.memory_released + 1)))

    # ---- 各假模块安装器 ----
    def _install_queue(self, name: str, cancels: list[str], *,
                       snapshot: dict | None = None) -> None:
        snap = snapshot or {"queued": [], "current": None,
                            "cloud_running": [], "lock_held": False}

        def _cancel(tid: str) -> str:
            cancels.append(tid)
            return "queued"

        getter = "get_image_queue" if "image" in name else "get_video_queue"
        if "image" in name:
            q = SimpleNamespace(snapshot=lambda: snap, cancel=_cancel)
        else:
            # 视频 queue：snapshot 的 queued 是数量契约，id 枚举走专方法
            ids = ([t["task_id"] for t in snap["queued"]]
                   + ([snap["current"]] if snap.get("current") else [])
                   + list(snap.get("cloud_running", [])))
            q = SimpleNamespace(snapshot=lambda: snap, cancel=_cancel,
                                active_task_ids=lambda ids=ids: list(ids))
        _install(self.mp, name, **{getter: lambda q=q: q})

    def _install_comfy(self) -> None:
        world = self

        class _Engine:
            def is_alive(self) -> bool:
                return world._comfy_alive

            def shutdown(self) -> None:
                world.comfy_shutdowns += 1

        _install(self.mp, "src.services.inference.comfy_paint_engine",
                 get_comfy_paint_engine=lambda: _Engine())

    def _install_model_manager(self) -> None:
        world = self

        def _get_model_manager() -> SimpleNamespace:
            return SimpleNamespace(
                get_loaded_models=lambda: list(world._models),
                unload_model=lambda mid: (world.unloaded.append(mid) or True),
                get_gpu_status=lambda device=None: {"vram_used_gb": 0.0},
            )

        _install(self.mp, "src.services.model_manager",
                 get_model_manager=_get_model_manager)

    def _install_vllm(self) -> None:
        world = self

        def _get_vllm_service() -> SimpleNamespace:
            return SimpleNamespace(
                is_running=lambda: world._vllm_running,
                is_booting=lambda: False,
                served_name=world._vllm_served,
                stop=lambda timeout_s: world.vllm_stops.append(timeout_s) or True,
            )

        _install(self.mp, "src.engines.vllm_service",
                 get_vllm_service=_get_vllm_service)


def _install_training(world: _World, rows: list[dict]) -> tuple[list, list]:
    updates: list[tuple[str, str]] = []
    cancel_reqs: list[str] = []

    class _FakeDb:
        def query(self, sql: str, params: tuple) -> list[dict]:
            assert "train_tasks" in sql
            return rows

        def update(self, table: str, data: dict, where: str,
                   params: tuple) -> None:
            updates.append((params[0], data["status"]))

    _enum = SimpleNamespace(
        QUEUED=SimpleNamespace(value="queued"),
        TRAINING=SimpleNamespace(value="training"),
        EVALUATING=SimpleNamespace(value="evaluating"),
        CANCELLED=SimpleNamespace(value="cancelled"))
    _install(world.mp, "src.data.database", get_db_safe=lambda: _FakeDb())
    _install(world.mp, "src.data.models", TrainStatus=_enum)
    _install(world.mp, "src.services.lora_training_service",
             get_lora_training_service=lambda: SimpleNamespace(
                 request_cancel=cancel_reqs.append))
    return updates, cancel_reqs


def _step(result: dict, step: str) -> dict:
    return next(a for a in result["actions"] if a["step"] == step)


# ════════════════ 队列取消 ════════════════

def test_image_queue_cancel_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队+运行+云道全量取消；绘画走端点函数，关键帧回落队列级。"""
    w = _World(monkeypatch)
    snap = {"queued": [{"task_id": "img1"}, {"task_id": "kf:abc:1"}],
            "current": "img2", "cloud_running": ["img9"], "lock_held": True}
    w._install_queue("src.services.image_queue", w.image_cancels, snapshot=snap)
    _patch_vram_readings(monkeypatch, 11.4, 1.0)

    result = vram_release.force_release_vram()

    # 绘画 id 走端点函数（含内存行/广播语义）；关键帧端点查无 → 队列级
    assert sorted(w.paint_endpoint_calls) == ["img1", "img2", "img9"]
    assert w.image_cancels == ["kf:abc:1"]
    step = _step(result, "cancel_tasks")
    assert step["status"] == "done"
    assert sorted(step["cancelled"]) == ["img1", "img2", "img9", "kf:abc:1"]


def test_video_queue_cancel_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """视频队列排队+运行任务全量取消（端点函数全语义）。"""
    w = _World(monkeypatch)
    snap = {"queued": [{"task_id": "v1"}], "current": "v2",
            "cloud_running": [], "lock_held": True}
    w._install_queue("src.services.video_queue", w.video_cancels, snapshot=snap)
    _patch_vram_readings(monkeypatch, 11.4, 1.0)

    result = vram_release.force_release_vram()

    assert sorted(w.video_endpoint_calls) == ["v1", "v2"]
    step = _step(result, "cancel_videos")
    assert step["status"] == "done"


def test_empty_queues_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """队列空 → skipped（不报错），动作链如实记账。"""
    _w = _World(monkeypatch)
    _patch_vram_readings(monkeypatch, 2.5, 2.5)

    result = vram_release.force_release_vram()

    assert _step(result, "cancel_tasks")["status"] == "skipped"
    assert _step(result, "cancel_videos")["status"] == "skipped"
    assert result["completed"] is True


# ════════════════ 无条件语义（保护翻转） ════════════════

def test_models_unloaded_even_when_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制版：台账模型全量卸载，不再有「功能锁持有类别」保护。"""
    w = _World(monkeypatch)
    w._models = [
        {"model_id": "m-paint", "category": "paint", "vram_gb": 9.0},
        {"model_id": "m-dialog", "category": "dialog", "vram_gb": 3.0},
    ]
    _patch_vram_readings(monkeypatch, 12.0, 0.0)

    result = vram_release.force_release_vram()

    assert sorted(w.unloaded) == ["m-dialog", "m-paint"]
    step = _step(result, "unload_models")
    assert step["status"] == "done"
    assert len(step["freed"]) == 2
    assert result["freed_gb"] == 12.0


def test_comfy_killed_when_generation_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制版：绘画/视频域在飞也杀 ComfyUI 进程树（不再保护跳过）。"""
    w = _World(monkeypatch)
    w._comfy_alive = True
    _patch_vram_readings(monkeypatch, 11.4, 1.0)

    result = vram_release.force_release_vram()

    assert w.comfy_shutdowns == 1
    step = _step(result, "comfy_stop")
    assert step["status"] == "done"
    assert "进程树已终止" in step["detail"]


def test_comfy_skipped_when_proc_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """ComfyUI 进程未运行 → 跳过且绝不为此拉起子进程。"""
    w = _World(monkeypatch)
    w._comfy_alive = False
    _patch_vram_readings(monkeypatch, 5.0, 5.0)

    result = vram_release.force_release_vram()

    step = _step(result, "comfy_stop")
    assert step["status"] == "skipped"
    assert w.comfy_shutdowns == 0
    assert "未运行" in step["detail"]


def test_vllm_stopped_even_when_dialog_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制版：dialog 锁持有中 vLLM 也终止（不再保护），台账同步。"""
    w = _World(monkeypatch)
    w._vllm_running = True
    w._vllm_served = "qwen35-9b-w4a16"
    w._models = [{"model_id": "qwen35-9b-w4a16", "category": "dialog",
                  "vram_gb": 12.0}]
    _patch_vram_readings(monkeypatch, 12.0, 0.0)

    result = vram_release.force_release_vram()

    assert w.vllm_stops == [5.0]
    step = _step(result, "stop_vllm")
    assert step["status"] == "done"
    assert "qwen35-9b-w4a16" in step["detail"]


def test_force_unlock_sweeps_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """功能锁强拆动作落账（清单来自 force_release_all）。"""
    w = _World(monkeypatch)
    w.lock_cleared = ["paint"]
    _patch_vram_readings(monkeypatch, 5.0, 5.0)

    result = vram_release.force_release_vram()

    step = _step(result, "force_unlock")
    assert step["status"] == "done"
    assert "paint" in step["detail"]


# ════════════════ 保留语义 ════════════════

def test_training_tasks_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队/训练中任务落库 cancelled + 置位运行时中断标志。"""
    w = _World(monkeypatch)
    updates, cancel_reqs = _install_training(
        w, [{"id": "t1", "status": "queued"},
            {"id": "t2", "status": "training"}])
    _patch_vram_readings(monkeypatch, 5.0, 5.0)

    result = vram_release.force_release_vram()

    assert sorted(updates) == [("t1", "cancelled"), ("t2", "cancelled")]
    assert sorted(cancel_reqs) == ["t1", "t2"]
    step = _step(result, "cancel_training")
    assert step["status"] == "done"


def test_learning_session_stop_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """活跃学习会话收到停止请求。"""
    _w = _World(monkeypatch)
    stopped: list[str] = []
    _install(monkeypatch, "src.services.browser_agent_service",
             get_browser_agent_service=lambda: SimpleNamespace(
                 active_session=lambda: SimpleNamespace(session_id="s1"),
                 stop_session=lambda sid: stopped.append(sid)
                 or SimpleNamespace(session_id=sid)))
    _patch_vram_readings(monkeypatch, 5.0, 5.0)

    result = vram_release.force_release_vram()

    assert stopped == ["s1"]
    step = _step(result, "learning_session")
    assert step["status"] == "done"


def test_step_failure_does_not_block_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """单步异常（队列编排炸）不阻断其余步骤，completed=False 如实降级。"""
    w = _World(monkeypatch)

    def _boom_snapshot() -> dict:
        raise RuntimeError("fake queue failure")

    _install(monkeypatch, "src.services.image_queue",
             get_image_queue=SimpleNamespace(
                 snapshot=_boom_snapshot, cancel=lambda tid: "queued"))
    _patch_vram_readings(monkeypatch, 5.0, 5.0)

    result = vram_release.force_release_vram()

    assert _step(result, "cancel_tasks")["status"] == "error"
    assert result["completed"] is False
    # 其余步骤照常走完（空队列 skipped + 空台账 done + 内存释放）
    assert _step(result, "unload_models")["status"] == "done"
    assert w.memory_released == 1


# ════════════════ 真类测试：feature_lock.force_release_all ════════════════

def test_feature_lock_force_release_all() -> None:
    """真 FeatureLockManager：强拆清全部域；此后 worker 收尾 release 为 no-op。"""
    from src.middleware.feature_lock import FeatureLockManager

    lock = FeatureLockManager()
    assert lock.acquire_sync("paint", task_id="t1") is True
    assert lock.acquire_sync("paint") is True          # 同功能重入 → 计数 2
    assert lock.acquire_sync("dialog", task_id="t2") is True
    assert set(lock.holders.values()) == {"paint", "dialog"}

    cleared = lock.force_release_all()

    assert cleared == ["dialog", "paint"]
    assert lock.holders == {}
    assert lock.status()["active_feature"] is None
    # 被强拆后收尾的 worker release → 持有者不匹配 → 静默 no-op
    lock.release_sync("paint")
    lock.release_sync("dialog")
    assert lock.holders == {}
