"""H7 对话断开黑洞真修钉子（2026-10-24 终结书 P0）。

R23 实锤 + 本轮深挖：WS 断开后回复丢失+GPU 空转。2026-10-22 旧修系
微秒级空操作——_stop_flags.add 后 finally 立即 discard（try/except/
finally 结构决定），生产者 stop_check 看不到旗标。真修三要点：
1. 置停后 await producer（等生产者真停）再清旗标
2. 断开后跳过被动补全（不再给走了的用户烧第二次推理）
3. 保存路径之后的全部 WS 发送带 _ws_alive 守卫（死 socket 异常
   不得越过保存）
源码哨兵钉住不回退（先例：test_dialog_idle_watchdog 源码扫描族）。
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2]
        / "src" / "api" / "dialog.py").read_text(encoding="utf-8")


def _handler_body() -> str:
    # 流式消费循环/断开处理/保存全在 _ws_handle_message（1955 起），
    # 不在 WS 端点 handle_dialog_stream 本体
    m = re.search(r"async def _ws_handle_message\(.*?(?=\nasync def |\Z)",
                  _SRC, re.S)
    assert m, "_ws_handle_message 不存在"
    return m.group(0)


def test_h7_stop_flag_outlives_producer_wait():
    """断开分支：add 旗标 → wait_for(producer) 有界等待 → finally 才 discard。"""
    fn = _handler_body()
    seg = fn[fn.find("_ws_alive = False"):fn.find('reply = "".join')]
    add_pos = seg.find("_stop_flags.add(sid)")
    await_pos = seg.find("wait_for(producer")
    timeout_pos = seg.find("timeout=120.0")
    discard_pos = seg.find("_stop_flags.discard(sid)")
    assert 0 < add_pos < await_pos < discard_pos, (
        f"H7 回退：旗标顺序错乱 add={add_pos} await={await_pos} "
        f"discard={discard_pos}（旧病=finally 立即 discard）")
    assert timeout_pos > await_pos, (
        "等待无上限：引擎不响应停旗标时会无限占住 dialog 功能锁")


def test_h7_passive_completion_skipped_on_disconnect():
    """被动补全条件须含 _ws_alive（断开不烧第二次推理）。"""
    fn = _handler_body()
    m = re.search(r"if reply and not error_holder[^:]*:", fn)
    assert m, "被动补全入口条件丢失"
    assert "_ws_alive" in m.group(0), (
        f"被动补全未跳过断开态: {m.group(0)!r}")


def test_h7_post_save_sends_guarded():
    """保存点之后的 meta/done 发送须带 _ws_alive 守卫。"""
    fn = _handler_body()
    save_pos = fn.find("_save_message(sid, \"assistant\"")
    meta_pos = fn.find('send_json({"type": "meta"')
    done_pos = fn.find('send_json({"type": "done"')
    assert 0 < save_pos < meta_pos < done_pos, "保存/发送顺序被改动"
    # 守卫形态：meta 发送前有 _ws_alive 判（紧邻 if）
    guard = fn[fn.rfind("if _ws_alive", 0, meta_pos):meta_pos]
    assert "_ws_alive" in guard, "meta 发送缺 _ws_alive 守卫"
    guard2 = fn[fn.rfind("if _ws_alive", 0, done_pos):done_pos]
    assert "_ws_alive" in guard2, "done 发送缺 _ws_alive 守卫"
