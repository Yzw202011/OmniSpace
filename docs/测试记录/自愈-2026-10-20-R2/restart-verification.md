# 自愈重建 · R2 重启窗口实弹记录（2026-10-20 21:02~21:06）

> 用户拍板「该重启就重启」。全程正规链：POST 启动页 5850 /api/quit → pythonw 重拉 boot.py。
> 战场前置核查：队列空、近 8 分钟无业务事件（最后活动 20:05）、另一会话已落库 8a02f7c5。

## 时间线
| 时刻 | 事件 |
|---|---|
| 21:02:06 | POST /api/quit → `{"ok": true}` |
| 21:02:08 | 5800 下线（等待 2s）；5850 全下线；logs/.heartbeat 被代摘 |
| 21:02:26 | pythonw boot.py 重拉（完全脱离会话） |
| 21:02:55 | /health healthy——**全链就绪 29s**（env 2.9s + backend 24.8s，240s 上限内） |

## 实弹结果（11 项 ✅）
| 用例 | 结果 | 证据 |
|---|---|---|
| A-11 正规退出链 | ✅ | quit ok、2s 下线、心跳代摘 |
| B-03 优雅停机不误报 | ✅ | 最后一条 crash_detected=15:58（本轮零新增） |
| **批4 幽灵清扫** | ✅ **主链** | 受控幽灵（SELFHEALTEST 注入 2 行）：learning_sessions `running→interrupted`+stop_reason「应用退出中断」；char_lora_tasks `training→error`+「已中断（应用退出/断电），可重试」。验后即删，零残留 |
| 批1 复认 | ✅ | health-check 10/10 全 ok（knowledge/cloud 在列） |
| 舱壁 | ✅ | dialog/paint/video/training 全 ok |
| A-28 冷启动计时 | ✅ | 29s（暖机基线 ~22s 口径附近，上限 240s 内） |
| A-22 启动页 status | ✅ | 五相全报（splash/env/backend/warmup/ready） |
| E-14 按需加载回归 | ✅ | warmup=skipped「默认不预热（按需真实加载）」 |
| A-04 双击幂等 | ✅ | 二次 boot：「检测到已在运行的启动页…复用并退出本实例（单实例守卫）」EXIT=0，主链无恙 |
| B-01 心跳 | ✅ | logs/.heartbeat 持续刷新（21:04:24） |
| A-01 解释器守卫 | ✅(变体) | 5800 监听=`runtime\py312\OmniSpace-Backend.exe`（品牌化 exe 包 py312，进程品牌化机制） |

## 未做（挂账）
- A-05~A-10/A-12/A-14~A-17/A-20/A-23~A-26：需破坏性故障注入（杀后端验看门狗、占端口验递进、假 state.json 验断电自愈等）——留专门故障注入窗口。
- A-27 启动页 DOM 实测：待浏览器批（本次仅 API 级）。
- 批5 真火：待 GPU 训练窗。

## 重启前幽灵快照（佐证当时零天然幽灵）
train_tasks: cancelled 1/done 3/error 22｜style_tasks: 空｜char_lora_tasks: done 2｜learning_sessions: completed 120/interrupted 22
