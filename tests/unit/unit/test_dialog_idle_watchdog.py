"""对话引擎空闲看门狗哨兵（2026-09-05 方案A，用户拍板「先 A 后评 B」）。

契约（源码断言模式，同 test_dialog_degrade_fallback——dialog_engine
模块导入重，测试读源码锁定结构不被未来改动静默删除）：
  - 活动戳：chat_stream 入口 + ensure_loaded 入口必须刷新
    _last_activity_ts（对话 API 每次调用先过 ensure_loaded，天然成为
    活动信号；chat_stream_ex/chat 均经 chat_stream 漏斗，单点覆盖）；
  - 判定纯函数 _idle_unload_due 四条件：阈值>0 / 引擎就绪 /
    系统完全空闲（无任何功能持锁，含进行中对话请求）/ 时长达标；
  - 触发必须走正规卸载链 unload_model（绝不直接杀 vLLM 进程）；
  - 卸载写大白话事件 idle_unload（告知用户「模型怎么没了+怎么回来」）；
  - 阈值唯一来源 config.scheduler.dialog_idle_unload_seconds（0=禁用），
    config.py 侧常量 DIALOG_IDLE_UNLOAD_SECONDS 不得脱钩。
"""
from __future__ import annotations

from pathlib import Path

ENGINE_PY = (Path(__file__).resolve().parents[2]
             / "services" / "inference" / "dialog_engine.py")
SRC = ENGINE_PY.read_text(encoding="utf-8")
CONFIG_PY = (Path(__file__).resolve().parents[2] / "config.py").read_text(
    encoding="utf-8")


def test_activity_stamps_wired() -> None:
    """对话入口与加载入口必须刷新活动戳（看门狗的时钟源）。"""
    assert SRC.count("self._last_activity_ts = time.monotonic()") >= 2, (
        "chat_stream/ensure_loaded 活动戳被删")
    assert 'self._last_activity_ts: float = time.monotonic()' in SRC, (
        "看门狗时钟初始化被删")


def test_decision_pure_function_contract() -> None:
    """判定纯函数四条件缺一不可（阈值/就绪/全空闲/时长达标）。"""
    assert "def _idle_unload_due(" in SRC, "判定纯函数被删"
    assert "if threshold_seconds <= 0:" in SRC, "0=禁用语义被删"
    assert 'if state != "ready":' in SRC, "就绪态守卫被删"
    assert "if active_feature is not None:" in SRC, "持锁守卫被删（进行中任务绝不卸）"
    assert "return idle_seconds >= threshold_seconds" in SRC, "时长达标判定被删"


def test_uses_regular_unload_chain_and_plain_event() -> None:
    """触发必须走持锁二次确认的正规卸载链，且写大白话事件（绝不直接杀进程）。"""
    assert "with self._lock:" in SRC, "看门狗持锁二次确认被删（竞态防线）"
    assert "had = self._unload_locked()" in SRC, "持锁卸载调用被删"
    assert '"dialog", "idle_unload"' in SRC, "idle_unload 大白话事件被删"
    assert "下次对话将自动重新加载" in SRC, "出路指引被删（诚实事件必须带出路）"


def test_send_preflight_queues_instead_of_dropping() -> None:
    """dialog.py 生成预检：装载未完成时排队等待（≤5 分钟）而非快速失败
    弄丢消息（2026-09-05 [生成失败]对话模型未就绪 事故根修）。"""
    api_src = (Path(__file__).resolve().parents[2]
               / "api" / "dialog.py").read_text(encoding="utf-8")
    assert "排队等装载完自动续跑" in api_src, "等待语义注释被删"
    assert "deadline = time.monotonic() + 300.0" in api_src, "有界等待被删"
    assert 'st["state"] in ("error", "unavailable")' in api_src, "失败态判定被删"
    assert "await asyncio.sleep(2)" in api_src, "轮询让出事件循环被删"


def test_false_ready_liveness_gate() -> None:
    """假 ready 防线：vLLM 后端 ready 必须过子进程健康体检，失联即
    降级重载（2026-09-05 孤儿进程事故：假 ready 放行生成撞死服务）。"""
    assert "假 ready 防线" in SRC, "假 ready 防线注释被删"
    assert "if not get_vllm_service().is_healthy():" in SRC, "健康体检被删"
    assert 'self._state = "unloaded"' in SRC, "失联降级被删"


def test_watchdog_thread_and_threshold_source() -> None:
    """巡检线程在位；阈值唯一来源 config.scheduler（0=禁用）。"""
    assert "target=self._idle_watchdog_loop" in SRC, "看门狗线程被删"
    assert "DIALOG_IDLE_UNLOAD_SECONDS" in SRC, "引擎未接阈值常量"
    assert 'get("dialog_idle_unload_seconds", 900)' in CONFIG_PY, (
        "config.py 阈值读取被删（默认 900s）")


def test_feature_lock_snapshot_guard() -> None:
    """持锁快照守卫在位：对话/绘画/视频/训练任一持锁时不得回收。"""
    assert "FeatureLockManager.instance().active_feature" in SRC, (
        "功能锁快照被删")
    assert "except Exception:  # noqa: BLE001 - 锁不可用时按系统空闲处理" in SRC, (
        "锁不可用降级语义被改")
# 本项目仅供学习使用，商业授权请+Q 3559331368
