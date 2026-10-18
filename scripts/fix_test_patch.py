"""修正互斥单测的 thermal guard patch 目标（函数内延迟导入）。一次性工具。"""
p = 'tests/unit/test_video_paint_mutex.py'
s = open(p, 'rb').read().decode('utf-8')
old = (
    '    import src.services.image_queue as iq' + chr(10) +
    '    monkeypatch.setattr(' + chr(10) +
    '        iq, "get_thermal_guard",' + chr(10) +
    '        lambda: type("G", (), {"is_paused": staticmethod(lambda: False)})())')
new = (
    '    # get_thermal_guard 是函数内延迟导入——patch 源模块' + chr(10) +
    '    import src.services.thermal_guard as tg' + chr(10) +
    '    monkeypatch.setattr(' + chr(10) +
    '        tg, "get_thermal_guard",' + chr(10) +
    '        lambda: type("G", (), {"is_paused": staticmethod(lambda: False)})())')
assert old in s, 'anchor'
s = s.replace(old, new, 1)
open(p, 'wb').write(s.encode('utf-8'))
print('fixed')
