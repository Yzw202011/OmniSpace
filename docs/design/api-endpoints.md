# OmniSpace AI API 端点总表

> 版本 v2.3.1 ｜ 生成于 2026-08-20（TASK-P2-02，对应审计 P03）｜ 机器提取自 FastAPI 应用实例（create_app() 路由表全量枚举，非手工誊抄）
>
> **2026-08-28 时效校准**：本表为 08-20 快照。同口径静态扫描当日共 **328 个 HTTP 路由装饰器（含别名）** + 4 WebSocket（WS 数不变）；新增端点以代码为准，本表未逐条补录。
> **2026-09-02 复核**：当前静态实测 **319 装饰器 + 15 模块**（license 入列；08-29 剔除 3D 导演台路由）。⚠️ 本表所列 `/api/v1/director/*` 端点已随 3D 导演台整链路剔除（2026-08-29 裁定），TripoSR 权重亦已隔离——相关行仅作 08-20 快照历史。
> **2026-09-16 复核**：静态实测 **334 装饰器**（含别名）+ 3 函数式注册（comic_asset generate-character/scene/prop）+ 2 app 级工具端点；期间增量=B3 死端点清退（-25/-40 总量）、zviews regenerate-view、Z-Image 引擎槽走既有管线无新路由。死端点治理现状：硬死 4 + 归档半死 27，最大清退候选=旧绘画栈别名族 ~22 条（前端已全面迁 /comic/asset/*）。⚠️ 08-20 的机器提取脚本未入库（一次性会话产物），本节为 AST 静态枚举口径；全表重生成待工具补档。
>
> 配套文档：[架构总览](architecture-overview.md) ｜ [数据库 ER 说明](database-er.md)

共 **270 个 HTTP 端点**（08-20 快照口径；2026-08-28 实测 328 含别名）+ **4 个 WebSocket 端点**，按业务域分 16 组。表中每行含处理函数名——需要细节时按函数名在 `backend/api/` 下直接定位。

## 通用约定

- **前缀**：所有业务端点挂载于 `/api/v1` 之下（ADR-03）。旧 `/v1` 前缀已废弃，返回 404。
- **信封**：响应恒为 HTTP 200 + 统一信封 `{"success", "data", "error", "meta"}`（ADR-01）。失败时 `error` 为 `{code, message, detail, suggestion?}`，`code` 是语义串（`MODEL_*`/`KNOWLEDGE_*`/`SYSTEM_*`/`FEATURE_*` 等约 100 个，历史数字码自动映射）。
- **别名约定**：漫剧域端点存在 23 个顶层旧别名（如 `/api/v1/storyboard/list` ≡ `/api/v1/manga/storyboard/list`），系 v2.1 契约兼容保留。本表只列权威 `/manga/*` 路径；前端代码统一使用权威路径。`/api/v1/director/text-to-3d` 例外——它只注册在顶层，无 `/manga` 前缀对。
- **限流**：默认 300 req/min/端点（2026-08-28 校准，config.yaml `rate_limit: 300`；由 100 提额——100 会把多标签页合法轮询打出 429。滑动窗口，路径参数归一化为 `{}` 后按端点计桶）；`/health` 与 `/favicon.ico` 豁免；`GET /api/v1/manga/media/*` 独立 600/min 桶（缩略图高频回读）。超限 429 + `SYSTEM_RATE_LIMITED` + Retry-After 头。
- **绑定**：127.0.0.1:5800，Host 头白名单校验（防 DNS 重绑定），CORS 仅放行 localhost 任意端口。
- **上传**：上传端点经 `middleware/upload_guard.py` 双层闸门——扩展名白名单 + 文件头魔数嗅验，PE/ELF/Mach-O 可执行体绝对黑名单（P0-03）。
- **功能互斥**：dialog / paint / video_gen / training 四类重量级功能经 `middleware/feature_lock.py` 进程内锁全互斥，冲突返回 `FEATURE_MUTEX_LOCKED`；GPU ≥90°C 拒绝新任务。

## WebSocket 端点（4 个）

| 路径 | 位置 | 说明 |
|------|------|------|
| `/ws` | main.py → ws_hub | 通用消息中枢：15 种服务端消息类型白名单（task_progress / task_complete / system_status / vram_warning / download_progress / quality_degraded 等），广播器由 draw/lora/agent 注入 |
| `/api/v1/hardware/realtime` | api/hardware.py:330 | 硬件实时遥测，每 2s 推 system_status |
| `/api/v1/learn/session/progress` | api/learning.py:620 | 学习会话进度，每 2s 推 learn_progress |
| `/api/v1/dialog/stream/{session_id}` | main.py | **已废弃（F-011）**——新前端走 `POST /api/v1/chat/stream` SSE |

所有 WS 端点 accept 前先过 `ws_origin_guard()`（防 CSWSH，非本地 Origin 直接 close(1008)）。

## 对话域（chat 15 + dialog 6）

`chat/*` 是 v2.3 前端主用入口（会话管理 + SSE 流式）；`dialog/*` 是规格 §4.2 基础端点（v2.1 契约保留）。

### /chat（前端主入口）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/chat/send` | `dialog_send` | 发送对话消息（真实推理） |
| `POST` | `/api/v1/chat/stream` | `chat_stream` | SSE 流式对话（F-011） |
| `POST` | `/api/v1/chat/stop` | `chat_stop` | 停止指定会话进行中的流式生成 |
| `POST` | `/api/v1/chat/clear` | `chat_clear` | 清空对话历史 |
| `GET` | `/api/v1/chat/history` | `chat_history` | 分页对话历史 |
| `GET` | `/api/v1/chat/favorites` | `chat_list_favorites` | 收藏夹列表（时间倒序） |
| `GET` | `/api/v1/chat/sessions` | `chat_list_sessions` | 会话列表（置顶优先，支持标题/内容搜索） |
| `POST` | `/api/v1/chat/sessions` | `chat_create_session` | 创建新会话 `{title?, mode?}` |
| `GET` | `/api/v1/chat/sessions/{session_id}` | `chat_get_session` | 会话详情 + 全部消息 |
| `PUT` | `/api/v1/chat/sessions/{session_id}` | `chat_update_session` | 重命名 / 置顶 / 切换模式 |
| `DELETE` | `/api/v1/chat/sessions/{session_id}` | `chat_delete_session` | 删除会话及其消息 |
| `GET` | `/api/v1/chat/sessions/{session_id}/messages` | `chat_list_messages` | 会话消息分页（时间升序） |
| `DELETE` | `/api/v1/chat/sessions/{session_id}/messages` | `chat_clear_messages` | 清空消息但保留会话 |
| `POST` | `/api/v1/chat/sessions/{sid}/messages/{mid}/rating` | `chat_rate_message` | 消息评分（1 赞 / -1 踩 / 0 取消） |
| `POST` | `/api/v1/chat/sessions/{sid}/messages/{mid}/favorite` | `chat_favorite_message` | 收藏切换 |

### /dialog（规格 §4.2 契约）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/dialog/send` | `dialog_send` | 发送对话消息（与 chat/send 同实现） |
| `GET` | `/api/v1/dialog/history` | `dialog_history` | 会话历史消息（最近 limit 条） |
| `GET` | `/api/v1/dialog/sessions` | `dialog_sessions` | 会话列表 |
| `POST` | `/api/v1/dialog/sessions` | `dialog_create_session` | 创建新会话 |
| `DELETE` | `/api/v1/dialog/sessions/{session_id}` | `dialog_delete_session` | 删除会话 |
| `GET` | `/api/v1/dialog/status` | `dialog_status` | 对话引擎状态（模型可用性/显存/首 token 统计） |

## 绘画域（draw 14 + paint 12）

`draw/*` 与 `paint/*` 大量共享处理函数（paint 是规格 §7.1 契约路径）。生成类端点均为异步任务：立即返回 task_id，经 result 端点轮询。

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/draw/generate` ≡ `/api/v1/paint/generate` | `draw_generate` | 文生图（异步任务） |
| `POST` | `/api/v1/draw/img2img` ≡ `/api/v1/paint/img2img` | `draw_img2img` | 图生图（init_image base64 + strength 0.05~1.0） |
| `POST` | `/api/v1/paint/inpaint` ≡ `/api/v1/art/inpaint` | `art_inpaint` | 局部重绘（异步任务） |
| `POST` | `/api/v1/draw/upscale` ≡ `/api/v1/paint/upscale` | `draw_upscale` | 图像超分 `{"image": base64, "scale": 2\|4}` |
| `GET` | `/api/v1/draw/result/{task_id}` ≡ `/api/v1/paint/result/{task_id}` | `draw_result` | 任务状态查询，done 返回图片 base64 + 路径 |
| `GET` | `/api/v1/draw/status` | `draw_status` | 绘画引擎状态 |
| `GET` | `/api/v1/draw/models` | `draw_models` | 绘画模型列表（路由表 + 本地实际可用状态） |
| `GET` | `/api/v1/draw/image/{filename}` | `draw_image` | 按文件名回读生成图片 |
| `POST` | `/api/v1/draw/controlnet/preview` | `controlnet_preview` | 预览 ControlNet 条件图（诚实降级版） |
| `GET` | `/api/v1/draw/queue` ≡ `/api/v1/paint/queue` | `paint_queue` | 任务队列快照（等待 + 运行中） |
| `POST` | `/api/v1/draw/task/{task_id}/cancel` ≡ `/api/v1/paint/task/{task_id}/cancel` | `paint_task_cancel` | 取消任务（协作式） |
| `POST` | `/api/v1/paint/task/{task_id}/priority` | `paint_task_priority` | 调整排队优先级 `{"priority": 0~9}` |
| `GET` | `/api/v1/draw/history` ≡ `/api/v1/paint/history` | `draw_history` | 生成历史（时间倒序分页） |
| `DELETE` | `/api/v1/{draw,paint}/history/{task_id}` | `paint_history_delete` | 删除单条历史（记录 + 图文件） |
| `POST` | `/api/v1/{draw,paint}/history/batch-delete` | `paint_history_batch_delete` | 批量删除（上限 200） |
| `POST` | `/api/v1/{draw,paint}/history/{task_id}/favorite` | `paint_history_favorite` | 收藏切换/设置 |

## 漫剧域（manga 43 + comic 27 + director 1，共 71 权威端点）

对应前端 `#/storyboard` 漫剧工作台全流程。源码在 `backend/api/manga/` 包（TASK-P2-01 拆分，各子域独立模块）。

### 分镜表（storyboard.py）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/manga/storyboard` | `storyboard_create` | 创建分镜表（为项目初始化空表） |
| `GET` | `/api/v1/manga/storyboard/list` | `storyboard_list` | 分镜行列表（按 sort_index 升序；须注册在 `{project_id}` 之前防路径吞噬） |
| `GET` | `/api/v1/manga/storyboard/{project_id}` | `storyboard_get` | 获取分镜表（项目不存在 40005；空表返回空 rows） |
| `PUT` | `/api/v1/manga/storyboard/{project_id}` | `storyboard_save` | 全量保存（覆盖增/删/重排，前端保存按钮/自动保存） |
| `PUT` | `/api/v1/manga/storyboard/{pid}/rows/{row_id}` | `storyboard_row_update` | 更新单行（仅非空字段） |
| `POST` | `/api/v1/manga/storyboard/{project_id}/auto-split` | `storyboard_auto_split` | AI 自动分镜（剧本文本拆行追加） |
| `POST` | `/api/v1/manga/storyboard/import` | `storyboard_import` | 导入剧本（按行拆分追加） |
| `GET` | `/api/v1/manga/storyboard/{project_id}/export` | `storyboard_export` | 导出分镜表 csv/json/png-seq/pdf |
| `POST` | `/api/v1/manga/storyboard/reorder` | `storyboard_reorder` | 拖拽重排（按 row_ids 顺序重写 sort_index 落库） |
| `POST` | `/api/v1/manga/storyboard/ai-describe` | `storyboard_ai_describe` | AI 画面描述（引擎未就绪时 DIALOG_NOT_READY 诚实错误） |
| `POST` | `/api/v1/manga/storyboard/preview` | `storyboard_preview` | 分镜预览图（绘画引擎生成；不可用时降级占位图） |
| `POST` | `/api/v1/manga/storyboard/emotion-detect` | `storyboard_emotion_detect` | 台词情绪识别（LLM/规则词典分级） |

### 关键帧（keyframe.py）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/manga/keyframe/generate` | `keyframe_generate` | 生成关键帧（行描述 → SDXL → 新版本登记；出图 2560×1440 铁律） |
| `POST` | `/api/v1/manga/keyframe/batch` | `keyframe_batch` | 批量生成（逐行串行，聚合成功/失败明细） |
| `POST` | `/api/v1/manga/keyframe/regenerate` | `keyframe_regenerate` | 重生成（产出 v{n+1}，旧版保留可回退） |
| `POST` | `/api/v1/manga/keyframe/rollback` | `keyframe_rollback` | 版本回退（指定版本置为当前） |
| `GET` | `/api/v1/manga/keyframe/list` | `keyframe_list` | 行关键帧版本列表（版本倒序） |
| `DELETE` | `/api/v1/manga/keyframe/{keyframe_id}` | `keyframe_delete` | 删除版本；删当前版本自动回退上一版 |
| `POST` | `/api/v1/manga/story/keyframe` | `story_keyframe_generate` | 故事生图（解说漫剧第 4 步，跨分镜一致性风格） |

### 漫画项目与剧本（comic.py）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/comic/project/create` | `comic_project_create` | 创建项目（template=comic_drama 预置 5 行分镜） |
| `GET` | `/api/v1/comic/project/list` | `comic_project_list` | 项目列表（更新时间倒序） |
| `PUT` | `/api/v1/comic/project/{project_id}` | `comic_project_update` | 重命名（重名报错） |
| `DELETE` | `/api/v1/comic/project/{project_id}` | `comic_project_delete` | 级联删除（分镜/行/视频任务/资产/关键帧/场景对象） |
| `POST` | `/api/v1/comic/script/import-dsl` | `comic_script_import_dsl` | DSL 剧本上传导入（multipart，.txt/.dsl ≤10MB） |
| `GET` | `/api/v1/comic/scene/object/list` | `comic_scene_object_list` | 3D 场景对象列表 |
| `PUT` | `/api/v1/comic/scene/object/update` | `comic_scene_object_update` | Transform 持久化（upsert） |
| `POST` | `/api/v1/comic/export/bundle` | `comic_export_bundle` | 项目合并打包（分镜 json/csv + 资产 + 视频 → 单 zip） |

### 资产库（comic_asset.py + comic_gen.py 管线）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/comic/asset/library` | `comic_asset_library` | 资产清单（按项目/类型过滤，含缩略图） |
| `POST` | `/api/v1/comic/asset/generate-character` | `_handler` | 角色资产生成（工厂端点，kind=character） |
| `POST` | `/api/v1/comic/asset/generate-scene` | `_handler` | 场景资产生成（工厂端点，kind=scene） |
| `POST` | `/api/v1/comic/asset/generate-prop` | `_handler` | 道具资产生成（工厂端点，kind=prop） |
| `POST` | `/api/v1/comic/asset/generate-turnaround` | `comic_asset_generate_turnaround` | 角色四视图 one-pass（FLUX.2 Klein 中文直入，单图 2560×1440（16:9，1×4 竖格）出四视图 + PIL 中文标注；FLUX.2 不可用回退 SDXL 逐视图并标 degraded） |
| `POST` | `/api/v1/comic/asset/batch-generate` | `comic_asset_batch_generate` | 批量生成（逐项串行，聚合明细） |
| `POST` | `/api/v1/comic/asset/{asset_id}/regenerate` | `comic_asset_regenerate` | 按现有 prompt 重生成覆盖 |
| `POST` | `/api/v1/comic/asset/{asset_id}/regenerate-view` | `comic_asset_regenerate_view` | 单视图重生（四视图资产指定视图） |
| `POST` | `/api/v1/comic/asset/infer-entities` | `comic_asset_infer_entities` | 从分镜行推断实体资产桩 |
| `POST` | `/api/v1/comic/asset/upload` | `comic_asset_upload` | 本地图片登记为资产 |
| `POST` | `/api/v1/comic/asset/{asset_id}/reference` | `comic_asset_reference_upload` | 上传 AI 参考图（img2img 用 reference.png） |
| `DELETE` | `/api/v1/comic/asset/{asset_id}/reference` | `comic_asset_reference_delete` | 删除参考图 |
| `GET` | `/api/v1/comic/asset/{asset_id}/history` | `comic_asset_history` | 生成历史（meta.history 留痕，最新在前） |
| `POST` | `/api/v1/comic/asset/{asset_id}/describe` | `comic_asset_describe` | 描述词 AI 扩写（name+kind → 绘图 prompt） |
| `PUT` | `/api/v1/comic/asset/{asset_id}` | `comic_asset_update` | 元信息更新（仅非 None 字段） |
| `PUT` | `/api/v1/comic/asset/bind` | `comic_asset_bind` | 资产 ↔ 分镜行绑定（多资产） |
| `PUT` | `/api/v1/comic/asset/unbind` | `comic_asset_unbind` | 解绑 |
| `POST` | `/api/v1/comic/asset/adopt` | `comic_asset_adopt` | 资产库资产引入当前项目 |
| `POST` | `/api/v1/comic/asset/export-pack` | `comic_asset_export_pack` | 资产打包导出（zip + manifest.json） |

### 导演台（director.py）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/manga/director/panorama` | `director_panorama` | 生成全景图 |
| `POST` | `/api/v1/manga/director/screenshot-4in1` | `director_screenshot_4in1` | 4合1截图（必须恰好 4 机位，否则 70003） |
| `POST` | `/api/v1/manga/director/export` | `director_export` | 导演台导出 |
| `POST` | `/api/v1/manga/director/camera/add` | `director_camera_add` | 添加机位 |
| `PUT` | `/api/v1/manga/director/camera/{camera_id}` | `director_camera_update` | 更新机位（仅非空字段） |
| `POST` | `/api/v1/manga/director/character/position` | `director_character_position` | 更新角色位置 |
| `POST` | `/api/v1/manga/director/character/lock` | `director_character_lock` | 锁定占位 |
| `POST` | `/api/v1/manga/director/character/unlock` | `director_character_unlock` | 解锁占位 |
| `POST` | `/api/v1/director/text-to-3d` | `director_text_to_3d` | 文生 3D（SDXL 概念图 → TripoSR 网格；注意顶层路径无 /manga 前缀） |

### 视频与叙事（video.py）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/manga/video/generate` | `video_generate` | 生成视频（后台线程真实产出；持有 video_gen 功能锁） |
| `GET` | `/api/v1/manga/video/{task_id}/status` | `video_status` | 状态轮询（真实进度，status 枚举 pending/generating/done/error/cancelled） |
| `GET` | `/api/v1/manga/video/{task_id}/result` | `video_result` | 生成结果（文件路径 + 下载地址） |
| `GET` | `/api/v1/manga/video/{task_id}/download` | `video_download` | 下载视频文件（流式） |
| `POST` | `/api/v1/manga/video/{task_id}/cancel` | `video_cancel` | 取消在途任务 |
| `GET` | `/api/v1/manga/video/tasks` | `video_task_list` | 项目任务列表（JOIN 分镜行取镜号） |
| `POST` | `/api/v1/manga/video/narrative` | `video_narrative_generate` | 视频生词（解说漫剧第 5 步） |
| `POST` | `/api/v1/manga/story/narrative` | `story_narrative_generate` | 故事生词（解说漫剧第 3 步，跨分镜聚合） |
| `GET` | `/api/v1/manga/models/available` | `list_available_models` | 按任务类型的可用本地模型清单（G2 工序弹窗数据源） |
| `GET` | `/api/v1/manga/media/{relpath:path}` | `manga_media` | 媒体回读（白名单 comic_assets/keyframes/generated/exports + 防路径穿越；限流 600/min） |

### 音色（voice.py）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/manga/voices` | `voices_list` | 音色列表（首启种子化预置音色） |
| `POST` | `/api/v1/manga/voices/bind` | `voices_bind` | 绑定角色与音色（自动解除旧绑定） |
| `PUT` | `/api/v1/manga/voices/{voice_id}/emotion` | `voices_emotion` | 更新情感标签 |
| `POST` | `/api/v1/manga/voices/preview` | `voices_preview` | 试听（base64 音频，可能降级静音占位） |
| `POST` | `/api/v1/manga/voices/upload` | `voices_upload` | 上传自定义音色（wav/mp3/flac/m4a ≤20MB） |
| `POST` | `/api/v1/manga/voices/clone` | `voices_clone` | 音色克隆 |

## 视觉工具域（art，7 端点）

文档E 附录B 随包小模型接线（F-01~F-04），输入 `image` 字段为 base64（允许 data URI 前缀）。权重缺失如实返回 `MODEL_FILE_NOT_FOUND`，不伪造结果。

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/art/image-to-3d` | `art_image_to_3d` | 单图 3D 生成（TripoSR → .glb） |
| `POST` | `/api/v1/art/segment` | `art_segment` | 图像分割（SAM ViT-H，点/框提示 → mask PNG） |
| `POST` | `/api/v1/art/depth` | `art_depth` | 深度估计（MiDaS-small ONNX → 伪彩深度图） |
| `POST` | `/api/v1/art/detect` | `art_detect` | 目标检测（YOLOv8n → 检测框 JSON） |
| `GET` | `/api/v1/art/assets/3d/{filename}` | `art_asset_3d` | 3D 资产下载（仅纯文件名，防穿越） |
| `GET` | `/api/v1/art/tools/status` | `art_tools_status` | 四引擎状态聚合（loaded/ready/degraded 如实上报） |

## 语音域（voice，4 端点）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/voice/transcribe` | `voice_transcribe` | 语音转写（Whisper 真实推理） |
| `POST` | `/api/v1/voice/synthesize` | `voice_synthesize` | 语音合成（voice_id/text/emotion 契约） |
| `GET` | `/api/v1/voice/models` | `voice_models` | 动态发现的语音模型（ready=True 可直接推理） |
| `GET` | `/api/v1/voice/status` | `voice_status` | 引擎状态（TTS 后端链 / ASR / SoVITS 门控） |

## 学习域（learn 47 + knowledge 7 + behavior 4）

`learn/*` 组内含大量契约别名端点（标注"契约别名"的行是规格 §7.1 路径到既有实现的转发，行为与目标端点完全一致）。

### 学习主题与会话

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/learn/topic/create` | `topic_create` | 创建主题 `{name, keywords?, source?, depth?, seed_urls?}` |
| `GET` | `/api/v1/learn/topic/list` | `topic_list` | 主题列表（关键词/状态过滤） |
| `PUT` | `/api/v1/learn/topic/{topic_id}` | `topic_update` | 更新主题 |
| `DELETE` | `/api/v1/learn/topic/delete` | `topic_delete` | 删除主题 |
| `POST` | `/api/v1/learn/topic/clone` | `topic_clone` | 克隆主题（复制名称/关键词/深度/种子 URL） |
| `POST` | `/api/v1/learn/session/start` | `session_start` | 启动学习会话（后台线程 Agent 循环） |
| `GET` | `/api/v1/learn/session/status` | `session_status` | 会话状态快照（缺省返回活跃/最近会话） |
| `POST` | `/api/v1/learn/session/stop` | `session_stop` | 停止会话（下一拍退出并生成报告） |
| `POST` | `/api/v1/learn/session/pause` | `session_pause` | 暂停会话 |
| `POST` | `/api/v1/learn/session/resume` | `session_resume` | 恢复会话 |
| `GET` | `/api/v1/learn/session/report` | `session_report` | 学习完成报告 |
| `GET` | `/api/v1/learn/session/logs` | `session_logs` | 会话操作日志（`/log` 为契约别名） |

### 知识管理（learn/knowledge/* 与 /knowledge/* 双路径）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/knowledge/import-document` | `knowledge_import_document` | 导入文档（pdf/docx/txt/md，完整管线入话题；learn 路径为契约别名） |
| `POST` | `/api/v1/knowledge/process-text` | `knowledge_process_text` | 直接处理文本（同管线） |
| `GET` | `/api/v1/knowledge/list` | `knowledge_list` | 知识列表（分页 + topic + 关键词） |
| `GET` | `/api/v1/knowledge/{kid}` | `knowledge_detail` | 知识详情 |
| `DELETE` | `/api/v1/knowledge/{kid}` | `knowledge_delete` | 删除知识（级联删向量与元数据） |
| `GET` | `/api/v1/knowledge/stats` | `knowledge_stats` | 库统计（条数/磁盘/主题分布/后端状态） |
| `GET` | `/api/v1/knowledge/graph` | `knowledge_graph` | 知识图谱（全局 Top-N 或指定话题） |
| `GET` | `/api/v1/learn/knowledge/search` | `learn_knowledge_search` | 语义检索（向量 + FTS5 混合，RRF 融合） |
| `GET` | `/api/v1/learn/knowledge/export` | `learn_knowledge_export` | 导出 json/csv（可按 topic 过滤） |
| `POST` | `/api/v1/learn/knowledge/import` | `learn_knowledge_import` | 导入 .json/.csv |
| `PUT` | `/api/v1/learn/knowledge/{kid}` | `learn_knowledge_update` | 编辑条目 |
| `DELETE` | `/api/v1/learn/knowledge/delete` | `learn_knowledge_delete` | 删除（契约路径，单条或批量） |
| `GET` | `/api/v1/learn/knowledge/graph/export` | `learn_knowledge_graph_export` | 图谱导出 gexf/graphml |

### LoRA 训练

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/learn/train` | `learn_train` | 创建训练任务（真实 QLoRA 微调入队；`/lora/train` 为契约别名） |
| `POST` | `/api/v1/learn/dataset/upload` | `learn_dataset_upload` | 上传 JSONL 训练数据 |
| `GET` | `/api/v1/learn/tasks` | `learn_tasks` | 任务列表（时间倒序） |
| `GET` | `/api/v1/learn/tasks/{task_id}` | `learn_task_detail` | 任务详情（实时状态与进度） |
| `POST` | `/api/v1/learn/tasks/{task_id}/cancel` | `learn_task_cancel` | 取消任务 |
| `POST` | `/api/v1/learn/tasks/reorder` | `learn_tasks_reorder` | 调整优先级（train_tasks.priority） |
| `GET` | `/api/v1/learn/training/status` | `learn_training_status` | 训练服务状态（充分性/版本/队列/活跃任务） |
| `GET` | `/api/v1/learn/lora/versions` | `learn_lora_versions` | LoRA 版本列表 |
| `GET` | `/api/v1/learn/lora/versions/compare` | `learn_lora_versions_compare` | 版本对比 `?a=v1&b=v2` |
| `POST` | `/api/v1/learn/lora/rollback` | `learn_lora_rollback` | 版本回滚 |
| `GET` | `/api/v1/learn/models` | `learn_models` | 可训练基础模型列表 |
| `GET/PUT` | `/api/v1/learn/train/defaults` | `train_defaults_get/put` | 训练默认参数（内置 + 用户覆盖合并） |

### 学习分析与其他

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/learn/analysis/trend` | `learn_analysis_trend` | 学习趋势（近 N 天按日聚合） |
| `GET` | `/api/v1/learn/analysis/efficiency` | `learn_analysis_efficiency` | 学习效率（提取率知识点/页） |
| `GET` | `/api/v1/learn/analysis/sources` | `learn_analysis_sources` | 来源分析（按域名聚合） |
| `GET` | `/api/v1/learn/analysis/topic-compare` | `learn_analysis_topic_compare` | 主题对比 |
| `GET/PUT` | `/api/v1/learn/settings` | `learn_settings_get/put` | 学习设置（时长/页数/搜索引擎/黑白名单/流量；`/settings/get`、`/settings/update` 为契约别名） |
| `GET` | `/api/v1/learn/quota` | `learn_quota` | 资源配额快照 |
| `POST` | `/api/v1/behavior/event` | `behavior_event` | 记录行为事件（异步落库 <10ms 返回） |
| `GET` | `/api/v1/behavior/stats` | `behavior_stats` | 行为学习统计（learn 路径为契约别名） |
| `GET` | `/api/v1/behavior/training-pairs` | `behavior_training_pairs` | LoRA 训练数据预览 |
| `POST` | `/api/v1/behavior/clear` | `behavior_clear` | 清空行为学习数据（learn 路径为契约别名） |

## 视频风格域（style，23 端点）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `POST` | `/api/v1/style/upload` | `style_upload` | 上传素材构建训练数据集 |
| `GET` | `/api/v1/style/datasets` | `style_datasets` | 数据集列表 |
| `GET` | `/api/v1/style/datasets/{dataset_id}` | `style_dataset_detail` | 样本统计与充分性判定 |
| `POST` | `/api/v1/style/train` | `style_train` | 触发 LoRA 训练（QLoRA 4bit，P2 优先级入队） |
| `GET` | `/api/v1/style/tasks` | `style_tasks` | 训练任务列表 |
| `GET` | `/api/v1/style/tasks/{task_id}` | `style_task_detail` | 任务详情（实时进度） |
| `POST` | `/api/v1/style/tasks/{task_id}/cancel` | `style_task_cancel` | 取消（队列中直接置 cancelled） |
| `POST` | `/api/v1/style/tasks/{task_id}/pause` | `style_task_pause` | 暂停（epoch 检查点挂起） |
| `POST` | `/api/v1/style/tasks/{task_id}/resume` | `style_task_resume` | 恢复暂停任务 |
| `POST` | `/api/v1/style/tasks/{task_id}/resume-training` | `style_task_resume_training` | 断点续训（复制配置重新入队新任务） |
| `GET` | `/api/v1/style/list` | `style_list` | 风格项目列表（版本即项目实体） |
| `GET` | `/api/v1/style/versions` | `style_versions` | 版本列表（含 meta、是否当前） |
| `PUT` | `/api/v1/style/{version}` | `style_rename` | 重命名项目 |
| `DELETE` | `/api/v1/style/{version}` | `style_delete` | 删除项目（训练中引用拒绝） |
| `GET` | `/api/v1/style/{version}/metrics` | `style_metrics` | 版本质量指标 |
| `POST` | `/api/v1/style/preview` | `style_preview` | 风格预览（应用 LoRA 生成对比帧） |
| `POST` | `/api/v1/style/merge` | `style_merge` | 多 LoRA 权重线性融合 |
| `POST` | `/api/v1/style/rollback` | `style_rollback` | 版本回滚 |
| `POST` | `/api/v1/style/export` | `style_export` | 导出风格包（tar.gz + SHA256） |
| `POST` | `/api/v1/style/clone` | `style_clone` | 克隆风格项目 |
| `GET` | `/api/v1/style/templates` | `style_templates` | 模板列表 |
| `POST` | `/api/v1/style/templates` | `style_template_save` | 保存模板 |
| `GET` | `/api/v1/style/status` | `style_status` | 服务状态（基座/FFmpeg/队列/当前版本） |

## 模型管理域（models，21 端点）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/models` | `models_list` | 模型列表（按类别分组，合并磁盘扫描 downloaded 标记；`/models/list` 为契约别名） |
| `GET` | `/api/v1/models/{model_id}` | `models_detail` | 模型详情（含依赖关系字段） |
| `DELETE` | `/api/v1/models/{model_id}` | `models_delete` | 移除注册（已加载先卸载） |
| `POST` | `/api/v1/models/load` | `models_load` | 加载到 GPU（接线 ModelManager.ensure_loaded，含显存预检） |
| `POST` | `/api/v1/models/unload` | `models_unload` | 从 GPU 卸载 |
| `PUT` | `/api/v1/models/select` | `models_select` | 手动选择模型（feature → model_id 绑定） |
| `POST` | `/api/v1/models/import` | `models_import` | 导入本地模型（校验路径后登记） |
| `POST` | `/api/v1/models/download` | `models_download` | 在线下载（诚实语义：进度真实） |
| `POST` | `/api/v1/models/{model_id}/verify` | `models_verify` | SHA256 校验 |
| `POST` | `/api/v1/models/export` | `models_export` | 导出 .tar.gz + manifest + SHA256 侧车 |
| `POST` | `/api/v1/models/benchmark` | `models_benchmark` | 性能基准（N 次真实推理） |
| `GET` | `/api/v1/models/benchmark/history` | `models_benchmark_history` | 基准历史 |
| `GET/PUT` | `/api/v1/models/config` | `models_config_get/put` | 量化精度偏好 |
| `GET` | `/api/v1/models/status` | `models_status` | 全景状态（GPU + 已加载 + 互斥 + 预测器） |
| `GET` | `/api/v1/models/vram` | `models_vram` | 显存全景（GPU 状态 + 逻辑预留 + 已加载占用） |
| `GET` | `/api/v1/models/health` | `models_health` | 子系统健康检查（契约端点） |
| `GET` | `/api/v1/models/predict` | `models_predict` | ML 预测下一功能（>0.7 给预加载建议） |
| `POST` | `/api/v1/models/usage` | `models_usage` | 记录功能切换事件（预测器学习数据） |
| `GET` | `/api/v1/models/update` | `models_update_check` | 版本更新检查 |

## 硬件域（hardware，4 端点）

探测失败时返回 `available=false` + null 读数，前端显示 `--`，绝不回填假数据（P1-05）。

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/hardware/info` | `hardware_info` | 硬件画像（静态） |
| `GET` | `/api/v1/hardware/realtime` | `hardware_realtime` | 实时遥测（GPU/CPU/RAM/磁盘） |
| `GET` | `/api/v1/hardware/synergy` | `hardware_synergy` | 协同调度聚合状态 |
| `PUT` | `/api/v1/hardware/tier` | `hardware_tier_set` | 手动设置硬件档位（auto 或六档之一） |

## 系统域（system，25 端点）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/system/version` | `system_version` | 版本信息 |
| `GET` | `/api/v1/system/info` | `system_info` | 系统信息聚合 |
| `GET` | `/api/v1/system/update` | `system_update` | 软件更新检查 |
| `GET/PUT` | `/api/v1/system/settings` | `system_settings_get/update` | 系统设置（持久化 system_settings 表） |
| `POST` | `/api/v1/system/restart` | `system_restart` | 重启后端 `{"confirm": "RESTART"}` |
| `POST` | `/api/v1/system/diagnose` | `system_diagnose` | 26 项启动自检（真实探测版） |
| `GET` | `/api/v1/system/disk` | `system_disk` | 磁盘概览（各卷 + 数据目录 + top 大文件） |
| `POST` | `/api/v1/system/backup` | `system_backup` | 备份（设置 JSON + SQLite 副本） |
| `GET/PUT` | `/api/v1/system/backup/config` | `backup_config_get/put` | 自动备份配置（开关 + 间隔） |
| `POST` | `/api/v1/system/export` | `system_full_export` | 全量数据导出（tar.gz + SHA256） |
| `POST` | `/api/v1/system/project/export` | `system_project_export` | 打包 .omnispace 项目归档 |
| `POST` | `/api/v1/system/project/import` | `system_project_import` | 导入项目归档 |
| `GET/PUT` | `/api/v1/system/inference/config` | `inference_config_get/put` | 推理参数（线程数/清缓存策略） |
| `GET/PUT` | `/api/v1/system/network/config` | `network_config_get/put` | 网络（代理/HF 镜像/带宽上限） |
| `GET` | `/api/v1/system/logs` | `system_logs` | 日志分页查询（级别过滤） |
| `PUT` | `/api/v1/system/logs/level` | `logs_level_put` | 运行时日志级别（即时生效） |
| `POST` | `/api/v1/system/logs/cleanup` | `system_logs_cleanup` | 清理早于 keep_days 的日志 |
| `GET` | `/api/v1/system/logs/export` | `system_logs_export` | 导出 backend.log |
| `GET` | `/api/v1/system/apikeys` | `api_keys_list` | API Key 列表（仅脱敏前缀） |
| `POST` | `/api/v1/system/apikeys` | `api_keys_create` | 创建 Key（完整值仅此一次返回，库存 bcrypt 哈希） |
| `DELETE` | `/api/v1/system/apikeys/{key_id}` | `api_keys_delete` | 删除 Key |

## 浏览器域（browser，7 端点）

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/api/v1/browser/status` | `browser_status` | 浏览器状态快照（running/tabs/memory） |
| `GET` | `/api/v1/browser/current-page` | `browser_current_page` | 当前页面信息 |
| `GET` | `/api/v1/browser/tabs` | `browser_tabs` | 标签页列表（上限按硬件档 1~5 自适应） |
| `GET` | `/api/v1/browser/screenshot` | `browser_screenshot` | 页面截图（base64 PNG，前端 2s 轮询） |
| `POST` | `/api/v1/browser/navigate` | `browser_navigate` | 用户手动导航（仅 http/https，黑名单拒绝，≥1s 节流） |
| `POST` | `/api/v1/browser/takeover` | `browser_takeover` | 用户接管（AI 控制暂停） |
| `POST` | `/api/v1/browser/handback` | `browser_handback` | 交还控制权给 AI |

## 根路径端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查（launcher 就绪判定依据：信封内 `data.db == "ok"` 才算数据库可用；注意在根路径，无 /api/v1 前缀） |
| `GET` | `/` | 前端 SPA 入口（frontend/dist 静态托管，Hash Router） |
| `GET` | `/favicon.ico` | 站点图标（限流豁免） |
