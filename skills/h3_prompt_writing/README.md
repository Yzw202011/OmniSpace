# H3 官方 Prompt 写作指南三件套（真源镜像）

来源：MiniMax 官方仓 [MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)
路径：`.agents/skills/h3-prompt-writing/`（官方同时镜像于 `.claude/skills/` 同名目录）
拉取：2026-09-30（当日本轮调研经 raw.githubusercontent 直拉，未经第三方转手）

## 三件构成

| 文件 | 内容 | 用途 |
|---|---|---|
| `SKILL.md` | 五种输入模式（T2VA/I2VA/FL2VA/L2VA/Ref2VA）路由 + 字段顺序总纲 | 官方声明"可移植到任何能读本地文件的 agent，无外部 API 依赖"——本仓 AI 链可直接引用 |
| `references/base-en.txt` | 文生/关键帧模式完整语法：三段式结构、对齐指令行、运镜三维词表、切镜时间戳、说话人 ID、`<d>`/`<scenetrans>`/`<cutoff>` 标记、画面文字规则、四个完整官方案例 | T2V 标准链与翻译模板升级的规则真源 |
| `references/ref-en.txt` | 全参考模式六段式：四类引用标签（`<Subject N>`/`<Picture N>`/`<Video N>`/`<Audio N>`）独立编号、retention 关系分析、350-500 词规范、完整样例 | 导演台链（ReferenceToVideo + TheodoreDirector）的规则真源 |

## 与本仓的接线（2026-09-30 批1 落地）

- `src/api/manga/video.py` 的 `_H3_TRANSLATE_SYSTEM`（翻译模板）按 base-en.txt 语法要点升级
- `skills/h3_convert/rules.py`（六段式构造）对照 ref-en.txt 的差距审计见
  `docs/audit/h3-prompt-official-alignment-2026-09-30.md`（批2 修复的供料清单）
- 官方防畸变思路：把模型自由发挥空间锁死在 prompt 语法（运镜三维/时间戳/ID 稳定/
  retention 关系）——文字理解与防畸变同源同修

## 维护

上游官方仓更新时重拉覆盖即可（本目录为镜像不改写）；改动需求走方案四段制。
