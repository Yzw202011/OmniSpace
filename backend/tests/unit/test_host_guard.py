"""回环绑定闸门单元测试（TASK-P0-05，规格 §14 约束2）。

直测 backend.config.assert_loopback_host 纯函数：
非回环 host 未豁免必须抛 RuntimeError；OMNISPACE_ALLOW_LAN=1 豁免放行。
闸门在 config.py 导入期生效，早于 uvicorn socket 绑定。
"""
from __future__ import annotations

import pytest

from backend.config import assert_loopback_host


@pytest.mark.smoke
def test_loopback_hosts_pass():
    assert_loopback_host("127.0.0.1")
    assert_loopback_host("localhost")
    assert_loopback_host("::1")


@pytest.mark.smoke
def test_wildcard_rejected_with_readable_message():
    with pytest.raises(RuntimeError, match="非回环地址"):
        assert_loopback_host("0.0.0.0", allow_lan=None)


@pytest.mark.smoke
def test_lan_ip_rejected():
    with pytest.raises(RuntimeError):
        assert_loopback_host("192.168.1.5", allow_lan="0")


@pytest.mark.smoke
def test_allow_lan_exemption_passes():
    """OMNISPACE_ALLOW_LAN=1 显式豁免：放行但调用方自担风险。"""
    assert_loopback_host("0.0.0.0", allow_lan="1")


@pytest.mark.smoke
def test_config_import_gate_active():
    """闸门在导入期已对真实 config.yaml 的 HOST 执行过（当前 127.0.0.1）。
    间接证据：backend.config 成功导入且 HOST 为回环值。"""
    import backend.config as cfg
    assert cfg.HOST in ("127.0.0.1", "localhost", "::1")
