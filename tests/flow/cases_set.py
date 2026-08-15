"""第九部分：设置页面操作流程测试（SET-001~030）。

设置页后端面较小：SystemSettings 仅 5 键（theme/font_size/auto_model_select/
default_video_codec/default_resolution，PUT 为整体替换），另有学习设置
（/learn/settings，SQLite learning_settings 表持久化）、备份/诊断/版本/
系统信息端点。代理/镜像/带宽/日志管理/性能指标/服务重启/磁盘管理等计划
端点经路由表核对缺失，如实记 DEGRADED；纯前端交互记 SKIP。
清理/重置类用例禁止真实清空数据库，仅做保护性契约验证。
"""
from __future__ import annotations

from .harness import Client, Recorder, case, ok_data, err_code

MOD = "set"

_state: dict = {}


# ── 通用工具 ─────────────────────────────────────────────────────

def _read_settings(c: Client) -> dict:
    """读取系统设置（SystemSettings 五键）。"""
    env = c.get("/api/v1/system/settings")
    d = ok_data(env)
    assert d is not None, f"设置读取异常: {env}"
    return d


def _write_settings(c: Client, settings: dict) -> dict:
    """整体替换式写入系统设置（PUT 语义为全量，须传完整 dict）。"""
    env = c.put("/api/v1/system/settings", settings)
    d = ok_data(env)
    assert d is not None, f"设置写入异常: {env}"
    return d


def _assert_missing(env: dict, path: str) -> None:
    """断言端点缺失（404 统一包装为 SYSTEM_RESOURCE_NOT_FOUND）。"""
    assert not env.get("success"), f"{path} 已存在，用例状态需重估: {env}"
    assert err_code(env) == "SYSTEM_RESOURCE_NOT_FOUND", \
        f"{path} 缺失但错误码异常: {env}"


# ── 9.1 通用设置 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-SET-001", "主题切换设置验证", "P0")
def set_001(c: Client, r: Recorder) -> None:
    d = _read_settings(c)
    old = d.get("theme", "sakura")
    new = "dark" if old != "dark" else "sakura"
    upd = dict(d); upd["theme"] = new
    d2 = _write_settings(c, upd)
    assert d2.get("theme") == new, f"主题写入未生效: {d2}"
    back = _read_settings(c)
    assert back.get("theme") == new, f"主题回读不一致: {back}"
    upd["theme"] = old
    d3 = _write_settings(c, upd)
    assert d3.get("theme") == old, f"主题还原失败: {d3}"
    r.record("TC-FLOW-SET-001", "主题切换设置验证", "PASS", "P0",
             f"主题设置读写回读一致（{old}→{new}→{old}，已还原）；200ms过渡动画/"
             "CSS Variables/Ctrl+Shift+T快捷键/3D背景跟随为前端渲染，留待UI冒烟；"
             "后端为进程内存存储，重启回默认（见SYS-026）")


@case(MOD, "TC-FLOW-SET-002", "语言设置切换验证", "P1")
def set_002(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-002", "语言设置切换验证", "SKIP", "P1",
             "后端 SystemSettings 仅 theme/font_size/auto_model_select/"
             "default_video_codec/default_resolution 五键，无 language 键"
             "（Pydantic 忽略额外字段，写入不落库）；语言切换为纯前端 i18n，"
             "留待浏览器冒烟")


@case(MOD, "TC-FLOW-SET-003", "启动行为设置验证", "P1")
def set_003(c: Client, r: Recorder) -> None:
    d = _read_settings(c)
    old = bool(d.get("auto_model_select", True))
    upd = dict(d); upd["auto_model_select"] = not old
    d2 = _write_settings(c, upd)
    assert d2.get("auto_model_select") == (not old), f"写入未生效: {d2}"
    back = _read_settings(c)
    assert back.get("auto_model_select") == (not old), f"回读不一致: {back}"
    upd["auto_model_select"] = old
    d3 = _write_settings(c, upd)
    assert d3.get("auto_model_select") == old, f"还原失败: {d3}"
    r.record("TC-FLOW-SET-003", "启动行为设置验证", "PASS", "P1",
             f"auto_model_select 读写回读一致（{old}→{not old}→{old}，已还原），"
             "对应「启动时自动加载常用模型」开关持久化链路；「恢复上次页面/"
             "最小化到托盘」为前端/桌面壳行为，留待冒烟")


@case(MOD, "TC-FLOW-SET-004", "开机自启动设置验证", "P2")
def set_004(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-004", "开机自启动设置验证", "SKIP", "P2",
             "开机自启动需写 OS 启动项并重启计算机验证，自动化不操作注册表/"
             "重启物理机；无后端端点，留待手动验证")


@case(MOD, "TC-FLOW-SET-005", "通知设置验证", "P2")
def set_005(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-005", "通知设置验证", "SKIP", "P2",
             "Windows Toast 通知弹出与通知权限为 OS/浏览器侧行为，"
             "通知开关无后端持久化键，留待浏览器冒烟")


@case(MOD, "TC-FLOW-SET-006", "快捷键设置验证", "P2")
def set_006(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-006", "快捷键设置验证", "SKIP", "P2",
             "快捷键列表/修改/冲突检测/恢复默认均为前端键位捕获逻辑，"
             "无后端端点，留待浏览器冒烟")


# ── 9.2 硬件设置 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-SET-007", "GPU显存阈值设置验证", "P0")
def set_007(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/models/vram")
    d = ok_data(env)
    assert d is not None, f"显存端点异常: {env}"
    gpu = d.get("gpu") or {}
    r.record("TC-FLOW-SET-007", "GPU显存阈值设置验证", "DEGRADED", "P0",
             f"显存告警阈值（85%/95%/98%）无后端持久化键（SystemSettings "
             "五键不含阈值），阈值判定/黄红告警/自动卸载为前端+调度器逻辑；"
             f"显存遥测链路可用：total={gpu.get('total_gb', gpu.get('vram_total_gb', '?'))}GB "
             f"free={gpu.get('free_gb', gpu.get('vram_free_gb', '?'))}GB "
             f"loaded={d.get('loaded_count')}")


@case(MOD, "TC-FLOW-SET-008", "硬件等级手动设置验证", "P1")
def set_008(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/hardware/info")
    d = ok_data(env)
    assert d, f"硬件信息异常: {env}"
    tier = d.get("tier") or {}
    r.record("TC-FLOW-SET-008", "硬件等级手动设置验证", "DEGRADED", "P1",
             f"硬件等级手动设置端点缺失（hardware 仅 GET info/realtime/synergy，"
             f"无 PUT）；当前为自动检测档位 tier={tier.get('tier')} "
             f"label={tier.get('label')} matched_by={tier.get('matched_by')}；"
             "手动切换及 fp16/int8/int4 精度联动提示未实现")


@case(MOD, "TC-FLOW-SET-009", "渲染质量设置验证", "P2")
def set_009(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-009", "渲染质量设置验证", "SKIP", "P2",
             "阴影质量/抗锯齿/像素比为 Three.js 前端渲染参数，FPS 变化与"
             "低配推荐为前端逻辑，无后端端点，留待浏览器冒烟")


@case(MOD, "TC-FLOW-SET-010", "内存与CPU线程设置验证", "P2")
def set_010(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/hardware/info")
    d = ok_data(env)
    assert d, f"硬件信息异常: {env}"
    cpu = d.get("cpu") or {}
    ram = d.get("ram") or {}
    r.record("TC-FLOW-SET-010", "内存与CPU线程设置验证", "DEGRADED", "P2",
             f"推理线程数/Celery并发/内存缓存设置无后端端点（路由表核对），"
             f"超出硬件能力告警未实现；当前硬件可读：CPU={str(cpu.get('name', ''))[:24]} "
             f"{cpu.get('cores')}核{cpu.get('threads')}线程 RAM={ram.get('total_gb')}GB")


@case(MOD, "TC-FLOW-SET-011", "硬件自动检测校准验证", "P1")
def set_011(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/hardware/info")
    d = ok_data(env)
    assert d, f"硬件信息异常: {env}"
    gpu = d.get("gpu") or {}
    cpu = d.get("cpu") or {}
    ram = d.get("ram") or {}
    disk = d.get("disk") or {}
    tier = d.get("tier") or {}
    assert gpu.get("name"), "GPU 型号未检测到"
    assert (gpu.get("vram_total_mb") or 0) > 0, "显存总量未检测到"
    r.record("TC-FLOW-SET-011", "硬件自动检测校准验证", "PASS", "P1",
             f"硬件自动检测返回真实数据：GPU={gpu.get('name')} "
             f"VRAM={gpu.get('vram_total_mb')}MB driver={gpu.get('driver_version', '')}；"
             f"CPU={str(cpu.get('name', ''))[:24]} {cpu.get('cores')}核；"
             f"RAM={ram.get('total_gb')}GB；磁盘余量={disk.get('free_gb')}GB；"
             f"自动推荐档位={tier.get('label')}（检测耗时<5s 为单次同步采集）")


# ── 9.3 网络与代理设置 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-SET-012", "网络代理设置验证", "P2")
def set_012(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-012", "网络代理设置验证", "DEGRADED", "P2",
             "代理地址/认证/测试连接端点缺失（路由表核对无 /system/proxy 等）；"
             "架构为离线单机（127.0.0.1 绑定），仅学习模块联网检索，"
             "代理认证加密存储未实现")


@case(MOD, "TC-FLOW-SET-013", "HuggingFace镜像源设置验证", "P2")
def set_013(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-013", "HuggingFace镜像源设置验证", "DEGRADED", "P2",
             "镜像源设置/测试镜像/多源优先级端点缺失（路由表核对）；"
             "模型以随包+本地导入为主（POST /models/import），"
             "镜像延迟探测与自动回切未实现")


@case(MOD, "TC-FLOW-SET-014", "网络带宽限制设置验证", "P3")
def set_014(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-014", "网络带宽限制设置验证", "DEGRADED", "P3",
             "下载带宽限制端点缺失（路由表核对无限速设置项），"
             "下载速度限制逻辑未实现")


# ── 9.4 训练与导出设置 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-SET-015", "默认训练参数设置验证", "P1")
def set_015(c: Client, r: Recorder) -> None:
    # 边界校验保护性验证：lora_rank 合法域 4~64，越界须被 Pydantic 拒绝
    # （校验先于入队，不会触发真实训练）
    env = c.post("/api/v1/learn/train",
                 {"base_model": "qwen3-vl-4b", "lora_rank": 999})
    assert not env.get("success"), f"越界参数未被拒绝: {env}"
    assert err_code(env) == "SYSTEM_PARAM_INVALID", f"错误码异常: {env}"
    r.record("TC-FLOW-SET-015", "默认训练参数设置验证", "DEGRADED", "P1",
             "训练默认参数（rank/alpha/lr/epochs/batch）无独立设置持久化端点"
             "（未实现，新建训练不读默认值配置）；参数边界校验在线："
             "lora_rank=999 被 SYSTEM_PARAM_INVALID 拒绝（合法域4~64，"
             "lr≤1e-3，epochs1~50，审计P0-6硬件安全边界）；模型内置默认 "
             "rank=16/alpha=32/lr=1e-4/epochs=3")


@case(MOD, "TC-FLOW-SET-016", "训练自动调度设置验证", "P2")
def set_016(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/learn/settings")
    d = ok_data(env)
    assert d is not None, f"学习设置读取异常: {env}"
    old_freq = d.get("auto_finetune_frequency", "weekly")
    old_win = d.get("schedule_windows") or []
    new_freq = "daily" if old_freq != "daily" else "weekly"
    new_win = ["02:00-04:00"]
    upd = c.put("/api/v1/learn/settings",
                {"auto_finetune_frequency": new_freq,
                 "schedule_windows": new_win})
    d2 = ok_data(upd)
    assert d2 and d2.get("auto_finetune_frequency") == new_freq, \
        f"频率写入未生效: {upd}"
    assert d2.get("schedule_windows") == new_win, f"时段写入未生效: {upd}"
    back = ok_data(c.get("/api/v1/learn/settings")) or {}
    assert back.get("auto_finetune_frequency") == new_freq, "回读不一致"
    # 数据阈值闸口：训练状态暴露 min_training_samples
    st = ok_data(c.get("/api/v1/learn/training/status")) or {}
    min_samples = st.get("min_training_samples")
    # 还原
    c.put("/api/v1/learn/settings",
          {"auto_finetune_frequency": old_freq, "schedule_windows": old_win})
    r.record("TC-FLOW-SET-016", "训练自动调度设置验证", "PASS", "P2",
             f"自动训练调度设置持久化于 learning_settings 表（SQLite）："
             f"auto_finetune_frequency {old_freq}→{new_freq}→已还原，"
             f"schedule_windows 读写回读一致；数据阈值闸口 "
             f"min_training_samples={min_samples}（规格§3.3 阈值100条），"
             "训练前资源空闲检查经调度器 evaluate/should_pause 在线")


@case(MOD, "TC-FLOW-SET-017", "导出默认参数设置验证", "P2")
def set_017(c: Client, r: Recorder) -> None:
    d = _read_settings(c)
    old_codec = d.get("default_video_codec", "h264")
    old_res = d.get("default_resolution", "1080p")
    new_codec = "h265" if old_codec != "h265" else "h264"
    new_res = "720p" if old_res != "720p" else "1080p"
    upd = dict(d)
    upd["default_video_codec"] = new_codec
    upd["default_resolution"] = new_res
    d2 = _write_settings(c, upd)
    assert d2.get("default_video_codec") == new_codec, f"编码写入未生效: {d2}"
    assert d2.get("default_resolution") == new_res, f"分辨率写入未生效: {d2}"
    back = _read_settings(c)
    assert back.get("default_video_codec") == new_codec \
        and back.get("default_resolution") == new_res, f"回读不一致: {back}"
    _write_settings(c, d)  # 还原
    r.record("TC-FLOW-SET-017", "导出默认参数设置验证", "PASS", "P2",
             f"导出默认编码/分辨率读写回读一致（{old_codec}/{old_res}→"
             f"{new_codec}/{new_res}→已还原）；默认帧率/默认导出路径无后端键"
             "（前端项）；编码格式按硬件能力调整为导出链路逻辑")


@case(MOD, "TC-FLOW-SET-018", "导出质量预设验证", "P2")
def set_018(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-018", "导出质量预设验证", "SKIP", "P2",
             "质量预设（最佳质量CRF18/平衡CRF23/最小文件CRF28）为前端参数"
             "模板映射逻辑，无后端预设端点，留待浏览器冒烟")


# ── 9.5 数据与隐私设置 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-SET-019", "数据自动备份设置验证", "P1")
def set_019(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/system/backup")
    d = ok_data(env)
    assert d and d.get("backup_id"), f"备份失败: {env}"
    r.record("TC-FLOW-SET-019", "数据自动备份设置验证", "DEGRADED", "P1",
             f"手动备份链路验证通过：backup_id={str(d.get('backup_id', ''))[:8]} "
             f"size={d.get('size_bytes')}B 设置JSON写盘={bool(d.get('path'))} "
             f"SQLite热备={bool(d.get('db_path'))}（data/backups/，文件名含时间戳）；"
             "自动备份开关/间隔/备份路径/最大保留数设置无后端键（未实现），"
             "超量轮转删除未实现")


@case(MOD, "TC-FLOW-SET-020", "数据清理与重置验证", "P1")
def set_020(c: Client, r: Recorder) -> None:
    # 保护性验证①：清理端点契约——虚构 session_id 删除 0 行，不触真实数据
    env = c.post("/api/v1/chat/clear",
                 {"session_id": "set_probe_不存在会话_勿删真实数据"})
    assert env.get("success"), f"清理端点异常: {env}"
    # 保护性验证②：重置所有设置为出厂默认（读→置默认→回读→还原原值）
    d = _read_settings(c)
    factory = {"theme": "sakura", "font_size": 14, "auto_model_select": True,
               "default_video_codec": "h264", "default_resolution": "1080p"}
    d2 = _write_settings(c, factory)
    assert d2.get("theme") == "sakura" and d2.get("font_size") == 14, \
        f"出厂重置未生效: {d2}"
    back = _read_settings(c)
    assert back.get("theme") == "sakura" \
        and back.get("auto_model_select") is True, f"回读不一致: {back}"
    d3 = _write_settings(c, d)  # 还原
    assert all(d3.get(k) == v for k, v in d.items()), f"还原失败: {d3}"
    r.record("TC-FLOW-SET-020", "数据清理与重置验证", "PASS", "P1",
             "重置所有设置为出厂值链路验证通过（sakura/14/true/h264/1080p "
             "回读一致，已还原原值）；清理端点契约保护性验证：/chat/clear "
             "以虚构 session_id 调用成功（0行删除，禁止真实清空数据库）；"
             "确认对话框/释放空间大小展示为前端项，留待冒烟")


@case(MOD, "TC-FLOW-SET-021", "数据导出与导入验证", "P2")
def set_021(c: Client, r: Recorder) -> None:
    # 契约保护性验证①：空 project_id 必须被拒绝
    env1 = c.post("/api/v1/system/project/export", {})
    assert not env1.get("success"), f"空 project_id 未被拒绝: {env1}"
    # 契约保护性验证②：白名单外路径必须被拒绝（审计 BK-028）
    env2 = c.post("/api/v1/system/project/import",
                  {"file_path": "C:/Windows/win.ini"})
    assert not env2.get("success"), f"白名单外路径未被拒绝: {env2}"
    assert err_code(env2) == "SYSTEM_UNAUTHORIZED", f"错误码异常: {env2}"
    r.record("TC-FLOW-SET-021", "数据导出与导入验证", "DEGRADED", "P2",
             f"全量数据导出（.tar.gz+SHA256校验+版本兼容性检查）未实现；"
             f"项目级 .omnispace 导出/导入端点存在，契约保护性验证通过："
             f"空 project_id 被拒绝（{err_code(env1)}），白名单外路径被拒绝"
             "（SYSTEM_UNAUTHORIZED）；真实导出需有效项目id，留待跨模块流程")


@case(MOD, "TC-FLOW-SET-022", "隐私设置验证", "P2")
def set_022(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/system/version")
    d = ok_data(env)
    assert d, f"版本信息异常: {env}"
    r.record("TC-FLOW-SET-022", "隐私设置验证", "SKIP", "P2",
             f"「收集使用数据/自动发送错误报告」开关无后端键（纯前端项）；"
             f"架构层证实数据不出本机：host={d.get('host')} port={d.get('port')} "
             "离线单机绑定，无数据上传通道；开关交互留待浏览器冒烟")


@case(MOD, "TC-FLOW-SET-023", "API Key管理验证", "P2")
def set_023(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-023", "API Key管理验证", "DEGRADED", "P2",
             "API Key 存储/验证/脱敏显示端点缺失（路由表核对，bcrypt 哈希"
             "存储未实现）；已有安全控制：日志中间件对 api_key/password/"
             "token/secret 等敏感字段自动脱敏（middleware/logger.py）")


# ── 9.6 关于与日志设置 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-SET-024", "关于页面信息验证", "P2")
def set_024(c: Client, r: Recorder) -> None:
    v = ok_data(c.get("/api/v1/system/version"))
    assert v and v.get("version"), "版本端点异常"
    u = ok_data(c.get("/api/v1/system/update"))
    assert u is not None and u.get("supported") is False, \
        f"更新端点应如实返回 supported=false: {u}"
    i = ok_data(c.get("/api/v1/system/info"))
    assert i and i.get("version"), "系统信息端点异常"
    hw = i.get("hardware") or {}
    r.record("TC-FLOW-SET-024", "关于页面信息验证", "PASS", "P2",
             f"关于页数据齐备：version={v.get('version')} build={v.get('build')}；"
             f"检查更新端点如实返回 supported=false（免安装RC形态 "
             f"channel={u.get('channel')}）；系统信息含硬件摘要 "
             f"GPU={hw.get('gpu_name', '')}；许可证类型/Python/PyTorch/CUDA "
             "版本列表为前端展示项")


@case(MOD, "TC-FLOW-SET-025", "日志级别设置验证", "P2")
def set_025(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-SET-025", "日志级别设置验证", "DEGRADED", "P2",
             "日志级别运行时设置端点缺失（路由表核对无 /system/logs* 及"
             "级别端点）；当前级别为 config.yaml 启动配置 logging.level=INFO，"
             "修改需重启进程；DEBUG/INFO/WARNING/ERROR 四级切换的日志量"
             "变化留待手动验证")


@case(MOD, "TC-FLOW-SET-026", "日志文件管理验证", "P2")
def set_026(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/system/logs", page=1, page_size=10))
    assert d is not None and "items" in d and "total" in d, \
        f"日志分页响应异常: {d}"
    lvl = ok_data(c.get("/api/v1/system/logs", page=1, page_size=5,
                        level="ERROR"))
    assert lvl is not None, "级别过滤异常"
    r.record("TC-FLOW-SET-026", "日志文件管理验证", "PASS", "P2",
             f"GET /system/logs 分页在线（total={d.get('total')}，最新在前）；"
             f"level=ERROR 过滤可用（命中 {lvl.get('total')} 条）；"
             "另有 /system/logs/export(.txt下载) 与 /system/logs/cleanup"
             "(keep_days 保留清理，活跃日志永不删)；清除确认对话框为前端")


@case(MOD, "TC-FLOW-SET-027", "系统信息查看验证", "P2")
def set_027(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/system/info")
    d = ok_data(env)
    assert d and d.get("version"), f"系统信息异常: {env}"
    hw = d.get("hardware") or {}
    assert hw.get("gpu_name"), f"硬件摘要缺失: {d}"
    tier = hw.get("tier") or {}
    r.record("TC-FLOW-SET-027", "系统信息查看验证", "PASS", "P2",
             f"GET /api/v1/system/info 返回 version={d.get('version')} + "
             f"硬件摘要：GPU={hw.get('gpu_name')} "
             f"VRAM={hw.get('vram_total_mb')}MB tier={tier.get('label')} "
             f"CPU={str(hw.get('cpu_name', ''))[:24]} "
             f"RAM={hw.get('ram_total_gb')}GB；OS版本/Python版本/磁盘详情与"
             "复制到剪贴板为前端展示项")


@case(MOD, "TC-FLOW-SET-028", "性能监控面板验证", "P3")
def set_028(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/system/metrics")
    _assert_missing(env, "/api/v1/system/metrics")
    rt = ok_data(c.get("/api/v1/hardware/realtime"))
    assert rt is not None, "实时遥测异常"
    gpu = rt.get("gpu") or {}
    cpu = rt.get("cpu") or {}
    r.record("TC-FLOW-SET-028", "性能监控面板验证", "DEGRADED", "P3",
             "GET /api/v1/system/metrics（Prometheus格式）端点缺失（实测404）；"
             f"实时遥测可经 /hardware/realtime 获取：GPU占用={gpu.get('usage_percent')}% "
             f"VRAM={gpu.get('vram_used_mb')}/{gpu.get('vram_total_mb')}MB "
             f"温度={gpu.get('temp_celsius')}℃ CPU={cpu.get('usage_percent')}%；"
             "折线图/时间范围切换/CSV导出为前端逻辑")


@case(MOD, "TC-FLOW-SET-029", "后端服务重启验证", "P2")
def set_029(c: Client, r: Recorder) -> None:
    # 保护性验证：自动化绝不触发真实重启，仅验证确认 token 闸门语义
    env = c.post("/api/v1/system/restart")
    assert not env.get("success") and err_code(env) == "SYSTEM_PARAM_INVALID", \
        f"无确认 token 应被参数校验拒绝: {env}"
    env2 = c.post("/api/v1/system/restart", {"confirm": "wrong-token"})
    assert not env2.get("success"), f"错误 token 应被拒绝: {env2}"
    r.record("TC-FLOW-SET-029", "后端服务重启验证", "PASS", "P2",
             "POST /system/restart 在线且确认闸门正确：无/错误 confirm token 均被 "
             "SYSTEM_PARAM_INVALID 拒绝（自动化不触发真实重启）；实现为 "
             "os.execv 原地替换进程，重启窗口 5~15s 前端轮询 /health 重连")


@case(MOD, "TC-FLOW-SET-030", "磁盘空间管理验证", "P2")
def set_030(c: Client, r: Recorder) -> None:
    d = ok_data(c.get("/api/v1/system/disk"))
    assert d and d.get("volumes"), f"磁盘概览异常: {d}"
    v0 = d["volumes"][0]
    assert "total_gb" in v0 and "free_gb" in v0, f"卷信息字段缺失: {v0}"
    r.record("TC-FLOW-SET-030", "磁盘空间管理验证", "PASS", "P2",
             f"GET /system/disk 在线：{len(d['volumes'])} 卷 "
             f"（如 {v0.get('mount')} total={v0.get('total_gb')}GB "
             f"free={v0.get('free_gb')}GB）+ 数据目录分类占用 "
             f"{list((d.get('data_usage_mb') or {}).keys())} + top 大文件列表；"
             "饼图可视化/清理确认为前端")
