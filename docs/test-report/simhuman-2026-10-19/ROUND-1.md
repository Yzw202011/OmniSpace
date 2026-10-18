# 仿真人深测 ROUND-1 报告——修复批二（2026-10-19）

> 计划真源：docs/test-plan-simhuman-grand-2026-10-19.md（846 条）。承接 ROUND-0。
> 窗口：后端健康（并行会话 05:4x 重启过）、GPU 空闲 42°C、在飞 WIP=对话引擎/队列/漫剧/打包四线。

## 一、本轮修复（已入库 6b2c02de，6 文件）

| # | 修复 | 定性 |
|---|---|---|
| F-5 | cloud_video_client：kling/ark/vidu 分支超时文案引用未绑定 `status`——真超时时 NameError 顶掉超时提示 | 真 bug（测试轴抓出） |
| F-6 | novel_service 前章断链根修（K-07 现行形态）：【上一章结尾】锚改取最近「完成且有正文」章，紧邻章失败/空稿不再丢锚 | K 表未修项真修 |
| F-7 | BrowserError 族签名 `code: int→str` + browser_pool 最后一处数字码 72001→BROWSER_UNAVAILABLE | 语义串化漏网（mypy 闸 7 条红的真因），**闸 4 全量复绿** |
| F-8 | request_context 访问留痕：非 2xx 或 ≥500ms 上 INFO（rid 进日志），其余 DEBUG 防轮询噪声 | N-03 ❌ 实测发现的观测缺口根修；随下次后端重启生效 |
| F-9 | MangaWorkspace promoteAssets 吞错→reportBgError 留痕 | 吞错扫描红账 -1 |

附带定性：**K-05 云视频幽灵 pending 已在库内演化中被修**（30 分钟轮询 deadline + 孤儿回收首次入队触发）——由修转回归验证项；残余弱点=重启后回收要等下一次视频入队才触发（video_queue 在飞，移交该线）。

## 二、R1 只读 P0 第二批（7/7 ✅ + 1 ❌ 即修）

| 用例 | 结果 | 证据 |
|---|---|---|
| N-09 训练队列快照 | ✅ | total=28，15ms |
| N-09b 风格状态 | ✅ | 9ms |
| A-21 知识计数三方对账 | ✅ | API total=1301 == 状态栏「知识 1,301 条」（值级对账完成） |
| G-35 模型注册表 | ✅ | 23 模型 |
| J-23 系统设置读 | ✅ | 17ms |
| K-16 日志事件 | ✅ | total=7475 |
| B-64 会话读 | ✅ | 6 会话 |
| **N-03 request_id 贯穿** | **❌→已修** | rid 在响应信封存在但 logs/backend.log 计数 0（前后端对不上账）；F-8 根修，重启后回归 |

## 三、累计进度与未完成清单

**进度：846 条已实弹 15 条（14 ✅ / 1 ❌ 已修待回归）；R1 P0 116 条中余 101 条。**

未完成/受阻（按解锁条件）：
1. **GPU 独占窗口**：D-90/91 ComfyUI 冷启、G-15/18 装载、E-125 五步全链、M6/M7 视频与超分基线——等无并发任务窗口；
2. **dist 部署窗口**：NovelPage 脏稿守卫（d1dee28f）+MangaWorkspace 留痕随部署上线后补 C-33/IAB 实弹；
3. **后端重启窗口**：F-8 访问日志生效后补 N-03 回归；
4. **漫剧在飞线红账四件**（非我圈，移交）：comic_gen.py +304 拆分（第 6 闸唯一红因）、ComicWorkspace.tsx:101 吞错、statusLabels/videoStatus 前后端状态枚举漂移×3、useMangaStore openProject 测试；
5. **待拍板项**（不修）：K-01 资产重复行（A清理/B防重/C手删）、K-02 manga-video 槽消费设计、anti-reverse G4 两 P1（升级回滚）；
6. K 表已修转回归：K-05（deadline+孤儿回收在库）、K-08（ROUND-0 F-1）。

## 四、闸门状态

1 compileall ✅ / 2 ruff ✅ / 3 smoke ✅ / 4 mypy ✅（F-7 复绿，0 新增）/ 5 文档对账 ✅ / 6 架构哨兵 ⚠️ 红（唯一红因=在飞 comic_gen.py，漫剧线落地时拆分或记账）。
