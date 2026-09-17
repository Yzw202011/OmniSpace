"""插件安全盾测试（DF v0.9.2 SecurityMonitor 移植，2026-09-17 用户拍板）。

三层覆盖：
1. **单测弹盒**（移植自上游 test_security_monitor.py，适配咱测试风格）：
   规则判定三态/意图探针历史/熔断 fail-closed/行为指纹三检测/审计四元组；
2. **宿主适配层**（security_gate.py）：总闸开关/fail-open/参数裁剪；
3. **真接线**：PluginRuntime.invoke 与内核 think 两条调用链真过门禁——
   正常插件零行为变化 + 拦截路径错误信封（API 级 TestClient 取证）。

纯 CPU，不触 GPU。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import registry as pr_registry
from src.services.plugin_runtime import security as sec
from src.services.plugin_runtime import security_gate as sg

RUST_MOVE_CODE = 'fn f() { let s = String::from("x"); let t = s; let u = s; }'


# ── 夹具 ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _fresh_monitor():
    """每个测试独立监控面（指纹基线/审计环形不跨测试污染）。"""
    sg.reset_security_monitor()
    yield
    sg.reset_security_monitor()


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient（不进 with = 不触发 lifespan，对齐 test_plugin_runtime 模式）。"""
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    monkeypatch.setattr(pr_registry, "OUTPUT_ROOT", tmp_path / "plug_out")
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def fresh_runtime(tmp_path):
    """独立运行时实例（不污染单例；供闸门类直测）。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pr_registry, "OUTPUT_ROOT", tmp_path / "out")
    rt = pr_registry.PluginRuntime()
    yield rt
    monkey.undo()


# ═══ 1. 规则判定（RuleBasedSafetyCheck） ═════════════════════

def test_rule_check_verdicts():
    checker = sec.RuleBasedSafetyCheck()
    assert checker.check({"tool": "rust-coding"}).verdict is sec.Verdict.ALLOW
    v = checker.check({"tool": "sudo_rm"})
    assert v.verdict is sec.Verdict.DENY
    assert v.risk_tags == ["privilege-escalation"]
    v2 = checker.check({"tool": "db_drop_table"})
    assert v2.verdict is sec.Verdict.REVIEW
    assert v2.risk_tags == ["access-core-database"]


def test_rule_check_topic_fallback():
    """无 tool 字段时回落 topic（内核 think 通道形态）。"""
    checker = sec.RuleBasedSafetyCheck()
    assert checker.check({"topic": "port_scan_all"}).verdict is sec.Verdict.DENY
    assert checker.check({"topic": "rust"}).verdict is sec.Verdict.ALLOW


def test_rule_check_empty_and_unknown():
    checker = sec.RuleBasedSafetyCheck()
    assert checker.check({}).verdict is sec.Verdict.ALLOW
    assert checker.check({"tool": "never-seen-tool"}).verdict is sec.Verdict.ALLOW


def test_rule_check_custom_rules():
    checker = sec.RuleBasedSafetyCheck(
        high_risk={"mine": ("secret_op",)},
        escape={"mine-escape": ("format_c",)})
    assert checker.check({"tool": "do_secret_op"}).verdict is sec.Verdict.REVIEW
    assert checker.check({"tool": "format_c_disk"}).verdict is sec.Verdict.DENY
    # 自定义后初始规则不再生效
    assert checker.check({"tool": "sudo"}).verdict is sec.Verdict.ALLOW


def test_rule_check_escape_takes_precedence():
    """同时命中逃逸+高危时逃逸（DENY）优先。"""
    checker = sec.RuleBasedSafetyCheck(
        high_risk={"hr": ("sudo",)}, escape={"esc": ("sudo",)})
    assert checker.check({"tool": "sudo"}).verdict is sec.Verdict.DENY


# ═══ 2. 意图探针（IntentProbe） ══════════════════════════════

def test_probe_records_history_and_stats():
    probe = sec.IntentProbe()
    probe.inspect({"tool": "ok_tool"})
    probe.inspect({"tool": "sudo"})
    assert probe.total_inspected == 2 and probe.total_denied == 1
    assert [a.action["tool"] for a in probe.history] == ["ok_tool", "sudo"]


def test_probe_history_capacity():
    probe = sec.IntentProbe(history_capacity=3)
    for i in range(5):
        probe.inspect({"tool": f"t{i}"})
    assert [a.action["tool"] for a in probe.history] == ["t2", "t3", "t4"]


# ═══ 3. 熔断器（CriticalActionCircuitBreaker） ═══════════════

def test_breaker_fail_closed_without_reviewer():
    """无审核者 = fail-closed：REVIEW 一律拒绝放行。"""
    breaker = sec.CriticalActionCircuitBreaker()
    review = breaker.trip({"tool": "db_write"}, sec.SafetyVerdict(sec.Verdict.REVIEW))
    assert breaker.resolve(review) is False
    assert review.resolved is True and review.approved is False
    assert breaker.total_rejected == 1


def test_breaker_review_lifecycle():
    breaker = sec.CriticalActionCircuitBreaker(reviewer=lambda r: True)
    review = breaker.trip({"tool": "db_write"},
                          sec.SafetyVerdict(sec.Verdict.REVIEW, reasons=["高危"]))
    assert breaker.resolve(review) is True
    assert review.approved is True


def test_breaker_pending_capacity():
    breaker = sec.CriticalActionCircuitBreaker(pending_capacity=2)
    for i in range(4):
        breaker.trip({"tool": f"hr{i}"}, sec.SafetyVerdict(sec.Verdict.REVIEW))
    assert len(breaker.pending) == 2
    assert breaker.total_tripped == 4


# ═══ 4. 组合面（SecurityMonitor.gate） ═══════════════════════

def test_monitor_gate_allow_passthrough():
    m = sec.SecurityMonitor()
    ok, audited, review = m.gate({"tool": "rust-coding", "params": "code"})
    assert ok is True and review is None
    assert audited.verdict.verdict is sec.Verdict.ALLOW
    # 放行动作回填指纹窗口 + 审计留痕
    assert m.fingerprint.stats()["window_size"] == 1
    assert m.tracer.stats()["traces"] == 1


def test_monitor_gate_review_returns_pending():
    m = sec.SecurityMonitor()
    ok, _audited, review = m.gate({"tool": "raw_sql_query"})
    assert ok is False and review is not None
    assert review.verdict.verdict is sec.Verdict.REVIEW


def test_monitor_gate_deny():
    m = sec.SecurityMonitor()
    ok, _audited, review = m.gate({"tool": "run_as_admin"})
    assert ok is False and review is None


def test_monitor_gate_fingerprint_flags_recorded_not_blocking():
    """指纹异常只留痕不拦截（高频调用仍 ALLOW，但 tags 进审计）。"""
    m = sec.SecurityMonitor()
    for _ in range(8):
        m.gate({"tool": "rust-coding"})  # 窗内超 freq_limit=6
    ok, _a, _r = m.gate({"tool": "rust-coding"})
    assert ok is True
    tagged = m.tracer.query(tag="frequency-burst")
    assert tagged, "频率突发应留痕于审计 tags"


def test_monitor_stats_consistency():
    m = sec.SecurityMonitor()
    m.gate({"tool": "a"}); m.gate({"tool": "sudo"})  # noqa: E702
    stats = m.stats()
    assert stats["probe"]["inspected"] == 2
    assert stats["tracer"]["traces"] == 2
    assert stats["breaker"]["tripped"] == 0


# ═══ 5. 行为指纹（BehavioralFingerprint） ════════════════════

def test_fingerprint_clean_action_no_flags():
    fp = sec.BehavioralFingerprint()
    r = fp.check({"tool": "rust-coding", "params": "fn main() {}"})
    assert r.anomalous is False and r.flags == []


def test_fingerprint_frequency_burst():
    fp = sec.BehavioralFingerprint(freq_limit=3, window=10.0)
    for _ in range(4):
        fp.observe({"tool": "rust"})
    r = fp.check({"tool": "rust"})
    assert "frequency-burst" in r.flags


def test_fingerprint_baseline_deviation():
    """基线学习后，偏离倍数超限即标记（即使未到硬上限）。"""
    fp = sec.BehavioralFingerprint(freq_limit=None, window=10.0,
                                   deviation_factor=2.0)
    fp.learn([{"tool": "rust"}] * 2)  # 基线速率 0.2 次/窗
    for _ in range(2):
        fp.observe({"tool": "rust"})  # 窗内实测 2 > 0.2*2=0.4
    r = fp.check({"tool": "rust"})
    assert "frequency-burst" in r.flags


def test_fingerprint_high_entropy_params():
    fp = sec.BehavioralFingerprint(entropy_threshold=5.0)
    r = fp.check({"tool": "x", "params":
                  "qX7#mZ2$vK9pL4@nR8&wJ5bT3yH6cF1dG0sA7uE2iO9kQ4zM5xP8"})
    assert "high-entropy-params" in r.flags


def test_fingerprint_unrelated_low_level():
    fp = sec.BehavioralFingerprint(task_tools={"report": {"db_read"}})
    assert "unrelated-low-level" in fp.check(
        {"tool": "iptables"}, task="report").flags
    # 任务内允许清单中的底层工具不告警（无此场景=仅测保守分支）
    assert fp.check({"tool": "db_read"}, task="report").anomalous is False
    # 无任务上下文时保守告警
    assert "unrelated-low-level" in fp.check({"tool": "kill"}).flags


def test_fingerprint_reset():
    fp = sec.BehavioralFingerprint()
    fp.observe({"tool": "a"})
    fp.learn([{"tool": "b"}])
    fp.check({"tool": "a"})
    fp.reset()
    assert fp.stats() == {"checks": 0, "anomalies": 0,
                          "baseline_tools": 0, "window_size": 0}


# ═══ 6. 审计四元组（ActionTracer） ══════════════════════════

def test_tracer_record_replay_four_tuple():
    tracer = sec.ActionTracer()
    aid = tracer.record(
        thinking_state={"caller": "test"},
        decision_basis={"verdict": "deny", "risk_tags": ["privilege-escalation"]},
        tool_params={"tool": "sudo"},
        execution_result={"allowed": False, "outcome": "DENY"},
        tags=["x"])
    pic = tracer.replay(aid)
    assert pic["thinking_state"] == {"caller": "test"}
    assert pic["decision_basis"]["verdict"] == "deny"
    assert pic["tool_params"]["tool"] == "sudo"
    assert pic["execution_result"]["outcome"] == "DENY"
    assert tracer.replay("nonexistent") == {}


def test_tracer_query_dimensions():
    tracer = sec.ActionTracer(id_factory=lambda: "id", clock=lambda: 100.0)
    tracer.record(tool_params={"tool": "a"}, tags=["t1"],
                  execution_result={"outcome": "ALLOW"})
    tracer.record(tool_params={"tool": "sudo"},
                  decision_basis={"risk_tags": ["privilege-escalation"]},
                  execution_result={"outcome": "DENY"})
    assert len(tracer.query(tool="a")) == 1
    assert len(tracer.query(tag="t1")) == 1
    assert len(tracer.query(outcome="DENY")) == 1
    assert len(tracer.query(risk_tag="privilege-escalation")) == 1
    assert len(tracer.query(since=99.0, until=101.0)) == 2
    assert len(tracer.query(since=100.5)) == 0


def test_tracer_capacity_eviction_and_export():
    tracer = sec.ActionTracer(capacity=3)
    for i in range(5):
        tracer.record(tool_params={"tool": f"t{i}"})
    assert len(tracer.traces) == 3
    exported = tracer.export()
    assert [e["tool_params"]["tool"] for e in exported] == ["t2", "t3", "t4"]


# ═══ 7. 宿主适配层（security_gate） ══════════════════════════

def test_host_gate_allow_with_audit():
    ok, outcome, _ = sg.gate_plugin_call("rust-coding", {"code": RUST_MOVE_CODE})
    assert ok is True and outcome == "allow"
    m = sg.get_security_monitor()
    recs = m.tracer.query(tool="rust-coding")
    assert len(recs) == 1
    assert recs[0].execution_result["allowed"] is True


def test_host_gate_kernel_event_topic_as_tool():
    ok, outcome, _ = sg.gate_kernel_event({"topic": "rust", "data": {"code": "x"}})
    assert ok is True and outcome == "allow"
    # 内核动作载荷以 topic 为名（tracer.query 的 tool 维度不适用，按 outcome 查）
    recs = sg.get_security_monitor().tracer.query(outcome="ALLOW")
    assert recs and recs[-1].tool_params.get("topic") == "rust"


def test_host_gate_off_short_circuit(monkeypatch):
    monkeypatch.setattr(sg, "security_gate_enabled", lambda: False)
    ok, outcome, _ = sg.gate_plugin_call("sudo_anything", {})
    assert ok is True and outcome == "GATE_OFF"
    assert sg.get_security_monitor().tracer.stats()["traces"] == 0


def test_host_gate_fail_open_on_internal_error(monkeypatch):
    class _Broken:
        def gate(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(sg, "get_security_monitor", lambda: _Broken())
    ok, outcome, _ = sg.gate_plugin_call("rust-coding", {})
    assert ok is True and outcome == "GATE_ERROR"


def test_host_gate_params_clipped():
    sg.gate_plugin_call("rust-coding", {"code": "x" * 5000})
    rec = sg.get_security_monitor().tracer.query(tool="rust-coding")[0]
    params = rec.tool_params["params"]
    assert len(params) <= sg._PARAMS_CLIP + 30  # 截断标记尾巴余量
    assert params.endswith(">")


def test_host_gate_stats_shape():
    sg.gate_plugin_call("rust-coding", {})
    stats = sg.gate_stats()
    assert stats["active"] is True
    assert stats["probe"]["inspected"] == 1


# ═══ 8. 真接线（invoke / kernel think 两条链） ═══════════════

def test_invoke_real_plugin_audited(fresh_runtime):
    """真插件经 invoke 全链：正常调用成功 + 安全盾留痕（零行为变化）。"""
    result = asyncio.run(fresh_runtime.invoke("rust-coding",
                                              {"code": RUST_MOVE_CODE}))
    assert result["data"]["label"] in ("move", "borrow", "lifetime", "type", "ok")
    recs = sg.get_security_monitor().tracer.query(tool="rust-coding")
    assert len(recs) == 1
    assert recs[0].execution_result["outcome"] == "ALLOW"


def test_invoke_blocked_by_gate(fresh_runtime, monkeypatch):
    """门禁拒绝时 invoke 抛语义错误码（拦截优先于执行）。"""
    monkeypatch.setattr(pr_registry, "gate_plugin_call",
                        lambda name, spec: (False, "deny", "测试拦截"))
    with pytest.raises(pr_registry.PluginRuntimeError) as ei:
        asyncio.run(fresh_runtime.invoke("rust-coding", {"code": "x"}))
    assert ei.value.code == "PLUGIN_SECURITY_DENIED"


def test_api_invoke_real_and_blocked(api_client, monkeypatch):
    """API 级取证：正常 invoke 出分类 + 门禁拒绝走统一错误信封。"""
    r = api_client.post("/api/v1/plugins/rust-coding/invoke",
                        json={"data": {"code": RUST_MOVE_CODE}})
    body = r.json()
    assert body["success"] is True, body
    assert sg.get_security_monitor().tracer.query(tool="rust-coding")

    monkeypatch.setattr(pr_registry, "gate_plugin_call",
                        lambda name, spec: (False, "review", "测试熔断"))
    r2 = api_client.post("/api/v1/plugins/rust-coding/invoke",
                         json={"data": {"code": "x"}})
    body2 = r2.json()
    assert body2["success"] is False
    assert body2["error"]["code"] == "PLUGIN_SECURITY_REVIEW"


def test_kernel_think_gated_via_wrapper(api_client, monkeypatch):
    """内核 think 网关：正常放行出结果；拒绝时 API 信封出语义码。"""
    r = api_client.post("/api/v1/plugins/kernel/think",
                        json={"topic": "rust", "data": {"code": RUST_MOVE_CODE}})
    body = r.json()
    assert body["success"] is True, body
    assert body["data"]["routed"] is True

    monkeypatch.setattr(sg, "gate_kernel_event",
                        lambda event, task=None: (False, "deny", "测试拦截"))
    r2 = api_client.post("/api/v1/plugins/kernel/think",
                         json={"topic": "rust", "data": {"code": "x"}})
    body2 = r2.json()
    assert body2["success"] is False
    assert body2["error"]["code"] == "PLUGIN_SECURITY_DENIED"
