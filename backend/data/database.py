"""OmniSpace AI v2.3.1 SQLite 数据库管理（规格 §7 数据库 / §14 约束1 本地存储）。

使用标准库 sqlite3 明文存储（诚实标注：未启用 SQLCipher 全库加密——
本地单机场景按规格 §14 约束1 仅绑定 127.0.0.1，数据不出本机；
历史 vendor/sqlcipher3 shim 与 DB_ENCRYPTION 配置为死代码，已移除）。
开启 WAL 模式提升并发读写。提供单例 get_db() 与 query / sql / insert / update / delete 方法。

规格引用：
  - §7 database.path / wal_mode / busy_timeout_ms
  - §14 约束1：所有数据本地存储，不上云
  - §4.1 分层架构：数据层
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from ..config import DB_BUSY_TIMEOUT, DB_PATH, DB_WAL_MODE

log = logging.getLogger("omnispace.db")

T = TypeVar("T")

# ═══════════════════════════════════════════════════════════════════
#  建表 DDL（规格 §3.2 数据模型 → 13 张业务表）
# ═══════════════════════════════════════════════════════════════════

_SCHEMA = """
-- 对话会话
CREATE TABLE IF NOT EXISTS dialog_sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新对话',
    model       TEXT DEFAULT '',
    pinned      INTEGER NOT NULL DEFAULT 0, -- 0/1 置顶（DIALOG-010）
    mode        TEXT DEFAULT '',            -- 对话模式
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);

-- 对话消息
CREATE TABLE IF NOT EXISTS dialog_messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,          -- user | assistant
    content     TEXT NOT NULL DEFAULT '',
    attachments TEXT DEFAULT '[]',      -- JSON 数组
    model_used  TEXT DEFAULT '',
    rating      INTEGER NOT NULL DEFAULT 0, -- 1 赞 / -1 踩 / 0 未评（DIALOG-024）
    favorite    INTEGER NOT NULL DEFAULT 0, -- 0/1 收藏（DIALOG-046）
    timestamp   REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES dialog_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dialog_messages_session ON dialog_messages(session_id);

-- 模型注册表
CREATE TABLE IF NOT EXISTS models (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    category            TEXT NOT NULL,      -- ModelCategory 枚举值
    purpose             TEXT DEFAULT '',
    size_gb             REAL DEFAULT 0,
    params              TEXT DEFAULT '',
    min_vram_gb         REAL DEFAULT 0,
    associated_features TEXT DEFAULT '[]',  -- JSON 数组
    status              TEXT DEFAULT 'not_installed',
    file_path           TEXT DEFAULT '',
    sha256              TEXT DEFAULT ''
);

-- 漫剧项目
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '未命名项目',
    path        TEXT DEFAULT '',
    work_mode   TEXT NOT NULL DEFAULT 'regular',  -- regular(5步) | narrative(6步解说)
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);

-- 分镜表（一个项目一张分镜表）
CREATE TABLE IF NOT EXISTS storyboards (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    name        TEXT DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_storyboards_project ON storyboards(project_id);

-- 分镜行（规格 §3.2 StoryboardRow，上限 50 行）
CREATE TABLE IF NOT EXISTS storyboard_rows (
    id                  TEXT PRIMARY KEY,
    storyboard_id       TEXT NOT NULL,
    shot_number         INTEGER NOT NULL DEFAULT 0,
    original_dialogue   TEXT DEFAULT '',
    description         TEXT DEFAULT '',
    characters          TEXT DEFAULT '[]',   -- JSON 数组
    scene               TEXT DEFAULT '',
    props               TEXT DEFAULT '[]',   -- JSON 数组
    voice_id            TEXT DEFAULT '',
    voice_emotion       TEXT DEFAULT '默认',
    director_stage_done INTEGER NOT NULL DEFAULT 0,
    generation_status   TEXT DEFAULT 'pending',
    is_ai_generated     INTEGER NOT NULL DEFAULT 0,
    sort_index          INTEGER NOT NULL DEFAULT 0,
    camera_type         TEXT DEFAULT '',     -- 镜头类型 8 枚举（特写/近景/中景/全景/远景/俯拍/仰拍/主观镜头）
    camera_angle        TEXT DEFAULT '',     -- 拍摄角度 5 枚举
    camera_movement     TEXT DEFAULT '',     -- 镜头运动 9 枚举
    duration            REAL DEFAULT 0,      -- 分镜时长（秒，1~60）
    transition          TEXT DEFAULT '',     -- 转场类型 6 枚举
    speed               REAL DEFAULT 1.0,    -- 配音语速 0.5~2.0
    volume              REAL DEFAULT 0.0,    -- 配音音量 dB -12~0
    music_path          TEXT DEFAULT '',     -- 配乐音频文件路径
    asset_id            TEXT DEFAULT '',     -- 绑定资产 id（comic_assets，旧列单值）
    asset_ids           TEXT DEFAULT '[]',   -- JSON 数组：多资产绑定（竞品对齐）
    is_locked           INTEGER NOT NULL DEFAULT 0, -- 行锁定：批量操作跳过
    FOREIGN KEY (storyboard_id) REFERENCES storyboards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_storyboard_rows_sb ON storyboard_rows(storyboard_id);

-- 漫剧资产图（角色/场景/道具，批 1.4）
CREATE TABLE IF NOT EXISTS comic_assets (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'character',  -- character/scene/prop
    name        TEXT DEFAULT '',
    file_path   TEXT DEFAULT '',
    prompt      TEXT DEFAULT '',
    meta        TEXT DEFAULT '{}',   -- JSON（尺寸/种子/子视图等）
    created_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comic_assets_project ON comic_assets(project_id);

-- 关键帧（分镜行 → 生成图，多版本，批 1.6）
CREATE TABLE IF NOT EXISTS keyframes (
    id          TEXT PRIMARY KEY,
    row_id      TEXT NOT NULL,
    project_id  TEXT DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 1,
    file_path   TEXT DEFAULT '',
    prompt      TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'done',   -- generating/done/error
    error       TEXT DEFAULT '',
    is_current  INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_keyframes_row ON keyframes(row_id);

-- 3D 场景对象 Transform 持久化（批 1.7，COMIC-090）
CREATE TABLE IF NOT EXISTS scene_objects (
    id          TEXT PRIMARY KEY,    -- project_id + object_id 合成
    project_id  TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    name        TEXT DEFAULT '',
    position    TEXT DEFAULT '{}',   -- JSON {x,y,z}
    rotation    TEXT DEFAULT '{}',
    scale       TEXT DEFAULT '{}',
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scene_objects_project ON scene_objects(project_id);

-- 导演台场景
CREATE TABLE IF NOT EXISTS director_stages (
    id              TEXT PRIMARY KEY,
    storyboard_id   TEXT NOT NULL,
    scene_id        TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    panorama_path   TEXT DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (storyboard_id) REFERENCES storyboards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_director_stages_sb ON director_stages(storyboard_id);

-- 导演台机位（规格 §3.2 CameraAdd / 4合1截图需恰好4个）
CREATE TABLE IF NOT EXISTS director_cameras (
    id          TEXT PRIMARY KEY,
    stage_id    TEXT NOT NULL,
    name        TEXT DEFAULT '',
    position    TEXT DEFAULT '{}',   -- JSON {x,y,z}
    rotation    TEXT DEFAULT '{}',   -- JSON {x,y,z}
    fov         INTEGER DEFAULT 60,
    FOREIGN KEY (stage_id) REFERENCES director_stages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_director_cameras_stage ON director_cameras(stage_id);

-- 导演台角色（规格 §3.2 CharacterPositionUpdate / CharacterLock）
CREATE TABLE IF NOT EXISTS director_characters (
    id              TEXT PRIMARY KEY,
    stage_id        TEXT NOT NULL,
    character_id    TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    position        TEXT DEFAULT '{}',
    rotation        TEXT DEFAULT '{}',
    scale           REAL DEFAULT 1.0,
    locked          INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (stage_id) REFERENCES director_stages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_director_chars_stage ON director_characters(stage_id);

-- 音色配置（规格 §3.2 VoiceProfile）
CREATE TABLE IF NOT EXISTS voice_profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    character_id TEXT DEFAULT '',
    is_preset   INTEGER NOT NULL DEFAULT 0,
    file_path   TEXT DEFAULT '',
    emotion     TEXT DEFAULT '默认',
    created_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_voice_profiles_char ON voice_profiles(character_id);

-- 视频生成任务（规格 §3.2 VideoGenerateRequest / VideoGenResult）
CREATE TABLE IF NOT EXISTS video_tasks (
    id                  TEXT PRIMARY KEY,
    storyboard_row_id   TEXT NOT NULL,
    description         TEXT DEFAULT '',
    screenshot_4in1     TEXT DEFAULT '',     -- base64 或文件路径
    character_assets    TEXT DEFAULT '[]',   -- JSON 数组
    audio_path          TEXT DEFAULT '',
    resolution          TEXT DEFAULT '1080p',
    fps                 INTEGER DEFAULT 24,
    duration_seconds    REAL DEFAULT 5.0,
    codec               TEXT DEFAULT 'h264',
    model_override      TEXT DEFAULT '',
    model_used          TEXT DEFAULT '',
    status              TEXT DEFAULT 'pending',
    progress            REAL DEFAULT 0.0,
    file_path           TEXT DEFAULT '',
    generation_time_ms  INTEGER DEFAULT 0,
    has_audio_sync      INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_video_tasks_row ON video_tasks(storyboard_row_id);

-- 训练任务（规格 §3.2 TrainTaskCreate / TrainTask）
CREATE TABLE IF NOT EXISTS train_tasks (
    id              TEXT PRIMARY KEY,
    base_model      TEXT NOT NULL,
    lora_rank       INTEGER DEFAULT 16,
    lora_alpha      INTEGER DEFAULT 32,
    learning_rate   REAL DEFAULT 1e-4,
    epochs          INTEGER DEFAULT 3,
    dataset_path    TEXT NOT NULL,
    status          TEXT DEFAULT 'queued',
    progress        REAL DEFAULT 0.0,
    -- 审计 R3-BE1：/learn/tasks/reorder 排序字段
    priority        INTEGER DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0
);

-- 系统设置（键值对，规格 §3.2 SystemSettings）
CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '{}',   -- JSON
    updated_at  REAL NOT NULL DEFAULT 0
);

-- 协同调度历史学习（文档B 第四章引擎清单3：记录每次调度决策与效果，
-- 自适应调整预加载阈值；时间列按文档D 数据库规范存 TEXT ISO 8601）
CREATE TABLE IF NOT EXISTS schedule_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,              -- ISO 8601 UTC，如 2026-08-07T00:45:00Z
    mode_before   TEXT NOT NULL DEFAULT '',   -- 切换前协同模式
    mode_after    TEXT NOT NULL DEFAULT '',   -- 切换后协同模式
    trigger       TEXT NOT NULL DEFAULT '',   -- 触发源（hysteresis/emergency/...）
    vram_free_mb  INTEGER NOT NULL DEFAULT 0, -- 决策时刻空闲显存
    wait_ms       INTEGER NOT NULL DEFAULT 0, -- 候选模式滞回等待时长
    success       INTEGER NOT NULL DEFAULT 1  -- 策略执行是否成功（0/1）
);
CREATE INDEX IF NOT EXISTS idx_schedule_history_ts ON schedule_history(ts);
"""


class Database:
    """SQLite 数据库管理器。

    线程安全：每个线程持有独立连接（threading.local），
    写操作通过全局锁串行化以避免 WAL 下的写冲突。
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._recover_if_corrupt()
        self._init_schema()

    def _recover_if_corrupt(self) -> None:
        """启动前损坏自检与自愈（审计 R3-ARCH3）。

        一次异常断电导致的 DB 文件损坏此前会让应用永久无法启动
        （_init_schema raise → lifespan re-raise → 进程退出），用户无自助
        恢复手段。此处 PRAGMA quick_check 非 'ok' 时隔离损坏文件
        （.corrupt-{ts} 后缀，含 -wal/-shm 伴生文件）并重建全新空库；
        原数据保留在隔离文件中，可配合 /system/backup 形成备份-恢复闭环。
        库文件不存在或校验通过时零开销直接返回。
        """
        if not self._path.exists():
            return
        try:
            probe = sqlite3.connect(str(self._path))
            try:
                verdict = probe.execute("PRAGMA quick_check").fetchone()
            finally:
                probe.close()
        except sqlite3.Error as exc:
            verdict = (f"open_failed: {exc}",)
        if verdict and str(verdict[0]).lower() == "ok":
            return
        log.error("数据库完整性检查未通过: %s → %s", self._path, verdict)
        ts = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self._path) + suffix)
            if src.exists():
                dst = Path(f"{src}.corrupt-{ts}")
                try:
                    src.rename(dst)
                    log.warning("已隔离损坏文件: %s → %s", src.name, dst.name)
                except OSError as exc:
                    log.error("损坏文件隔离失败: %s → %s", src, exc)
                    raise
        log.warning("数据库将以全新空库重建（原数据保留于 .corrupt-%s 文件）", ts)

    # ── 连接管理 ──────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """获取当前线程的连接（懒创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._path),
                timeout=DB_BUSY_TIMEOUT / 1000.0,
                isolation_level=None,          # 自动提交模式
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row     # 返回字典式行
            conn.execute("PRAGMA foreign_keys = ON")
            if DB_WAL_MODE:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {int(DB_BUSY_TIMEOUT)}")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        """初始化所有表结构。"""
        conn = self._conn()
        try:
            conn.executescript(_SCHEMA)
            self._migrate_columns(conn)
            n_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            log.info("数据库初始化完成: %s（%d 张表）", self._path, n_tables)
        except sqlite3.Error as exc:
            log.error("数据库建表失败: %s", exc)
            raise

    # 存量库列迁移：按 schema 版本分组 —— (版本号, ((表, 列, 列定义), ...))。
    # 新增迁移时：追加新版本组并同步抬升 SCHEMA_VERSION，禁止修改历史组。
    # SQLite 无 IF NOT EXISTS 列语法，以 PRAGMA table_info 判定后 ALTER TABLE 补齐。
    SCHEMA_VERSION = 2

    _MIGRATION_GROUPS: tuple[tuple[int, tuple[tuple[str, str, str], ...]], ...] = (
        (1, (
            ("dialog_sessions", "pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("dialog_sessions", "mode", "TEXT DEFAULT ''"),
            ("dialog_messages", "rating", "INTEGER NOT NULL DEFAULT 0"),
            ("dialog_messages", "favorite", "INTEGER NOT NULL DEFAULT 0"),
            # R2-B06：存量库补齐分镜行排序列（新库 _SCHEMA 已含，幂等跳过）
            ("storyboard_rows", "sort_index", "INTEGER NOT NULL DEFAULT 0"),
            # R3-BE1：存量库补齐训练任务优先级列（/learn/tasks/reorder 支撑）
            ("train_tasks", "priority", "INTEGER DEFAULT 0"),
            # 批 1.3（修复任务清单）：分镜行导演字段扩展
            ("storyboard_rows", "camera_type", "TEXT DEFAULT ''"),
            ("storyboard_rows", "camera_angle", "TEXT DEFAULT ''"),
            ("storyboard_rows", "camera_movement", "TEXT DEFAULT ''"),
            ("storyboard_rows", "duration", "REAL DEFAULT 0"),
            ("storyboard_rows", "transition", "TEXT DEFAULT ''"),
            ("storyboard_rows", "speed", "REAL DEFAULT 1.0"),
            ("storyboard_rows", "volume", "REAL DEFAULT 0.0"),
            ("storyboard_rows", "music_path", "TEXT DEFAULT ''"),
            ("storyboard_rows", "asset_id", "TEXT DEFAULT ''"),
            # 竞品对齐改造：分镜行多资产绑定 + 行锁定（新库 _SCHEMA 已含，幂等跳过）
            ("storyboard_rows", "asset_ids", "TEXT DEFAULT '[]'"),
            ("storyboard_rows", "is_locked", "INTEGER NOT NULL DEFAULT 0"),
        )),
        (2, (
            # G1 解说漫剧：项目作品类型（新库 _SCHEMA 已含，幂等跳过）。
            # 2026-08-14 事故：建库早于该列引入，CREATE TABLE IF NOT EXISTS 不补列，
            # 新建叙事项目报错 —— 自此迁移按版本登记并纳入测试守护。
            ("projects", "work_mode", "TEXT NOT NULL DEFAULT 'regular'"),
        )),
    )

    @property
    def _COLUMN_MIGRATIONS(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(col for _v, cols in self._MIGRATION_GROUPS for col in cols)

    def schema_version(self, conn: sqlite3.Connection | None = None) -> int:
        """读取库当前 schema 版本（PRAGMA user_version，0=未记录的初始库）。"""
        c = conn or self._conn()
        return int(c.execute("PRAGMA user_version").fetchone()[0])

    def _migrate_columns(self, conn) -> None:
        """为已存在的旧库补齐新增列，并推进 user_version（幂等）。

        版本守护：库版本高于代码版本（用户回退到旧程序打开新库）时拒绝启动，
        避免旧代码在缺列 schema 上写入半截数据。
        """
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本 v{current} 高于程序支持版本 "
                f"v{self.SCHEMA_VERSION}，请升级程序后再打开（{self._path}）")
        for version, columns in self._MIGRATION_GROUPS:
            if version <= current:
                continue
            for table, column, ddl in columns:
                cols = {r[1] for r in conn.execute(
                    f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    log.info("数据库迁移 v%d: %s 新增列 %s", version, table, column)
            conn.execute(f"PRAGMA user_version = {version}")
            log.info("数据库 schema 已升级至 v%d", version)

    # ── 查询方法 ──────────────────────────────────────────────

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        """执行 SELECT，返回字典列表。"""
        conn = self._conn()
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        """执行 SELECT，返回单行字典或 None。"""
        conn = self._conn()
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None

    def sql(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行原始 SQL（CREATE/INSERT/UPDATE/DELETE），返回受影响行数。

        写操作加全局锁串行化。
        """
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute(sql, params)
            affected = cur.rowcount
            cur.close()
            return affected

    def executescript(self, script: str) -> None:
        """执行多语句脚本。"""
        conn = self._conn()
        with self._write_lock:
            conn.executescript(script)

    # ── 显式事务 ──────────────────────────────────────────────

    def execute_in_transaction(
            self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在全局写锁内以 BEGIN IMMEDIATE 显式事务执行 fn(conn)（审计 R3-P2）。

        连接为自动提交模式（isolation_level=None），多条写语句各自成事务
        会造成写放大（如分镜全量保存 1 DELETE + 50 INSERT + 1 UPDATE
        = 52 次独立提交）。本方法把 fn 包裹为单事务：fn 抛异常时
        ROLLBACK 并原样上抛，成功时 COMMIT。

        注意：fn 内必须使用传入的裸连接执行 SQL，禁止回调
        self.sql / insert / update / delete / executescript ——
        _write_lock 为 threading.Lock（不可重入），嵌套调用会死锁。
        列值编码可用 Database._serialize 保持与既有写路径一致。
        """
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error as rb_exc:  # 极端情形事务已隐式回滚
                    log.warning("事务回滚异常（原异常仍将上抛）: %s", rb_exc)
                raise
            conn.execute("COMMIT")
            return result

    # ── 便捷 CRUD ────────────────────────────────────────────

    def insert(self, table: str, data: dict) -> str:
        """向指定表插入一行，data 为列名→值映射，返回 rowid。

        dict / list 类型自动序列化为 JSON。
        """
        cols = list(data.keys())
        vals = [self._serialize(data[c]) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute(sql, vals)
            rowid = cur.lastrowid
            cur.close()
        return str(rowid) if rowid else ""

    def update(self, table: str, data: dict, where: str,
               where_params: Sequence[Any] = ()) -> int:
        """更新指定表，data 为列名→值映射，where 为条件字符串。

        返回受影响行数。
        """
        if not data:
            return 0
        set_parts = [f"{c} = ?" for c in data.keys()]
        set_str = ", ".join(set_parts)
        vals = [self._serialize(v) for v in data.values()] + list(where_params)
        sql = f"UPDATE {table} SET {set_str} WHERE {where}"
        return self.sql(sql, vals)

    def delete(self, table: str, where: str,
               where_params: Sequence[Any] = ()) -> int:
        """删除指定表中满足条件的行，返回受影响行数。"""
        sql = f"DELETE FROM {table} WHERE {where}"
        return self.sql(sql, where_params)

    def count(self, table: str, where: str = "",
              where_params: Sequence[Any] = ()) -> int:
        """统计指定表行数。"""
        sql = f"SELECT COUNT(*) AS c FROM {table}"
        if where:
            sql += f" WHERE {where}"
        row = self.query_one(sql, where_params)
        return row["c"] if row else 0

    # ── 工具方法 ──────────────────────────────────────────────

    @staticmethod
    def _serialize(value: Any) -> Any:
        """将 dict / list 序列化为 JSON 字符串，其余原样返回。"""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, bool):
            return int(value)
        return value

    def close(self) -> None:
        """关闭当前线程的连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_db_instance: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """获取全局数据库单例（线程安全双重检查）。"""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = Database(DB_PATH)
    return _db_instance


def get_db_safe() -> Database | None:
    """获取数据库单例；不可用返回 None。

    供 API 层使用：返回 None 时调用方降级到内存模拟存储，
    保证数据库未就绪时应用仍可运行（规格 §4.1 容错降级）。
    """
    try:
        return get_db()
    except Exception as exc:  # noqa: BLE001 - 降级而非崩溃
        log.warning("数据库不可用，API 将降级到内存存储: %s", exc)
        return None


def parse_json(value: Any, default: Any) -> Any:
    """反序列化数据库中的 JSON 字符串字段。

    sqlite3 取回的 JSON 列为字符串，本函数将其还原为 list/dict；
    解析失败或为空时返回 default。已经是 list/dict 的直接返回。
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        if value == "":
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return default
    return default


def utcnow() -> float:
    """当前 Unix 时间戳（秒，浮点），供 created_at / updated_at 使用。"""
    return time.time()
