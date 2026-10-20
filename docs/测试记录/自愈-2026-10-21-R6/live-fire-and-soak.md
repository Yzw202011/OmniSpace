# 自愈重建 · R6 真火+浸泡起跑 实弹记录（2026-10-21 01:54~02:04）

## 一、批5 埋点真火（GPU 窗口实弹，SELFHEALTEST 测试项目）

| 链 | 结果 | 证据 |
|---|---|---|
| 大纲生成（P1 埋点） | ✅ | flow `outline_generate` **success，output=「4 卷 20 章」**（01:55:11→01:57:40，约 2.5min 含 4B 引擎冷加载）；start+end 记账全链 |
| 章节生成（P0 埋点） | ✅ | flow `chapter_generate` **success，nodes=2**：`引擎加载`(success, 热态 0ms) + `正文生成`(success, **84975ms**)——两枚新埋节点全部真实打卡，nodes≥1 判据达成 |

流程：造 SELFHEALTEST 项目（python 直调，Git Bash curl 中文 body 必炸坑复现并绕过）→ 大纲 → 第一章生成 → flow_trace 全程对账 → **batch-delete 级联清零**（项目/章节残留 0；flow 账本留痕属诚实记账不删）。

## 二、S-12 双开轰炸 100 连 ✅

100 次连续拉起 boot.py（对运行中链）：**exit0=100 / 非零=0，耗时 25s**（探测路径极快）；全程主链 healthy、守护（pythonw）与后端（OmniSpace-Backend 品牌 exe）进程数恒定。单实例守卫「复用并退出」路径 100/100。

## 三、S-01 24h 被动浸泡探针起跑 ✅

- `scripts/soak_selfheal_probe.py`（ruff 绿）02:03:28 发射，脱离会话独立运行。
- 每 60s 一行快照（/health status+uptime、RAM%、VRAM GB）→ `logs/soak_selfheal.jsonl`；异常事件（down/restart/ramlow≥85%）→ `logs/soak_selfheal_events.log`；24h 后自动退出（明早 ~02:03）。
- 首采样：healthy / uptime 1060.8s / RAM 50.1% / VRAM 1.21GB ✅
- **复核待办（明日）**：S-01 无静默死亡/无 RADAR 前兆、S-05 重启时长不劣化、S-11 重启频率对账 R15 基线。

## 四、终局状态

- 健康链 5800+5850 在跑；测试项目零残留；探针独立运行中。
- keyframe/资产链埋点仍锁定（漫剧线 comic_gen.py/ComicWorkspace.tsx WIP 在飞，本次复核确认）。
