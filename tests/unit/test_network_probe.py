"""四连崩专项件②单测（2026-09-20）：网络探测线程硬隔离与告警退避。

覆盖：①裸 IP 双探针成败；②上一探针悬挂时不叠新线程、沿用缓存结论；
③失败连击计数与恢复复位（退避的数据面）。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.services import learning_scheduler as ls


def _bare_scheduler():
    s = ls.LearningScheduler.__new__(ls.LearningScheduler)
    s._network_cache = (True, 0.0)
    s._network_cache_ttl = 10.0
    return s


def test_probe_online_double_ip(monkeypatch):
    """两个探针 IP 任一可达即在线；全不可达离线。"""
    calls: list[str] = []

    def _fake_connect(addr, timeout=None):
        calls.append(addr[0])
        if addr[0] == "223.5.5.5":
            raise OSError("unreachable")
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(ls.socket, "create_connection", _fake_connect)
    assert ls.LearningScheduler._probe_online() is True
    assert calls == ["223.5.5.5", "114.114.114.114"]

    def _all_fail(addr, timeout=None):
        raise OSError("unreachable")

    monkeypatch.setattr(ls.socket, "create_connection", _all_fail)
    assert ls.LearningScheduler._probe_online() is False


def test_hung_probe_does_not_stack_threads():
    """上一探针线程仍活着 → 沿用缓存结论且不替换探针句柄。"""
    s = _bare_scheduler()
    fake = SimpleNamespace(is_alive=lambda: True)
    s._probe_thread = fake
    assert s.check_network(use_cache=False) is True  # 缓存值
    assert s._probe_thread is fake  # 未叠新线程


def test_fail_streak_and_recovery(monkeypatch):
    """离线连击计数递增；恢复后清零并回写缓存。"""
    s = _bare_scheduler()
    monkeypatch.setattr(s, "_probe_online", lambda: False)
    assert s.check_network(use_cache=False) is False
    assert s._net_fail_streak == 1
    assert s.check_network(use_cache=False) is False
    assert s._net_fail_streak == 2
    monkeypatch.setattr(s, "_probe_online", lambda: True)
    assert s.check_network(use_cache=False) is True
    assert s._net_fail_streak == 0
    assert s._network_cache[0] is True


def test_probe_thread_completes_fast():
    """正常路径：探针线程即刻完成，结论为真（不被 join 超时误杀）。"""
    s = _bare_scheduler()
    s._probe_online = staticmethod(lambda: True)  # noqa: B010
    assert s.check_network(use_cache=False) is True


def test_dns_free_probe_surface():
    """契约锁定：探测方法不得再引入域名解析（getaddrinfo 不受超时保护）。"""
    import inspect
    src = (inspect.getsource(ls.LearningScheduler.check_network)
           + inspect.getsource(ls.LearningScheduler._probe_online))
    assert "urlopen" not in src, "网络探测不得回退 HTTP/域名探测"
    assert "baidu" not in src, "网络探测不得使用域名目标"
