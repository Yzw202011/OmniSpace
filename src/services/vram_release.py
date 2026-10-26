"""一键释放显存编排服务（2026-10-26 强制版，用户拍板方案 v2）。

语义 = **无条件中断 + 全额释放**（不看任何「是否空闲/是否在飞」条件）：
1. 取消图像队列全部任务（排队出队；运行中置旗标经推理步检查点中断，
   ComfyUI 任务随进程终止止损）；
2. 取消视频队列全部任务（H3 链经 3s 采样轮询检查点退出并清理半成品）；
3. 停止活跃学习会话（Agent 下一拍退出并生成报告）；
4. 取消排队/训练/评估中的训练任务（语义对齐 /learn/tasks/{id}/cancel）；
5. 终止 ComfyUI 进程树（杀进程=连 CUDA context 数百 MB + torch 常驻
   GB 级 RAM 一并回收，进程保留热启动的 /free 在此不够彻底）；
6. 终止 vLLM 对话子进程（无条件，不再受 dialog 锁保护）；
7. 卸载台账全部模型（无类别保护）；
8. 强拆功能锁（force_release_all 兜泄漏）+ 释放闲置内存块。

行一致性：绘画/视频任务优先复用 api 层取消端点函数（内存行/DB 行 +
取消旗标 + WS 广播 + 追踪收尾全语义）；关键帧等非绘画产线端点查无
此任务时回落队列级 cancel——运行中任务由 runner 的 check_cancel
检查点收尾自己的行。强拆锁后 worker 收尾 release() 命中持有者不匹配
= 静默 no-op（feature_lock.release 同款守卫），不会误伤新锁。

HTTP 入口 POST /system/force-release-vram（src/api/system.py 薄壳）。
本模块全部同步阻塞——调用方必须经 services/offload.run_blocking 调入
（F-008：异步循环中禁止同步阻塞），端点侧已按此接线。
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("omnispace.services.vram_release")


def _vram_used_gb() -> float:
    """当前 GPU 已用显存（GB）；采集不可用时返回 0.0（账目如实降级）。"""
    try:
        from .model_manager import get_model_manager

        st = get_model_manager().get_gpu_status()
        return float(st.get("vram_used_gb", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        log.debug("vram_used_gb: 降级忽略", exc_info=True)
        return 0.0


def _queue_task_ids(snapshot: dict) -> list[str]:
    """队列快照 → 去重保序的任务 id 全集（排队+当前+云道在跑）。"""
    ids: list[str] = [str(t.get("task_id")) for t in snapshot.get("queued", [])]
    if snapshot.get("current"):
        ids.append(str(snapshot["current"]))
    ids += [str(k) for k in snapshot.get("cloud_running", [])]
    seen: set[str] = set()
    ordered: list[str] = []
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            ordered.append(tid)
    return ordered


def _cancel_image_queue_tasks(actions: list[dict[str, Any]]) -> None:
    """取消图像队列全部任务（绘画走端点函数全语义，其余回落队列级）。"""
    try:
        from ..middleware.error_handler import ApiError
        from .image_queue import get_image_queue

        q = get_image_queue()
        task_ids = _queue_task_ids(q.snapshot())
        if not task_ids:
            actions.append({"step": "cancel_tasks", "status": "skipped",
                            "detail": "图像队列为空"})
            return
        cancelled: list[str] = []
        for tid in task_ids:
            try:
                # 行一致性优先：绘画任务端点函数（内存行+旗标+广播+追踪收尾）
                from ..api.draw import paint_task_cancel

                paint_task_cancel(tid)
            except ApiError:
                # 非绘画产线（kf:/kfbatch: 等关键帧任务）或已终态——
                # 回落队列级取消（运行中任务由 runner 检查点收尾自己的行）
                try:
                    q.cancel(tid)
                except Exception:  # noqa: BLE001
                    log.debug("队列级取消跳过: %s", tid, exc_info=True)
            except Exception:  # noqa: BLE001 - 单任务失败不阻断批量
                log.warning("绘画任务取消异常: %s", tid, exc_info=True)
                try:
                    q.cancel(tid)
                except Exception:  # noqa: BLE001
                    pass
            cancelled.append(tid)
        actions.append({"step": "cancel_tasks", "status": "done",
                        "detail": f"图像队列已取消 {len(cancelled)} 个任务"
                                  "（排队出队；运行中于下一检查点中断）",
                        "cancelled": cancelled})
    except Exception as exc:  # noqa: BLE001
        log.warning("图像队列取消编排异常: %s", exc, exc_info=True)
        actions.append({"step": "cancel_tasks", "status": "error",
                        "detail": str(exc)})


def _cancel_video_queue_tasks(actions: list[dict[str, Any]]) -> None:
    """取消视频队列全部任务（复用端点函数：DB 行+队列旗标+内存旗标）。"""
    try:
        from ..middleware.error_handler import ApiError
        from .video_queue import get_video_queue

        q = get_video_queue()
        # 视频 snapshot 的 queued 是数量契约（整数），id 枚举走专方法
        task_ids = q.active_task_ids()
        if not task_ids:
            actions.append({"step": "cancel_videos", "status": "skipped",
                            "detail": "视频队列为空"})
            return
        cancelled: list[str] = []
        for tid in task_ids:
            try:
                from ..api.manga.video import video_cancel

                video_cancel(tid)
            except ApiError:
                try:
                    q.cancel(tid)
                except Exception:  # noqa: BLE001
                    log.debug("队列级取消跳过: %s", tid, exc_info=True)
            except Exception:  # noqa: BLE001 - 单任务失败不阻断批量
                log.warning("视频任务取消异常: %s", tid, exc_info=True)
                try:
                    q.cancel(tid)
                except Exception:  # noqa: BLE001
                    pass
            cancelled.append(tid)
        actions.append({"step": "cancel_videos", "status": "done",
                        "detail": f"视频队列已取消 {len(cancelled)} 个任务"
                                  "（H3 链经采样轮询检查点退出）",
                        "cancelled": cancelled})
    except Exception as exc:  # noqa: BLE001
        log.warning("视频队列取消编排异常: %s", exc, exc_info=True)
        actions.append({"step": "cancel_videos", "status": "error",
                        "detail": str(exc)})


def _stop_learning_session(actions: list[dict[str, Any]]) -> None:
    """活跃学习会话发停止请求（无会话则 skipped，不报错）。"""
    try:
        from .browser_agent_service import get_browser_agent_service

        session = get_browser_agent_service().active_session()
        if session is None:
            actions.append({"step": "learning_session", "status": "skipped",
                            "detail": "无活跃学习会话"})
            return
        stopped = get_browser_agent_service().stop_session(session.session_id)
        actions.append({"step": "learning_session", "status": "done",
                        "detail": f"停止请求已受理（session={stopped.session_id}，"
                                  "Agent 下一拍退出并生成报告）"})
    except Exception as exc:  # noqa: BLE001
        log.warning("学习会话停止异常: %s", exc, exc_info=True)
        actions.append({"step": "learning_session", "status": "error",
                        "detail": str(exc)})


def _cancel_running_training(actions: list[dict[str, Any]]) -> None:
    """取消排队/训练/评估中的训练任务（语义对齐 /learn/tasks/{id}/cancel）。"""
    try:
        from ..data.database import get_db_safe
        from ..data.models import TrainStatus

        db = get_db_safe()
        if db is None:
            actions.append({"step": "cancel_training", "status": "skipped",
                            "detail": "数据库不可用"})
            return
        cancellable = (
            TrainStatus.QUEUED.value,
            TrainStatus.TRAINING.value,
            TrainStatus.EVALUATING.value,
        )
        marks = ",".join("?" for _ in cancellable)
        rows = db.query(
            f"SELECT id, status FROM train_tasks WHERE status IN ({marks})",
            cancellable)
        if not rows:
            actions.append({"step": "cancel_training", "status": "skipped",
                            "detail": "无进行中训练任务"})
            return
        cancelled: list[str] = []
        for row in rows:
            task_id = str(row.get("id", ""))
            try:
                db.update("train_tasks",
                          {"status": TrainStatus.CANCELLED.value,
                           "updated_at": time.time()},
                          "id=?", (task_id,))
                # 置位运行时中断标志（训练循环下一 step 边界干净退出）
                from .lora_training_service import get_lora_training_service

                get_lora_training_service().request_cancel(task_id)
                cancelled.append(task_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("训练任务 %s 取消异常: %s", task_id, exc,
                            exc_info=True)
        actions.append({"step": "cancel_training", "status": "done",
                        "detail": f"已取消 {len(cancelled)} 个训练任务",
                        "cancelled": cancelled})
    except Exception as exc:  # noqa: BLE001
        log.warning("训练任务取消编排异常: %s", exc, exc_info=True)
        actions.append({"step": "cancel_training", "status": "error",
                        "detail": str(exc)})


def _stop_comfy(actions: list[dict[str, Any]]) -> None:
    """终止 ComfyUI 进程树（杀进程=连 CUDA context 一并回收，无条件）。"""
    try:
        from .inference.comfy_paint_engine import get_comfy_paint_engine

        engine = get_comfy_paint_engine()
        if not engine.is_alive():
            actions.append({"step": "comfy_stop", "status": "skipped",
                            "detail": "ComfyUI 进程未运行"})
            return
        # shutdown 幂等（委托 comfy_proc 统一管理器，杀进程树）
        engine.shutdown()
        actions.append({"step": "comfy_stop", "status": "done",
                        "detail": "ComfyUI 进程树已终止"
                                  "（CUDA context 与驻留权重全额回收）"})
    except Exception as exc:  # noqa: BLE001
        log.warning("ComfyUI 终止编排异常: %s", exc, exc_info=True)
        actions.append({"step": "comfy_stop", "status": "error",
                        "detail": str(exc)})


def _unload_ledger_models(actions: list[dict[str, Any]]) -> None:
    """卸载台账全部模型（强制版：无类别保护， unloaded 失败如实记账）。"""
    try:
        from .model_manager import get_model_manager

        mgr = get_model_manager()
        freed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for entry in mgr.get_loaded_models():
            model_id = str(entry.get("model_id", ""))
            cat = str(entry.get("category") or "").strip().lower()
            if mgr.unload_model(model_id):
                freed.append({"model_id": model_id, "category": cat,
                              "vram_gb": float(entry.get("vram_gb", 0.0) or 0.0)})
            else:
                failed.append({"model_id": model_id, "category": cat,
                               "reason": "台账失效或引擎回调未受理"})
        actions.append({
            "step": "unload_models", "status": "done",
            "detail": f"卸载 {len(freed)} 个模型，失败 {len(failed)} 个",
            "freed": freed, "skipped": failed,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("台账模型卸载编排异常: %s", exc, exc_info=True)
        actions.append({"step": "unload_models", "status": "error",
                        "detail": str(exc)})


def _stop_vllm(actions: list[dict[str, Any]]) -> None:
    """终止 vLLM 对话子进程（无条件；终止后台账幂等同步）。"""
    try:
        from ..engines.vllm_service import get_vllm_service

        svc = get_vllm_service()
        if not (svc.is_running() or svc.is_booting()):
            actions.append({"step": "stop_vllm", "status": "skipped",
                            "detail": "vLLM 子进程未在运行"})
            return
        served = svc.served_name
        stopped = svc.stop(timeout_s=5.0)
        # 子进程已死而台账仍挂 → 面板 stale；幂等同步
        if stopped and served:
            try:
                from .model_manager import get_model_manager

                get_model_manager().unload_model(served)
            except Exception:  # noqa: BLE001
                log.debug("vLLM 台账同步跳过", exc_info=True)
        actions.append({"step": "stop_vllm", "status": "done",
                        "detail": (f"vLLM 子进程已终止（{served or 'vllm-subprocess'}）"
                                   if stopped else "vLLM 子进程终止超时（详见日志）")})
    except Exception as exc:  # noqa: BLE001
        log.warning("vLLM 终止编排异常: %s", exc, exc_info=True)
        actions.append({"step": "stop_vllm", "status": "error",
                        "detail": str(exc)})


def _force_unlock(actions: list[dict[str, Any]]) -> None:
    """强拆全部功能锁（兜击杀后残留与历史泄漏；worker 收尾 no-op 安全）。"""
    try:
        from ..middleware.feature_lock import get_feature_lock

        cleared = get_feature_lock().force_release_all()
        actions.append({"step": "force_unlock", "status": "done",
                        "detail": (f"功能锁已强拆: {', '.join(cleared)}"
                                   if cleared else "无持有中的功能锁")})
    except Exception as exc:  # noqa: BLE001
        log.warning("功能锁强拆异常: %s", exc, exc_info=True)
        actions.append({"step": "force_unlock", "status": "error",
                        "detail": str(exc)})


def force_release_vram() -> dict[str, Any]:
    """编排一键释放显存（同步阻塞；调用方须经 run_blocking 调入）。

    顺序：取消图像队列 → 取消视频队列 → 停学习会话 → 取消训练 →
    终止 ComfyUI 进程树 → 卸载台账全部模型 → 终止 vLLM → 强拆功能锁 →
    内存侧释放。每步独立容错（失败不阻断后续），动作链如实返回。

    Returns:
        {vram_before_gb, vram_after_gb, freed_gb, completed, actions,
         duration_ms}
    """
    t0 = time.monotonic()
    actions: list[dict[str, Any]] = []
    before_gb = _vram_used_gb()

    _cancel_image_queue_tasks(actions)
    _cancel_video_queue_tasks(actions)
    _stop_learning_session(actions)
    _cancel_running_training(actions)
    _stop_comfy(actions)
    _unload_ledger_models(actions)
    _stop_vllm(actions)
    _force_unlock(actions)

    # 内存侧补充：释放闲置内存块（unload_model 已做进程级 gc + 归还物理页）
    try:
        from ..engines.memory_manager import get_memory_manager

        get_memory_manager().release()
        actions.append({"step": "memory_release", "status": "done",
                        "detail": "闲置内存块已释放"})
    except Exception as exc:  # noqa: BLE001
        log.debug("memory_manager 释放跳过: %s", exc)
        actions.append({"step": "memory_release", "status": "error",
                        "detail": str(exc)})

    after_gb = _vram_used_gb()
    completed = all(a.get("status") != "error" for a in actions)
    result: dict[str, Any] = {
        "vram_before_gb": round(before_gb, 2),
        "vram_after_gb": round(after_gb, 2),
        "freed_gb": round(max(0.0, before_gb - after_gb), 2),
        "completed": completed,
        "actions": actions,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    log.info("一键释放显存完成: %.2fGB→%.2fGB (freed=%.2fGB, %dms, completed=%s)",
             before_gb, after_gb, result["freed_gb"],
             result["duration_ms"], completed)
    return result
