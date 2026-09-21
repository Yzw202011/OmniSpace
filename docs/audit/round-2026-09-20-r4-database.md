# 第四轮深审报告（R4）— round-2026-09-20（数据库专项·限额期主审亲扫）

> **元信息**
> - 时间：2026-09-20 13:29 起（账户 5h 限额锁定期 15:59 前重置——**本轮零代理，主审亲自扫**）；基线提交 `f995a99` 无新变化。
> - 本轮主题（用户新增重点）：**数据库层专项深审** + 清算第三轮未竟面前端部分。
> - 全程只读（三库全部 mode=ro 只读探查），本报告为唯一新增文件。

---

## 一、执行摘要（四问回答）

**问1 方向（含数据库）**：数据库层结论=**选型与纪律都对，病在两个具体点**（截图内联 98.6MB + 散建表游离），无方向性问题。SQLite WAL+字段加密+三库分职对单机卖断产品是正解（R2 已判，本轮实证背书：PRAGMA 四件套规范落地、迁移组 append-only 在位、索引覆盖良好）。
**问2 增量**：新 P2×1、P3×4、观察项×2；R3 未竟面前端切片部分清零。
**问3**：无新变化（R2 三层病结论维持；本轮无 ComfyUI/显存新面）。
**问4 总账**：未修 **P1×21、P2≈75、P3≈122**（本轮 +1 P2）。

## 二、数据库专项发现

### P2（1 条，✅ 主审亲测三库只读实证）

**R4-P2-1 `video_tasks.screenshot_4in1` 以 base64 内联存四图合一截图——单列 98.60MB = 主库 137MB 的 72%**
- 实测：90 行视频任务，该列合计 98.60MB（最肥单行 6.36MB）；全库全部表逻辑数据 105MB 中它独占 94%。
- 全链代价：①写入端 `api/manga/video.py:1205` 存前端原始 base64（`data/models.py:912` 注释自认 `# base64`）；②**列表查询拖整列**——`_VIDEO_TASK_COLS`（video.py:99）含该列，detail/list 端点（:1347/:1389/:1530 JOIN）每行拖 MB 级载荷；③每次自动备份 137MB 快照连坐（09-19 单日 5 份=690MB 里 5×98MB 是同一批截图的拷贝）；④WAL/ vacuum 空间放大（空闲页 20.7MB 另计）。
- 讽刺点：**DDL 注释自带修法**（database.py:257 `screenshot_4in1 TEXT DEFAULT '' -- base64 或文件路径`）——文件路径方案预留了没走。
- 修法方向（供拍板）：新任务改存文件（data/keyframes 同款目录+路径列）→ 迁移存量（读出→落盘→改路径→删列内容）→ `VACUUM` 回收（预计主库 137→~38MB，备份体积同降 72%）。

### P3（4 条）与观察项（2 条）

1. **散建表 8 张 DDL 游离迁移体系**（v4 B10 债活体确认）：audit_log/api_keys（system.py）、paint_history（draw.py——与 paint_engine.py 双份 DDL=已知 P3-38）、model_benchmarks（models.py）、learning 四表（browser_agent_service.ensure_learning_tables）、训练两张（char_lora/style）——全部不在 database.py `_SCHEMA`/迁移组内，加列即旧库 no such column 的温床（P1-9 恢复重迁问题会放大它们）。
2. **11 张零行表**：director_stages/cameras/characters（3D 已剔除的死表，连索引一起空转）、novel 全家 5 表零行（小说模块无真实使用痕迹或已清库——产品面数据信号，供方向参考）、art_styles（按 09-02 拍板清空=正常）、api_keys（功能未用过）。建议：director 三表随下一次迁移组下线（历史组禁改只许追加→新组 DROP），novel 观察不动。
3. **novel_* 4 个索引空转**（零行表上的索引，无害但属死重，随表处理）。
4. **绕锁直连清单复核**：`sqlite3.connect` 出数据层共 6 处——data_prune.py（已知违纪 P3-44 在案）、system.py（sqlite3 backup API 热备=合法）、flow_trace.py（独立库设计内）、recovery.py（恢复链设计内）、**model_manager/predictor.py（新查证：连的是自己的第三库 model_usage.db，WAL+降级内存缓冲，设计内合规，排除嫌疑）**。
5. 观察项①：learning_logs 25,620 行为最大表（30 天修剪在位，规模健康），但 dialog_messages 仅 87 行/9 会话——**真实对话使用量极低**，与「对话是第一入口」的产品叙事有落差（数据信号，供产品判断）。
6. 观察项②：flow_trace 库 19MB（孤儿污染旧案在册）；model_usage.db 16KB（第三库轻量健康）。

### 复核无恙面（数据库）

- **PRAGMA 四件套规范落地**：foreign_keys=ON / WAL / busy_timeout / synchronous / cache_size 全部按 config 键驱动（database.py:458-465）✅
- **索引覆盖良好**：28 索引对全部 FK 热查询（dialog_messages.session / storyboard_rows.sb / keyframes.row / video_tasks.row / comic_assets.project / learning_logs.session 等）一一在位，未见缺失索引的热 WHERE ✅
- **三库分职清晰**：主库（业务）/flow 库（遥测隔离避主库写锁）/model_usage 库（用量事件流）——职责不重叠 ✅
- **迁移组 append-only 在位**（顺序问题=P2-21 旧案，机制本身健康）；FTS5 结构正常（手动同步之弊=P2-15 旧案）；损坏探测隔离（PRAGMA quick_check 探针）在位 ✅

## 三、R3 未竟面清算（本轮完成部分）

- **前端 manga 切片余部 ✅**：keyframeSlice（27 行缓存，无问题）、rowsSlice（109 行——**好范式**：reorder 乐观更新+失败回滚重拉+错误分级；fetchRows 属已立案 P1-10 竞态族成员不另立）、assetSlice（45 行——**好范式**：绑定「以响应为唯一真源，不合并局部推测」）、videoPoller（R3 已核）。切片层整体质量好。
- **仍未竟（低风险，限额重置后可续）**：config 注释级默认值深对、克隆族谱剩余六族、tests/flow 逐文件断链核查、DialogView/DialogPage 全文、小 store 全文（useLearn/useLearning/useStyle/useHardware/useSystemHealth/useNotification/useTask/useWarmup）。

## 四、总账（截至第四轮）

未修 **P1×21**（R3 清单不变）｜P2≈75（+R4-P2-1）｜P3≈122｜文档漂移 16+9 不变。今日修复入库仍 12 笔全无回归。修复梯队增补：R4-P2-1 截图外迁应与「自动备份明文化（N-P2-2）」**同批做**——先外迁再加密，备份体积直接从 137MB 降到 ~40MB，一举两得。

---

> **纪律声明**：本轮零代理（账户限额）、零写盘（三库只读探查 mode=ro）、零进程操作；所有数字（98.60MB/90 行/28 索引/25,620 行等）均为主审亲测输出。
