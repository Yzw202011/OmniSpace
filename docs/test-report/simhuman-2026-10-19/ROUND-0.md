# 仿真人深测 ROUND-0 报告——修复先行批（2026-10-19）

> 计划真源：docs/test-plan-simhuman-grand-2026-10-19.md（846 条）。
> 本轮性质：用户令「先继续修复，后继续测试」——修复批 + R1 只读 P0 首批实弹。
> 执行窗口：后端健康（uptime 千秒级）、GPU 空闲 40°C、并行会话 3~4 条线在飞（dist/settings/test-rounds/manga），
> 全程未重启后端、未重建 dist、未动他线 WIP。

## 一、本轮修复（已入库）

| # | 修复 | 提交 | 验证 |
|---|---|---|---|
| F-1 | 写作台脏稿守卫三连：切章确认弹窗/生成写回保留用户稿+「载入新正文」显式入口/章删复位（K-08 现行形态+C-33 根修，NovelPage.tsx 79 行） | d1dee28f | tsc 全绿+eslint 0+vitest 96 过；随下次 dist 部署生效后补 IAB 实弹 |
| F-2 | 全仓 ruff 闸 38 处机械清账（H3 脚本 17 支+main/video/h3_chain_engine/sr_dlssnr_runner 等）+ sr_modes Path→str 类型真修 | 被 fac7bfdc（打包线 05:11）卷走进库+acd3ef8d 基线件 | 全仓 ruff 'All checks passed'+py_compile 22 文件+mypy 单文件 0 错 |
| F-3 | models/buffalo_l(+.zip) 602MB POC 残留删除（生产读 ComfyUI 侧 antelopev2，全仓零路径引用）→ smoke 闸解封 | 无 git 件（models/ 不入库） | 注册表哨兵 test_model_registry 6/6 复绿 |
| F-4 | 架构基线记账：comic_asset.py 2529→2648（10-07 d5ffa25f 既成涨账） | acd3ef8d | 第 6 闸对已定型文件恢复可判 |

## 二、闸门状态（本轮结束时）

| 闸 | 状态 | 说明 |
|---|---|---|
| 1 compileall / 2 ruff / 3 smoke / 4 mypy / 5 文档对账 | ✅ 全绿 | ruff 由 F-2 解封；smoke 由 F-3 解封；mypy 未动基线 |
| 6 架构哨兵 | ⚠️ 红（在飞件） | comic_gen.py +304 行超容忍——并行漫剧线 WIP 未定型，不代记账；其落地时须拆分或记账 |

## 三、R1 只读 P0 首批实弹（7/7 ✅）

| 用例 | 结果 | 证据 |
|---|---|---|
| N-01 /health×5 | ✅ | 0~29ms（M8 P95<800ms 达标），request_id 全带 |
| N-02 错误信封×2 | ✅ | 404→success:false+SYSTEM_RESOURCE_NOT_FOUND；坏参→KNOWLEDGE_NOT_FOUND（HTTP 200 信封制 ADR-01 达约） |
| A-21 知识统计端点 | ✅ | 端点 21ms 可用（与状态栏 1,301 条的数值对账留全量轮） |
| B-84 会话列表只读 | ✅ | 10ms，真实数据未动 |
| N-44 API 前缀保护 | ✅（判定口径修正） | 越前缀探测被统一信封 NOT_FOUND 拒绝、未吐 health=命名空间保护成立 |

**进度：846 条中已实弹 7 条（R1 P0 116 条中 7 条），全部 ✅；0 ❌ 0 🔶。**

## 四、并行会话事件簿（诚实留痕）

1. 05:11 打包线 fac7bfdc（H1 出厂哨兵）将本会话工作区内的 22 文件 ruff 修复+sr_modes 类型修**卷走进库**（逐文件 diff 吻合，内容零丢失、归属两线混合）；acd3ef8d 提交信息与实际内容不符（只含基线），已在 d1dee28f 提交信息中书面更正。
2. settings 线 05:24/05:27/05:31 三笔（BUG-01~08 修复+258 条计划+报告）；test-rounds 线 Round16/17 两笔；manga 线 comic_gen.py 在飞。
3. 教训：**并发窗口内 commit 要一次成型小步快跑**——多闸失败的多轮补修间隙就是被卷窗口。

## 五、下一步

1. 等 dist 部署窗口：NovelPage 守卫随部署上线后补 C-33 IAB 实弹（R1 补充批）。
2. R1 P0 余 109 条按窗口分批：GPU 类（D-90/91、E-125、G-15/18）须独占窗口；🔒 真实数据只读类可随时。
3. 第 6 闸红账移交：comic_gen.py 拆分或记账归漫剧线落地时处理。
4. 测试轮次发现即修即提（沿用本轮「修复→闸绿→commit」节奏）。
