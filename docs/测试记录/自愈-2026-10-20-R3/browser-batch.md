# 自愈重建 · R3 浏览器批实弹记录（2026-10-20 21:2x）

> 零破坏批：I 组可测子集 + A-27。工具=IAB（role-click 超时按已知坑走 dispatchEvent 兜底）。

## 实弹结果
| 用例 | 结果 | 证据 |
|---|---|---|
| I-16 体检卡 10 项渲染 | ✅ | 「共 10 项检查」入 summary；10 项逐条三色渲染；靶标生效时 logs_retention=注意+「一键修复」按钮现身 |
| **I-17 修复闭环** | ✅ **抓到真缺陷 SH-GAP-14 并当场修复闭环** | 见下 |
| I-19 深度体检 | ✅ | UI 渲染诊断项（GPU 可用性/FFmpeg 等项名可见），24 通过 0 未通过；Chroma 字样未在 UI 文本出现（小疑点记账；API 层 27 项已在 J-17 证） |
| I-25 WS 灯 | ✅ | 底部状态栏「实时通道已连接」常驻 |
| I-28 全局异常上报 | ✅ | evaluate 注入未捕获 Error → 1 条 SELFHEALTEST 记录入 events-20261020.jsonl；页面未崩（ErrorBoundary 只采集不炸页） |
| A-27 启动页 | 🔶 | **5850 就绪后自动跳转 5800/#/chat 被实弹抓到**（正向引流行为验证）；进度 HUD 的 DOM 测量窗口仅存在于后端未就绪的启动瞬间→留下次重启窗口 |

## SH-GAP-14（新缺陷，当场修复）
- **现象**：体检报「有 1 个过期日志文件可一键清理」（checker 按 mtime），点「一键修复」toast=「已清理 0 个过期日志文件」，警告永挂——修复闭环断裂。
- **根因**：event_log.py cleanup_expired 事件日志段只认 `events-YYYYMMDD` 文件名日期（strptime 失败直接 continue），异名过期文件永远清不掉；checker（health_check._check_logs_retention）按 mtime。口径分裂。
- **修复**：清理器文件名解析失败回退 mtime（异名兜底，标准名行为不变）。
- **验证**：单测 test_logs_cleanup_covers_odd_named_expired_file（19 passed）+ ruff + mypy 无新增 + 子进程直调 cleanup_expired（deleted 含靶标、文件消失）+ 浏览器复检全绿（一切正常/日志项转绿/修复按钮消失）。

## 附带观察（如实记）
- 体检期间底部栏 GPU 3%→98%、显存 1.3→10.5GB（外部模型装载动作，非浏览器批所为）。
- 硬件健康卡「保护动作已启用」5 条全部在列（显存自动卸载/内存危急拒新/GPU 温度暂停/自动降参/后端补位 ≤5 次）=L3 守卫的前端可见性佐证。
- 设置页维护分区 URL 同步 `#/settings?tab=maintenance`。
