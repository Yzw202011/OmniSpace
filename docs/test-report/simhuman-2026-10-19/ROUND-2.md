# 仿真人深测 ROUND-2 报告——前端红账收口 + C-33 守卫 IAB 实弹（2026-10-19）

> 计划真源：docs/test-plan-simhuman-grand-2026-10-19.md（846 条）。承接 ROUND-0/1。
> 窗口：后端健康（06:52 启动，早于 07:07 中间件修复，N-03 回归仍等重启）；GPU 有并行会话加载模型（显存 9.9GB），
> 本轮全程 CPU-only 未碰生成链；dist 05:48 重建（已含 ROUND-0 NovelPage 守卫）→ 浏览器实弹解锁。

## 一、本轮修复（已入库 2e927ad4，6 文件）

| # | 修复 | 定性 |
|---|---|---|
| F-10 | useMangaStore.test：mock 工厂补 listVideoTasks/listKeyframes；openProject 断言更新为 fetchAssets 双清单制现行契约 | 测试滞后（功能已演进未跟测）|
| F-11 | videoSlice：listKeyframes 返回值 `\|\|[]` 防御 | 健壮性小加固 |
| F-12 | statusLabels.test：MODEL_AVAILABLE 清单补 cloud | 测试清单滞后 |
| F-13 | videoStatus.ts+schema.ts：补 SR 超分四态 running/busy/failed/none（枚举+标签+Zod） | 超分产线入库后前端承接滞后（宁松勿严） |
| F-14 | videoStatus.test：扫描器按域排除 models_available.py | 域分清（模型可用性域归 statusLabels 承接） |

**前端测试红账：5 → 1**（余 1 = ComicWorkspace.tsx:101 吞错，漫剧在飞件专属，移交）。

## 二、IAB 实弹（R1 补充批）

**C-33 脏稿守卫全链 ✅（四路全对，dist 已上线验证）**：
1. 脏稿切章 → 确认弹窗精确出现：「本章有未保存的修改」+「不可恢复」+ 两行说明 + 「留在本章/丢弃并切换」双钮（文案与 d1dee28f 设计一字不差）；
2. 留在本章 → 弹窗消失、弹回第 1 章、脏稿原文保留、dirty 保持（保存钮可点）；
3. 保存修改后干净切换 → 无弹窗直接换章；
4. 再脏稿切换 → 丢弃并切换 → 明确弃稿、目标章正常载入。
- DB 对账：章详情落库含验证串 ✓；TESTAUTO 项目级联清理 ✓；逆天邪神🔒零接触 ✓；后端全程健康 ✓。
- **IAB 坑留痕（补强纪律）**：Dali 主题动画令 role 定位器 click 的稳定性判定超时（元素可点性三证齐全仍超时）→ dispatchEvent 兜底全通；后续 IAB 用例定位器超时两连即走兜底，不再重试。

## 三、累计进度与未完成

**进度：16/846 已实弹（15✅ / 1❌已修待回归）；R1 P0 余 100。**

未完成/受阻（解锁条件同前，新增两项）：
1. GPU 独占窗口：D-90/91、G-15/18、E-125、M6/M7（本轮观察到并行会话已开始加载模型，窗口收紧）；
2. dist 部署窗口：F-9（MangaWorkspace 留痕）等本批前端修复随下次部署上线；
3. 后端重启窗口：N-03 回归（rid 落日志）；
4. 漫剧在飞红账（移交）：comic_gen.py 拆分、ComicWorkspace:101；
5. 待拍板三项：K-01 资产重复行 A/B/C、K-02 视频槽消费、anti-reverse G4 两 P1；
6. K 表已修待回归：K-05（deadline+孤儿回收）、K-07（F-6 prev_tail 锚，随下次重启生效）、K-08（F-1 本轮已实弹销账 ✅）。

**K-08 正式销账**：ROUND-0 修复 + 本轮 IAB 实弹通过 → 从「已知缺口」转「已修复已验证」。

## 四、闸门状态

1~5 闸 ✅ / 6 闸 ⚠️（唯一红因=在飞 comic_gen.py，漫剧线落地时收口）。
