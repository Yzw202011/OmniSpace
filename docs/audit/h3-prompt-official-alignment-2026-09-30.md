# H3 Prompt 官方语法对齐审计（批1-c，2026-09-30）

**性质**：差距清单（只列不改），批 2 修复的供料。
**对照双方**：官方真源 `skills/h3_prompt_writing/`（2026-09-30 官方仓直拉镜像）
vs 现实现 `skills/h3_convert/rules.py`（166 行）+ `convert.py`（385 行）。
**验证状态**：本文全部结论为文本级全文对照 ✅；唯 C-1 的 adapter 消费格式未核实 ⚠️（批 2 前置任务）。

## A 级差距（语法违反实锤，低风险可修）

| # | 现实现 | 官方规则 | 位置 |
|---|---|---|---|
| A-1 | `[Shot 1] At 0.00 seconds, ...` 第一镜带时间戳且格式 `0.00 seconds` | 第一镜**不加时间戳**；后续镜格式 `At 00:03.500`（分:秒.三位毫秒）。咱导演台段=单镜，时间戳应整行删除 | rules.py:125 vs base-en §4.2 |
| A-2 | summary 前缀 `[reference generation]` | 首帧锚=帧锚定，属 `keyframe completion`；多关系用 ` + ` 组合 → 应为 `[reference generation + keyframe completion]` | rules.py:80 vs ref-en §3 |
| A-3 | 对白行 `<S1> murmurs: <d>[cmn] ...</d>`（尖括号 ID） | 官方：`<Subject 1> (S1) murmurs: <d>[cmn] ...</d>`——**主体标签 + 圆括号 ID**，且首次出现给识别短语（年龄/性别/音色/语速） | rules.py:132 vs base-en §4.4 / ref-en §5 |
| A-4 | 运镜独立标签行 `Camera: {camera_en}` | 官方明确"运镜写成镜头内自然英文动作句，**不要堆标签**"（type+amplitude+speed 三维）。翻译模板已于批1-b 升级（video.py `_H3_TRANSLATE_SYSTEM`），convert 拼装格式待批2 | rules.py:128 vs base-en §4.3 |

## B 级差距（丰富度差距，中风险，批2 拍板项）

| # | 现实现 | 官方规则 |
|---|---|---|
| B-1 | detailed_description 全段 2-4 句（style 声明+action+camera+对白） | 生成任务 350-500 英文词，逐镜按构图/主体外观位置/环境光照/动作状态变化/运镜/声音展开；对话密集优先台词时间线。受上游 C 段中文文本量约束——修法两选一：翻译模板扩写指令（轻）或 convert 层加 LLM 扩写环节（重） |
| B-2 | 无 sfx 时兜底 `Quiet ambient atmosphere only; no prominent sound events.` | N/A 仅限用户明确要求全静音；无具体声效时应写 plausible 环境底噪（近义，低优先） |

## C 级差异（标签体系差异，高风险——改前必须核实消费端 ⚠️）

| # | 现实现 | 官方规则 | 风险 |
|---|---|---|---|
| C-1 | 素材引用 `{{ref:别名}}`（Theodore V7 导播台自定义约定） | 四类官方标签 `<Subject N>`/`<Picture N>`/`<Video N>`/`<Audio N>` 独立编号 | **改前必须核实 TheodoreDirector_H3Adapter（custom_nodes）实际解析哪种格式**——若 adapter 按 `{{ref:}}` 解析资产槽位，换官方标签即断链。"存在≠可装"同类教训 |
| C-2 | 首帧对齐声明并入 subject_definitions 段内 | 官方 Part-1 对齐指令行独立于六段之前（"For the target video, at 0.00 seconds ... is fully referenced."） | 语义等价迁移；若 adapter 消费六段 schema 则维持现状（低优先） |

## D 级（功能已备未接线）

| # | 现状 | 说明 |
|---|---|---|
| D-1 | `dialogue_cutoff_mark()`（rules.py:153）提供 `<cutoff>` 但 convert.py 未调用 | **2026-09-30 批2 判定：暂缓**——精确接线需台词起止时间数据（台词是否被段尾裁时截断），上游 C 段协议只有镜级 start/end 无台词级时间戳，硬做即假逻辑。待上游协议补台词时间后接线 |

## 批 2 修复记录（2026-09-30，用户拍板 C 连做）

- ✅ A-1 首镜时间戳删除（rules.py `detailed_description_section`）
- ✅ A-2 summary 前缀组合 `[reference generation + keyframe completion]`
- ✅ A-3 说话人官方格式 `<Subject N> (S1) manner: <d>[cmn]…</d>`（含 subjects 映射注入 + 语气括号捕获 `manner_zh`→英文 manner 词表，此前语气被解析丢弃）
- ✅ A-4 运镜 `_camera_sentence()`：自然句直通 + 降级包裹，"Camera:" 标签行绝迹
- ✅ 新增 7 条语法锁定断言（test_convert 31 passed）
- ⏸ D-1 暂缓（见上）
- B 级/C 级未动（B-1 扩写路线随转义层落地一并评估；C-1 待 adapter 消费格式核实——本批 validate.py 反向证实其消费 `{{ref:}}`：`test_validate_rejects_official_numbering` 主动拦截官方 `<Picture N>` 标签）

## 已对齐项（免修，验收基线）

- 六段字段顺序（FULL_FIELDS vs ref-en §1 表）✅
- retention 关系词形 `fully_preserved` 下划线（rules.py vs ref-en §4 表）✅
- summary 方括号 task-type prefix 机制 ✅（仅组合缺失，见 A-2）
- 对白逐字 `<d>[cmn]` 保留原文不译（rules.py:119 vs base-en §4.4）✅
- overall_soundscape 不含对白/配乐、non_diegetic_music 无配乐 N/A（rules.py:137-150 vs base-en §4.6/4.7）✅
- subject_definitions 逐行定义+保留特征（rules.py:49-75 vs ref-en §2）✅
- 每段 5-15s 训练域（SEGMENT_MIN_S=5，>15s 已有 P3 标定把关）✅

## 批 2 修复建议顺序

A-1 → A-2 → A-3 → A-4（低风险语法四连修，每项带 test_convert 断言）→ D-1（cutoff 接线）
→ C-1 核实后另拍板 → B-1 拍板扩写路线。B-2 顺手。
