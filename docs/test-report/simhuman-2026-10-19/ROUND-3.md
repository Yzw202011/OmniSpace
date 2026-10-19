# 仿真人深测 ROUND-3 报告——协议层+导航层只读批 + 帮助页漂移即修（2026-10-19）

> 计划真源：docs/test-plan-simhuman-grand-2026-10-19.md（846 条）。承接 ROUND-0/1/2。
> 窗口：后端连跑 7.2h（06:52 版本，早于 07:07 中间件修复）；dist 仍 05:48；GPU 侧有并行会话活动痕迹。
> 我圈可修候选（K-06/K-09/K-19/comic_gen 拆分）仍全被在飞件占住 → 本轮按诚实转**测试密集轮**。

## 一、R1 只读 P0 第三批（10 项：9✅ + 1❌即修）

| 用例 | 结果 | 证据 |
|---|---|---|
| N-05 /ws 心跳 | ✅ | ping→pong 0ms，payload 正确 |
| N-07 hardware/realtime 节奏 | ✅ | 5 帧间隔均值 2.00s、最大偏差 0.02s（M14 阈值 ±20% 远超达标） |
| N-20 Host 伪造 | ✅ | 伪造 Host → HTTP 400 "Invalid host header"（TrustedHost 闸实弹） |
| K-16 日志端点扩展 | ✅ | stats(7553 事件)/modules/errors/summary(learn 1541 top)/flows/trace-stats(18335 条，running=0 零悬挂) 五端点全过；顺带健康信号：flow 零悬挂 |
| A-26 未知路由 | ✅ | `#__not_exist_route__` → 重定向 `#/chat` |
| A-25 深链 | ✅ | `#/settings?tab=data` 直达数据签（tab [selected]） |
| A-03 导航高亮 | ✅ | 点帮助后高亮跟随（helpActive ✓ chatInactive ✓ hash=#/help） |
| L-05 关于版本 | ✅ | 帮助页 v2.5.0 == /health version 2.5.0 |
| N-19 限流 | ✅（口径修正） | /health 305 发全 200——查实 `/health` 在 `_WHITELIST_PREFIXES` 显式豁免（R1-14 精修），健康检查不限流为正确设计；业务端点实弹留独占窗口（避免污染并行会话事件流） |
| **L-01 帮助页模块简介** | **❌→即修** | 实测仅覆盖 4/6 模块名：旧 MODULES 挂着已清退的「AI绘画」与已并入训练中心的「视频风格」，缺写作台/训练中心/MagicAI/系统日志 |

## 二、本轮修复（d15ca7b）

帮助页 MODULES 九模块与 11 路由现状对齐（AI绘画→AI漫画 更名、视频风格条目清退、四缺失模块补齐，文案逐页核对）。tsc+eslint 绿；**随下次 dist 部署生效后浏览器复测 L-01 销账**。

## 三、累计进度与未完成

**进度：26/846 已实弹（24✅ / 2❌已修待回归：N-03 等后端重启、L-01 等 dist 部署）；R1 P0 余 90。**

未完成/受阻（解锁条件不变）：
1. GPU 独占窗口 → D-90/91、G-15/18、E-125、M6/M7、N-19 业务端点限流实弹；
2. dist 部署 → L-01 复测 + F-9/F-13 上线验证；
3. 后端重启 → N-03/K-07 回归；
4. 漫剧在飞红账（移交）→ comic_gen.py 拆分（第 6 闸唯一红因）、ComicWorkspace:101（vitest 唯一红）；
5. 待拍板三项 → K-01 资产重复行 A/B/C、K-02 视频槽消费、anti-reverse G4 两 P1。

## 四、闸门状态

1~5 ✅ / 6 ⚠️（在飞 comic_gen.py，红因不变）。

## 五、执行坑增补

- IAB 飘花动画坑（ROUND-2 已录）本轮复用 dispatchEvent 兜底零阻碍；帮助页/设置页定位器正常。
- 探针副作用提示：N-02 的 404 探测会被事件系统记为 module=`__no_such_endpoint__`（与系统日志线 GAP-10「模块下拉被污染」同源）——后续探测统一走不落事件的路径或接受该噪声。
