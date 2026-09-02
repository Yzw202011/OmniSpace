# OmniSpace AI 数据库 ER 说明

> 版本 v2.3.1 ｜ 生成于 2026-08-20（TASK-P2-02，对应审计 P03）｜ 事实来源：backend/data/database.py 及各服务层自建表 DDL 全量提取
> **2026-08-28 校准**：实测 `data/omnispace.db` 共 **31 张用户表**（37 个对象含 FTS5 影子表与 sqlite_sequence），本文档成文时为 30 张、漏记 `art_styles`（database.py `_SCHEMA` 建表，供漫剧漫画风格包使用）；`PRAGMA user_version` 实测为 **7**（原文 3），迁移组 4~7 详见 `database.py` `_MIGRATION_GROUPS`。
> **2026-09-01 校准（只读实测）**：主库实测 **30 张用户表**（36 对象 = 30 用户表 + 6 张 knowledge_fts\* 影子表；加上 sqlite_sequence 为 37）。两处口径修正：① `flow_executions / flow_nodes` 实际建在**独立库 `logs/flow_trace.db`**（此前按主库口径计数系误差），两库合计 **32 张用户表**；② `paint_history` 一直在主库但历史计数从未包含。列级漂移已同步：art_styles 6 列（+pack/pack_def）、keyframes（+shot_seeds/consistency）、dialog_messages（+reasoning）、projects（+art_style）。全字段清单见 `docs/全量技术文档-2026-09-01.md` 附录 B。
>
> 配套文档：[架构总览](architecture-overview.md) ｜ [API 端点总表](api-endpoints.md)

单一 SQLite 库 `data/omnispace.db`（WAL 模式，busy_timeout 10000ms，`PRAGMA foreign_keys=ON`）。规格 §3.2 称"13 张业务表"，实际用户表 **32 张 = 主库 30 + flow 独立库 2**（2026-09-01 实测校准）：18 张集中在 `database.py` 的 `_SCHEMA`（建库时统一创建，含 art_styles），主库另有 12 张由各服务层在首次使用时 `CREATE TABLE IF NOT EXISTS` 幂等自建（含此前漏计的 paint_history），`flow_executions/flow_nodes` 两张建在独立库 `logs/flow_trace.db`。向量数据不在 SQLite——`data/vector_db.py` 走 ChromaDB 独立持久化（data/chroma/）。

## 1. 全景关系图

外键只有漫剧域与对话域两处显式声明，其余跨表关系由应用层维护（图中以虚线标出）：

```
对话域                          漫剧核心域（级联删除链）
┌────────────────┐             ┌──────────────┐
│ dialog_sessions│◄─┐          │   projects   │
└───────┬────────┘  │FK        └──────┬───────┘
        │1:N        │                 │1:N
┌───────▼────────┐  │          ┌──────▼────────┐     ┌──────────────┐
│ dialog_messages│──┘          │  storyboards  │◄─┐  │ comic_assets │
└────────────────┘             └──────┬────────┘  │FK└──────▲───────┘
                                      │1:N         │        │应用层关联
                               ┌──────▼────────┐   │  ┌─────┴────────┐
                               │ storyboard_   │   │  │   keyframes  │
                               │     rows      │───┼──►(row_id 应用层)
                               └──┬───┬───┬────┘   │  └──────────────┘
        ┌──────────────────┬─────┘   │   └───┬─────────────┐
        │1:N(应用层)       │voice_id │       │row_id 应用层  │
┌───────▼────────┐ ┌───────▼──────┐ │ ┌─────▼─────┐ ┌─────▼──────┐
│  video_tasks   │ │voice_profiles│ │ │scene_     │ │ director_  │
└────────────────┘ └──────────────┘ │ │ objects   │ │ stages     │
                                    │ └───────────┘ └─────┬──────┘
                                    │                  1:N │FK
                                                ┌─────────┴────────┐
                                                │ director_cameras │
                                                │ director_chars   │
                                                └──────────────────┘
训练/系统域                      学习域（browser_agent_service 自建）
┌──────────────┐               ┌────────────────┐
│ train_tasks  │               │ learning_topics│◄──┐
├──────────────┤               └───────┬────────┘   │1:N
│ style_tasks  │               ┌───────▼────────┐   │
├──────────────┤               │learning_sessions│──┘
│ model_bench- │               └───────┬────────┘
│    marks     │               ┌───────▼────────┐
├──────────────┤               │ learning_logs  │
│ models       │               ├────────────────┤
├──────────────┤               │learning_settings│
│system_settings│              └────────────────┘
├──────────────┤               知识域（knowledge_service/graph_store/fts_store 自建）
│schedule_     │               ┌────────────────┐   ┌────────────┐
│   history    │               │ knowledge_meta │──►│knowledge_fts│(FTS5)
├──────────────┤               └───┬────────┬───┘   └────────────┘
│ behavior_logs│                   │kid     │kid
├──────────────┤               ┌───▼────┐ ┌─▼──────┐
│  api_keys    │               │kg_     │ │kg_edges│
└──────────────┘               │entities│ └────────┘
                               └────────┘
```

## 2. 对话域（database.py）

### dialog_sessions — 对话会话

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | 会话 ID |
| title | TEXT | 默认"新对话" |
| model | TEXT | 使用的模型 |
| pinned | INTEGER | 0/1 置顶（DIALOG-010） |
| mode | TEXT | 对话模式 |
| created_at / updated_at | REAL | Unix 时间戳 |

### dialog_messages — 对话消息

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | 消息 ID |
| session_id | TEXT **FK→dialog_sessions** | `ON DELETE CASCADE` |
| role | TEXT | user / assistant |
| content | TEXT | 消息正文（**AES-256-GCM 字段级加密**，见 §6） |
| attachments | TEXT | JSON 数组 |
| model_used | TEXT | 生成模型 |
| rating | INTEGER | 1 赞 / -1 踩 / 0 未评（DIALOG-024） |
| favorite | INTEGER | 0/1 收藏（DIALOG-046） |
| reasoning | TEXT | 深度思考链（深度思考开关，2026-08-31 接线） |
| timestamp | REAL | |

索引：`idx_dialog_messages_session(session_id)`。

## 3. 漫剧核心域（database.py）

### projects — 漫剧项目（级联删除根）

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | |
| name | TEXT | 默认"未命名项目"，重命名查重 |
| path | TEXT | |
| work_mode | TEXT | `regular`（5 步普通漫剧）/ `narrative`（6 步解说漫剧，G1，v2 迁移） |
| art_style | TEXT | 项目画风绑定（画风库 517 条，`custom:{id}` 建项目即用，2026-08-29） |
| created_at / updated_at | REAL | |

### storyboards — 分镜表

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | |
| project_id | TEXT **FK→projects** | `ON DELETE CASCADE`；一个项目一张表 |
| name | TEXT | |
| created_at / updated_at | REAL | |

### storyboard_rows — 分镜行（业务最宽的表）

上限 200 行（STORYBOARD_MAX_ROWS，2026-08-23 由 50 放宽，配套 10000 字剧本），拆自规格 §3.2 StoryboardRow：

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | |
| storyboard_id | TEXT **FK→storyboards** | `ON DELETE CASCADE` |
| shot_number | INTEGER | 镜号 |
| original_dialogue | TEXT | 台词 |
| description | TEXT | 画面描述 |
| characters / props | TEXT | JSON 数组 |
| scene | TEXT | 场景 |
| voice_id / voice_emotion | TEXT | 音色绑定（默认情感"默认"） |
| director_stage_done | INTEGER | 导演台完成标记 |
| generation_status | TEXT | `pending/generating/done/error` |
| is_ai_generated | INTEGER | |
| sort_index | INTEGER | 拖拽排序持久化（R2-B06，reorder 端点逐行重写） |
| camera_type / camera_angle / camera_movement | TEXT | 镜头 8 枚举 / 角度 5 枚举 / 运动 9 枚举 |
| duration | REAL | 1~60 秒 |
| transition | TEXT | 转场 6 枚举 |
| speed / volume | REAL | 配音语速 0.5~2.0 / 音量 -12~0 dB |
| music_path | TEXT | 配乐路径 |
| asset_id | TEXT | 单资产绑定（旧列） |
| asset_ids | TEXT | JSON 数组多资产绑定（竞品对齐） |
| is_locked | INTEGER | 行锁定：批量操作跳过 |

### comic_assets — 资产图

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | |
| project_id | TEXT | 应用层关联（无 FK） |
| kind | TEXT | character / scene / prop |
| name / file_path / prompt | TEXT | 四视图资产 file_path 指向拼合图 |
| meta | TEXT | JSON（尺寸/种子/子视图/history 留痕） |
| created_at | REAL | |

### art_styles — 漫画风格包（database.py `_SCHEMA`，2026-08-28 校准补记）

此前本文档漏记。现 **6 列**：id TEXT PK / name TEXT / prompt TEXT / created_at REAL / **pack / pack_def**（2026-08-31 风格卡↔风格包显式绑定：500 卡族回填，resolve_route(pack_id/pack_def) 覆写嗅探；自定义风格必须导入风格包 JSON）。
供漫剧漫画项目（api/manga/comic.py）查询与新增风格预设。

### keyframes — 关键帧（多版本）

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | |
| row_id | TEXT | 应用层关联 storyboard_rows |
| project_id | TEXT | 冗余，级联删除用 |
| version | INTEGER | v1 起，regenerate 递增 |
| file_path / prompt | TEXT | |
| status | TEXT | generating / done / error |
| error | TEXT | |
| is_current | INTEGER | 当前版本标记（rollback 切换，delete 当前版自动回退上一版） |
| created_at | REAL | |
| shot_seeds | TEXT | 按镜位种子记录（确定性生成 0 diff 的凭证） |
| consistency | TEXT | 一致性标定记录（按镜位阈值 0.58/0.82/0.75、相似度实测值） |

### scene_objects — 3D 场景对象

`id` 为 `project_id + object_id` 合成主键；position/rotation/scale 存 JSON `{x,y,z}`（COMIC-090，导演台 Transform 持久化）。

### 导演台三表

| 表 | 父表（FK 级联） | 关键列 |
|----|----------------|--------|
| director_stages | storyboards | panorama_path（全景图）、scene_id、name |
| director_cameras | director_stages | position/rotation JSON、fov（默认 60）；4合1 截图须恰好 4 机位 |
| director_characters | director_stages | position/rotation/scale、locked（占位锁定） |

### voice_profiles — 音色

character_id 应用层关联角色；is_preset 预置音色首启种子化；emotion 默认/愤怒/悲伤；file_path 指向上传/克隆音频。

### video_tasks — 视频任务

| 列 | 类型 | 说明 |
|----|------|------|
| id | TEXT PK | |
| storyboard_row_id | TEXT | 应用层关联分镜行 |
| description / screenshot_4in1 | TEXT | 生成描述 / 4合1 截图（base64 或路径） |
| character_assets | TEXT | JSON 数组 |
| resolution / fps / duration_seconds / codec | — | 1080p 默认 / 24 默认 / ≤20s（VIDEO_MAX_DURATION）/ h264 默认 |
| model_override / model_used | TEXT | |
| status | TEXT | `pending/generating/done/error/cancelled`（前端枚举一致性测试守护） |
| progress | REAL | 后台工作线程真实写入 |
| file_path / generation_time_ms / has_audio_sync | — | 产物与耗时 |
| created_at / updated_at | REAL | |

## 4. 训练与系统域

### train_tasks — LoRA 训练任务（database.py）

base_model / lora_rank(16) / lora_alpha(32) / learning_rate(1e-4) / epochs(3) / dataset_path / status / progress / **priority**（reorder 端点排序字段，审计 R3-BE1）。

### style_tasks — 风格训练任务（style_lora_service.py 自建）

与 train_tasks 同构，多 style_prompt、dataset_id、version（版本即项目实体）、error 列。

### models — 模型注册表（database.py）

id / name / category（枚举）/ purpose / size_gb / params / min_vram_gb（显存闸门数据源）/ associated_features(JSON) / status(not_installed 默认) / file_path / sha256。

### model_benchmarks — 基准记录（api/models.py 自建）

model_id / engine / runs / tokens_per_s / first_token_ms / total_ms / output_tokens / vram_peak_gb；索引 `(model_id, created_at DESC)`。

### system_settings — 系统设置 KV（database.py）

key 主键，value JSON，updated_at。

### schedule_history — 协同调度历史（database.py）

INTEGER 自增主键；ts 为 **ISO 8601 TEXT**（文档D 规范，与其余表的 REAL 时间戳不同）；mode_before/mode_after/trigger/hysteresis wait_ms/success——调度决策学习数据源。

### behavior_logs — 行为日志（behavior_service.py 自建）

event_type / content / context / before / after（content 等四列 **AES-256-GCM 加密**）/ timestamp / feature；异步批量落库（入队 <10ms）。

### api_keys — API Key（api/system.py 自建）

name / prefix（脱敏前缀）/ key_hash（**bcrypt 哈希**，明文只在创建响应出现一次）/ created_at / last_used_at。

## 5. 学习域与知识域（全部服务层自建）

### 学习四表（browser_agent_service.py）

| 表 | 关系 | 说明 |
|----|------|------|
| learning_settings | 独立 KV | 学习设置（时长/页数/搜索引擎/黑白名单/流量） |
| learning_topics | — | name/keywords(JSON)/status/progress/knowledge_count/source/depth/seed_urls(JSON)/max_pages |
| learning_sessions | topic_id 应用层关联 | goal/budget(JSON)/status/pages_visited/knowledge_extracted/coverage/stop_reason/trigger_type |
| learning_logs | session_id 应用层关联，自增 PK | ts/action/reason/result（会话操作日志） |

### knowledge_meta — 知识元数据（knowledge_service.py）

content / type / title / topic / source_url / quality_score / simhash（去重）/ lifecycle（知识生命周期"青年"起）/ vector_id（ChromaDB 关联键）/ access_count / lang。索引 topic、created_at。

### knowledge_fts — 全文检索（data/fts_store.py）

FTS5 虚拟表 + content 表双结构，与 knowledge_meta 同步维护；语义检索时与向量召回 RRF 融合。

### kg_entities / kg_edges — 知识图谱（data/graph_store.py）

- kg_entities：name UNIQUE、kind、ref_count（引用计数）、时间戳。
- kg_edges：src/dst（实体名）、relation（默认"相关"）、kid（知识条目关联）、weight；`UNIQUE(src,dst,relation,kid)` 防重边。

### paint_history — 绘画历史（paint_engine.py 自建）

task_id PK / prompt / negative / params_json / file_path / seed（-1 表密码学随机）/ favorite / created_at；索引 created_at DESC。服务重启后内存任务表丢失，从此表恢复查询。（2026-09-01 校准：实测另有 favorite 列。）

### flow_executions / flow_nodes — 执行流程追踪（flow_trace.py 自建，**独立库 logs/flow_trace.db**）

- flow_executions（18 列）：flow_id / module / feature / status / friendly / trigger / detail / input_summary / output_summary / error_code / error_detail / started_at / ended_at / duration_ms / last_active / node_count / resource_start / resource_end——flow_id 贯穿一次业务流程，孤儿恢复任务把遗留 running 改写为 orphan。
- flow_nodes（14 列）：自增 id / flow_id / seq / node / module / status / started_at / duration_ms / input_summary / output_summary / detail / friendly / error / resource——流程内节点级明细，供 /logs/flows 泳道图与甘特时间轴。

## 6. 迁移与加密机制

**迁移**（P0-03 版本化机制）：`PRAGMA user_version` 携带版本号，当前 **SCHEMA_VERSION=7**（backend/data/database.py `SCHEMA_VERSION = 7`，2026-08-28 实测；本文档成文时为 3，组 4~7 为后续追加迁移，明细以 `_MIGRATION_GROUPS` 为准）。`_MIGRATION_GROUPS` 按版本分组声明增量列（组1：17 列，含 pinned/rating/favorite/sort_index/priority 等；组2：projects.work_mode——2026-08-14 事故后纳入；组3：无新增列，纯数据迁移）。`_migrate_columns()` 用 `PRAGMA table_info` 判存后 `ALTER TABLE ADD COLUMN`；`_DATA_MIGRATIONS` 声明版本→数据迁移函数映射（v3：`_migrate_encrypt_legacy_fields` 存量明文加密，迁移后 VACUUM 重建库文件清空闲页明文残留）。两条铁律：库版本高于代码版本直接 RuntimeError 拒绝启动（防旧程序写坏新库）；历史组禁止修改，只许追加新组。

**加密**（data/crypto.py，字段级）：AES-256-GCM，密钥 32 字节随机，经 Windows DPAPI（CurrentUser）保护存 `data/keys/dbkey.bin`，非 Windows/DPAPI 不可用回退机器指纹 HKDK。密文格式 `enc:v1:` + base64(nonce12‖ct‖tag16)，无前缀明文原样透传（兼容存量）。加密范围：dialog_messages.content 与 behavior_logs 的 content/context/before/after；知识库正文不加密（FTS5/向量检索依赖明文）。注意这是字段级加密——整库仍是普通 SQLite 文件（P2-05 覆盖范围即此口径）。

**连接模型**：`threading.local` 每线程独立连接 + 全局 `_write_lock` 串行化写；`get_db()` 线程安全单例，`get_db_safe()` 异常返回 None 供 API 层降级内存模拟存储（§4.1 容错降级）。启动前 `PRAGMA quick_check` 自检，损坏则隔离为 `.corrupt-{ts}` 后缀并重建空库（审计 R3-ARCH3）。批量写走 `execute_in_transaction`（BEGIN IMMEDIATE 显式事务，防逐条提交写放大；禁止嵌套调用——会 threading.Lock 死锁）。

## 7. 约定速查

- 时间戳：除 schedule_history.ts（ISO 8601 TEXT）外，全部 REAL Unix 秒。
- JSON 列：TEXT 存 JSON 字符串（characters/props/asset_ids/meta/budget/keywords 等），读取方负责 parse。
- 外键级联链只有两条：projects→storyboards→storyboard_rows / director_stages→{cameras,characters}；comic 项目删除端点在应用层额外级联资产/关键帧/视频任务/场景对象。
- 软删除不存在——删除即物理删除，关键帧靠多版本结构天然保留历史。
