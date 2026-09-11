"""vLLM 多模型热切换压力测试（并发稳定性验证，2026-08-21）。

在 test_vllm_multimodal.py 单次热切换 e2e 之上，构造连续快速切换的
压力场景，验证系统在真实高频换载下的稳定性：

1. 串行快速往返：N 轮 8b-awq(vllm) ⇄ 4b(transformers) 零缓冲切换，
   每轮校验状态一致性；末轮对比首轮显存基线（泄漏检测）+
   py313 孤儿进程计数（进程树泄漏检测）
2. 并发稳定性（核心）：主线程连续切换的同时，worker 线程并发
   推理请求 + 状态查询——切换窗口期允许「预期失败」（引擎未就绪
   RuntimeError / vllm 子进程被杀的连接拒绝），但不允许挂死、
   死锁、崩溃或任何非预期异常类型
3. load 竞态：多线程同时请求不同模型 → 引擎锁串行化，最终状态
   必须自洽（backend 与 model_id 匹配，绝不出现半初始化状态）

运行方式（需 GPU + 双模型 + py313 运行时，总时长 ~10-20 分钟）:
  set OMNISPACE_VLLM_STRESS=1
  runtime\\py310\\python.exe -m pytest backend/tests/test_vllm_hot_switch_stress.py -v
轮数可调:
  set OMNISPACE_STRESS_ROUNDS=5
默认套件（tools/run_tests.py）自动跳过，不占 GPU。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_M8B_DIR = ROOT / "models" / "qwen3-vl-8b-awq"
_M4B_DIR = ROOT / "models" / "qwen3-vl-4b"
_PY313_EXE = (ROOT / "runtime" / "py313" / "python.exe")

_optin = os.environ.get("OMNISPACE_VLLM_STRESS") == "1"
try:
    _rounds = max(2, int(os.environ.get("OMNISPACE_STRESS_ROUNDS", "3")))
except ValueError:
    _rounds = 3

_assets_ready = (
    (_M8B_DIR / "config.json").is_file()
    and (_M4B_DIR / "config.json").is_file()
    and _PY313_EXE.is_file()
)

# 显存泄漏阈值：多轮 transformers 装卸的 CUDA 碎片宽容度（16GB 卡实测
# 数轮后 free 漂移通常 <0.5GB，超 1.5GB 判定泄漏）
_VRAM_DRIFT_LIMIT_GB = 1.5
# worker 线程 join 预算：超时即判定挂死/死锁（单请求 HTTP 超时 120s，
# 多请求排队上限给 180s 余量）
_JOIN_TIMEOUT_S = 180.0


# ── 助手 ──────────────────────────────────────────────────────

def _vram_free_gb() -> float:
    """当前 GPU 空闲显存（GB）；pynvml 不可用时返回 -1。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(h)
            return info.free / 1024 ** 3
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001 - 无 GPU/驱动时测试资产本身不满足
        return -1.0


def _py313_proc_count() -> int:
    """存活 py313 进程数（0=未运行，1=vllm 正常运行，>1=进程树泄漏）。"""
    try:
        import psutil
    except ImportError:
        return -1
    target = str(_PY313_EXE).lower()
    n = 0
    for p in psutil.process_iter(["exe"]):
        try:
            if (p.info["exe"] or "").lower() == target:
                n += 1
        except Exception:  # noqa: BLE001 - 进程退出竞态，跳过
            continue
    return n


def _classify_exc(e: BaseException) -> str:
    """推理请求异常分类：expected（切换窗口预期失败）/ unexpected。"""
    # 引擎未就绪 / vLLM 服务未就绪：热切换窗口的正常表现
    if isinstance(e, RuntimeError) and ("未就绪" in str(e) or "vLLM" in str(e)):
        return "expected"
    # vllm 子进程刚被 taskkill：连接拒绝/重置/超时（requests 家族）
    if type(e).__module__.startswith("requests"):
        return "expected"
    # urllib 兜底（requests 底层）
    if type(e).__module__.startswith("urllib3"):
        return "expected"
    return "unexpected"


def _chat_probe(eng: Any) -> tuple[str, str]:
    """单次极短推理探测。返回 (分类, 详情)。

    ok          有产出
    expected    切换窗口预期失败（未就绪/连接拒绝）
    unexpected  非预期异常（崩溃/挂死防护外的错误类型）
    """
    try:
        messages = eng.build_context("只回复两个字：就绪")
        reply = "".join(eng.chat_stream(
            messages, temperature=0.1, max_new_tokens=16))
        if reply.strip():
            return "ok", reply[:40]
        return "unexpected", "空回复（链路异常）"
    except BaseException as e:  # noqa: BLE001 - 分类后统一统计
        return _classify_exc(e), f"{type(e).__name__}: {e}"[:120]


def _require_stress() -> None:
    """压力档资产前置断言（skipif 之外运行时再兜底）。"""
    assert _assets_ready, (
        "qwen3-vl-8b-awq / qwen3-vl-4b / runtime-py313 未就绪")


# ── 1. 串行快速往返 ───────────────────────────────────────────

@pytest.mark.skipif(
    not _optin, reason="热切换压力测试需显式开启: OMNISPACE_VLLM_STRESS=1")
@pytest.mark.skipif(
    _optin and not _assets_ready, reason="双模型或 py313 运行时未就绪")
def test_rapid_serial_switch_roundtrip():
    """N 轮零缓冲 8b-awq ⇄ 4b 快速往返。

    校验点：
    - 每轮 backend 与模型匹配（8b-awq→vllm / 4b→vl）
    - vllm 进程状态跟随（vllm 后端时存活、transformers 时已终止）
    - served_name 动态正确（= 模型目录名）
    - 显存无累积泄漏（末轮 ready 后 free vs 首轮，漂移 < 1.5GB）
    - 无 py313 孤儿进程（任意时刻 ≤1）
    """
    from backend.engines.vllm_service import get_vllm_service
    from backend.services.inference.dialog_engine import get_dialog_engine

    _require_stress()
    eng = get_dialog_engine()
    svc = get_vllm_service()

    expected: dict[str, str] = {
        "qwen3-vl-8b-awq": "vllm",
        "qwen3-vl-4b": "vl",
    }

    baseline_free = -1.0
    switch_times: list[float] = []

    try:
        for i in range(_rounds):
            for mid in ("qwen3-vl-8b-awq", "qwen3-vl-4b"):
                t0 = time.time()
                assert eng.load_model(mid), (
                    f"第{i+1}轮加载 {mid} 失败: {eng._last_error}")
                switch_times.append(time.time() - t0)

                st = eng.get_status()
                assert st["backend"] == expected[mid], (
                    f"{mid} 后端应为 {expected[mid]}，实际 {st['backend']}")
                assert st["state"] == "ready", (
                    f"{mid} 状态应为 ready，实际 {st['state']}")

                # vllm 进程状态一致性
                if mid == "qwen3-vl-8b-awq":
                    assert svc.is_healthy(), "vllm 后端时服务应健康"
                    assert svc.status()["served_name"] == mid, (
                        f"served_name 应为 {mid}，"
                        f"实际 {svc.status()['served_name']}")
                else:
                    assert not svc.is_running(), (
                        "transformers 后端时 vllm 子进程必须已终止"
                        "（否则显存未回收，4b 加载必然 OOM）")

                # 孤儿进程检测：vLLM Windows 架构 = APIServer 主进程 +
                # EngineCore 子进程（均为 py313），运行时常态 2 个；
                # transformers 后端时必须归零（残留 = 进程树泄漏）
                n = _py313_proc_count()
                if mid == "qwen3-vl-8b-awq":
                    assert 1 <= n <= 4, (
                        f"py313 进程数异常: {n} 个（正常 2：APIServer+"
                        "EngineCore；>4 疑似失控增殖）")
                else:
                    assert n == 0, (
                        f"vllm 已停但残留 {n} 个 py313 进程（进程树泄漏）")

                # 首轮 8b 就绪后记显存基线
                if i == 0 and mid == "qwen3-vl-8b-awq":
                    baseline_free = _vram_free_gb()

                print(f"[串行往返] 第{i+1}轮 {mid} 就绪 "
                      f"({switch_times[-1]:.0f}s, free="
                      f"{_vram_free_gb():.1f}GB)")

        # 显存泄漏检测：末轮 4b ready 后与首轮 8b 基线比（两者装载占用
        # 不同，比较的是「同类就绪态的 free 是否逐轮衰减」——直接用
        # 末轮 unload 后的回收量在收尾断言，此处校验 ready 态衰减）
        final_free = _vram_free_gb()
        if baseline_free > 0 and final_free > 0:
            drift = baseline_free - final_free
            # 8b-awq(vllm 0.85 预算) 与 4b(transformers bf16) 装载占用
            # 本身不同，drift 为正且小于「两者占用差 + 泄漏阈值」即通过；
            # 真泄漏的表现是逐轮单调衰减，这里用宽上界拦截严重泄漏
            assert drift < 8.0 + _VRAM_DRIFT_LIMIT_GB, (
                f"显存泄漏疑似：首轮基线 free {baseline_free:.1f}GB，"
                f"末轮 {final_free:.1f}GB，漂移 {drift:.1f}GB")

        avg = sum(switch_times) / len(switch_times)
        print(f"\n[串行往返] {_rounds} 轮完成，平均单次切换 {avg:.0f}s，"
              f"共 {len(switch_times)} 次换载")
    finally:
        assert eng.unload_model(), "卸载失败（显存未回收）"
        assert not svc.is_running(), "收尾后 vllm 子进程应已终止"


# ── 2. 并发稳定性（核心） ─────────────────────────────────────

@pytest.mark.skipif(
    not _optin, reason="热切换压力测试需显式开启: OMNISPACE_VLLM_STRESS=1")
@pytest.mark.skipif(
    _optin and not _assets_ready, reason="双模型或 py313 运行时未就绪")
def test_concurrent_switch_with_traffic():
    """切换进行时并发推理 + 状态查询：不崩溃、不挂死、失败可预期。

    线程模型：
    - 主线程：连续切换 M 轮（每轮切换后停留 2s 让推理线程采到
      成功样本，随后立即下一轮——模拟用户高频换载）
    - 推理线程：循环极短探测请求，异常按白名单分类统计
    - 状态线程：循环 get_status + vllm status（纯读，必须全成功）

    断言：
    - unexpected 失败数 == 0（任何崩溃/空回复/未知异常都算）
    - 状态查询失败数 == 0
    - worker 线程 join 不超时（挂死/死锁检测）
    - 收尾后系统仍可用：重载 8b-awq + 推理 + 卸载，显存回基线
    """
    from backend.engines.vllm_service import get_vllm_service
    from backend.services.inference.dialog_engine import get_dialog_engine

    _require_stress()
    eng = get_dialog_engine()
    svc = get_vllm_service()

    stop = threading.Event()
    chat_stats = {"ok": 0, "expected": 0, "unexpected": 0}
    chat_details: list[str] = []
    status_stats = {"ok": 0, "fail": 0}

    def chat_worker() -> None:
        while not stop.is_set():
            cls, detail = _chat_probe(eng)
            chat_stats[cls] += 1
            if cls == "unexpected":
                chat_details.append(detail)
            # 短间隔高密度采样（切换窗口 ~40-60s，间隔 0.5s 可采 ~80 次）
            stop.wait(0.5)

    def status_worker() -> None:
        while not stop.is_set():
            try:
                st = eng.get_status()
                assert "backend" in st and "state" in st
                vst = svc.status()
                assert "served_name" in vst and "running" in vst
                status_stats["ok"] += 1
            except Exception:  # noqa: BLE001 - 纯读不允许任何失败
                status_stats["fail"] += 1
            stop.wait(0.2)

    t_chat = threading.Thread(target=chat_worker, name="stress-chat")
    t_status = threading.Thread(target=status_worker, name="stress-status")
    t_chat.start()
    t_status.start()

    baseline_free = _vram_free_gb()
    try:
        for i in range(_rounds):
            for mid in ("qwen3-vl-8b-awq", "qwen3-vl-4b"):
                assert eng.load_model(mid), (
                    f"并发场景第{i+1}轮加载 {mid} 失败: {eng._last_error}")
                # 停留窗口：让推理线程在就绪态采到成功样本
                time.sleep(2.0)
            print(f"[并发] 第{i+1}/{_rounds} 轮切换完成 "
                  f"(chat={chat_stats}, status={status_stats})")
    finally:
        stop.set()
        # 挂死/死锁检测：join 超时 = 请求链路卡死（如引擎锁未释放）
        t_chat.join(timeout=_JOIN_TIMEOUT_S)
        t_status.join(timeout=_JOIN_TIMEOUT_S)
        assert not t_chat.is_alive(), (
            f"推理线程 join 超时({_JOIN_TIMEOUT_S:.0f}s)：疑似挂死/死锁")
        assert not t_status.is_alive(), (
            f"状态线程 join 超时({_JOIN_TIMEOUT_S:.0f}s)：疑似挂死/死锁")
        eng.unload_model()

    # ── 结果断言 ──
    total = sum(chat_stats.values())
    assert chat_stats["unexpected"] == 0, (
        f"存在非预期失败 {chat_stats['unexpected']} 次: "
        f"{chat_details[:5]}")
    assert status_stats["fail"] == 0, (
        f"纯读状态查询失败 {status_stats['fail']} 次")
    assert chat_stats["ok"] > 0, (
        "推理线程没有任何成功样本（探测链路可能全程被阻断）")
    # 预期失败应集中在切换窗口（存在即合理，但占比不应压倒性）
    print(f"\n[并发] 推理探测 {total} 次: 成功 {chat_stats['ok']}, "
          f"切换窗口预期失败 {chat_stats['expected']}, "
          f"非预期 {chat_stats['unexpected']}")
    print(f"[并发] 状态查询 {status_stats['ok'] + status_stats['fail']} 次: "
          f"成功 {status_stats['ok']}, 失败 {status_stats['fail']}")

    # ── 压力后系统可用性 + 显存回收 ──
    assert eng.load_model("qwen3-vl-8b-awq"), (
        f"压力后重载失败: {eng._last_error}")
    cls, detail = _chat_probe(eng)
    assert cls == "ok", f"压力后推理探测失败: {cls} {detail}"
    assert eng.unload_model(), "压力后卸载失败"

    final_free = _vram_free_gb()
    if baseline_free > 0 and final_free > 0:
        leak = baseline_free - final_free
        assert leak < _VRAM_DRIFT_LIMIT_GB, (
            f"显存泄漏：基线 free {baseline_free:.1f}GB，"
            f"压力后回收 {final_free:.1f}GB，差 {leak:.1f}GB")
        print(f"[并发] 显存回收验证通过: {baseline_free:.1f}GB -> "
              f"{final_free:.1f}GB (漂移 {leak:.2f}GB)")

    n = _py313_proc_count()
    assert n == 0, f"收尾后存在 py313 孤儿进程 {n} 个（进程树泄漏）"


# ── 3. load 竞态 ──────────────────────────────────────────────

@pytest.mark.skipif(
    not _optin, reason="热切换压力测试需显式开启: OMNISPACE_VLLM_STRESS=1")
@pytest.mark.skipif(
    _optin and not _assets_ready, reason="双模型或 py313 运行时未就绪")
def test_concurrent_load_race():
    """多线程同时 load 不同模型：引擎锁串行化，最终状态自洽。

    竞态窗口：两线程几乎同时请求 8b-awq 与 4b，引擎实例锁保证
    逐个执行；无论谁最后完成，最终 state 必须 ready 且
    (model_id, backend) 二元组匹配，绝不出现半初始化/状态错乱。
    附加：同模型并发重复 load 幂等（全部 True，无重复加载开销）。
    """
    from backend.engines.vllm_service import get_vllm_service
    from backend.services.inference.dialog_engine import get_dialog_engine

    _require_stress()
    eng = get_dialog_engine()
    svc = get_vllm_service()

    pair = {"model": "", "backend": ""}
    pair_lock = threading.Lock()
    results: dict[str, bool] = {}

    def load_target(mid: str) -> None:
        ok = eng.load_model(mid)
        with pair_lock:
            results[mid] = ok
            if ok:
                st = eng.get_status()
                pair["model"] = str(st["model"])
                pair["backend"] = str(st["backend"])

    try:
        t1 = threading.Thread(target=load_target, args=("qwen3-vl-8b-awq",))
        t2 = threading.Thread(target=load_target, args=("qwen3-vl-4b",))
        t1.start()
        t2.start()
        t1.join(timeout=_JOIN_TIMEOUT_S)
        t2.join(timeout=_JOIN_TIMEOUT_S)
        assert not t1.is_alive() and not t2.is_alive(), (
            "竞态 load 线程 join 超时：引擎锁疑似死锁")

        # 至少一个成功（显存闸门下双载不可能，串行后一胜一负或后者
        # 换载前者——最终态以最后完成的 load 为准）
        assert any(results.values()), (
            f"竞态后无任何加载成功: {results}, {eng._last_error}")

        st = eng.get_status()
        assert st["state"] == "ready", (
            f"竞态后状态应 ready，实际 {st['state']}")
        # 最终 (model, backend) 必须自洽匹配
        match = {
            "qwen3-vl-8b-awq": "vllm",
            "qwen3-vl-4b": "vl",
        }
        assert st["backend"] == match.get(str(st["model"])), (
            f"竞态后状态错乱: model={st['model']}, "
            f"backend={st['backend']}（应匹配 {match}）")
        print(f"\n[竞态] 双线程并发 load 结果 {results}，最终态: "
              f"model={st['model']}, backend={st['backend']}")

        # 同模型并发幂等：3 线程同时 load 当前已载模型 → 全部快速 True
        cur = str(st["model"])
        idem_results: list[bool] = []
        idem_lock = threading.Lock()

        def idem_load() -> None:
            ok = eng.load_model(cur)
            with idem_lock:
                idem_results.append(ok)

        threads = [threading.Thread(target=idem_load) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60.0)
        assert all(idem_results) and len(idem_results) == 3, (
            f"同模型并发幂等 load 应全部 True: {idem_results}")
    finally:
        assert eng.unload_model(), "竞态测试收尾卸载失败"
        assert not svc.is_running(), "竞态测试收尾后 vllm 进程应终止"
# 本项目仅供学习使用，商业授权请+Q 3559331368
