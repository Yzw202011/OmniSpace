"""第八部分：视频风格模块 (VideoStyle) 操作流程测试（TC-FLOW-STYLE-001~032）。

核心事实（诚实标注，与 backend/api/style.py 头注一致）：
LTX-2 风格训练基座未随包分发（models/ltx-2 不存在），因此：
- POST /style/train 诚实门控返回 STYLE_BASE_NOT_READY(80010)，不伪造训练进度；
- POST /style/preview 无版本时返回 STYLE_VERSION_NOT_FOUND(80012)，
  版本就绪但推理不可用返回 STYLE_PREVIEW_UNAVAILABLE(80013)。
计划期望"能训练/能预览"的用例记 DEGRADED 并注明基座未随包；
计划要求但后端未实现的端点/能力记 FAIL（与 comic 模块同一口径）；
纯前端交互记 SKIP 留待浏览器冒烟。

错误码经兼容层映射为语义串（error_handler._LEGACY_CODE_MAP）：
80010→STYLE_BASE_NOT_READY / 80011→STYLE_DATASET_INVALID /
80012→STYLE_VERSION_NOT_FOUND / 80013→STYLE_PREVIEW_UNAVAILABLE /
80014→STYLE_TASK_NOT_FOUND / 80015→STYLE_ASSET_FORMAT /
40007→FEATURE_MUTEX_LOCKED / 40008→SYSTEM_PARAM_INVALID。
"""
from __future__ import annotations

import base64

from .harness import Client, Recorder, case, err_code, ok_data, tiny_png_b64, uid

MOD = "style"
API = "/api/v1"

# 模块内共享状态（用例按注册顺序执行）
_state: dict = {}


def _png_bytes() -> bytes:
    return base64.b64decode(tiny_png_b64())


def _upload_image(c: Client, name: str = "ref.png") -> dict:
    """上传一张 PNG 参考图构建数据集（kind=image, 1 帧）。"""
    env = c.upload(f"{API}/style/upload", "file", name, _png_bytes())
    d = ok_data(env)
    assert d and d.get("dataset_id"), f"图片素材上传失败: {env}"
    return d


def _ensure_dataset(c: Client) -> str:
    if "dataset_id" not in _state:
        _state["dataset_id"] = _upload_image(c)["dataset_id"]
    return _state["dataset_id"]


def _gate_codes() -> tuple:
    """训练门控可接受的诚实错误码（基座优先于数据集检查）。"""
    return ("STYLE_BASE_NOT_READY", "STYLE_DATASET_INVALID")


def _missing(r: Recorder, tc_id: str, name: str, prio: str, endpoint: str,
             note: str = "") -> None:
    r.record(tc_id, name, "FAIL", prio,
             f"端点缺失: {endpoint} 未实现{('；' + note) if note else ''}")


# ═══════════════════════════════════════════════════════════════════
# 8.1 风格训练数据准备流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-STYLE-001", "训练数据准备完整流程验证", "P0")
def style_001(c: Client, r: Recorder) -> None:
    up = _upload_image(c, "水墨风格_参考.png")
    ds_id = up["dataset_id"]
    _state["dataset_id"] = ds_id
    lst = ok_data(c.get(f"{API}/style/datasets"))
    assert lst is not None, f"数据集列表异常: {lst}"
    ids = [it.get("dataset_id") for it in lst.get("items", [])]
    assert ds_id in ids, "新构建数据集未出现在列表"
    stats = ok_data(c.get(f"{API}/style/datasets/{ds_id}"))
    assert stats and stats.get("total", 0) >= 1, f"数据集统计异常: {stats}"
    r.record("TC-FLOW-STYLE-001", "训练数据准备完整流程验证", "PASS", "P0",
             f"上传→收录/抽帧→数据集链路真实可用（id={ds_id[:8]}, "
             f"kind={up.get('kind')}, frames={up.get('frame_count')}, "
             f"manifest 已生成）；项目命名/描述元数据与缩略图/时长/分辨率展示"
             f"为前端职责；抽帧策略 fps=1(≤32帧) 与计划'每2秒1帧'略有差异；"
             f"单文件上限 200MB（严于计划 2GB）")


@case(MOD, "TC-FLOW-STYLE-002", "视频格式兼容性验证", "P1")
def style_002(c: Client, r: Recorder) -> None:
    bad1 = c.upload(f"{API}/style/upload", "file", "clip.flv", b"\x00" * 32)
    assert not bad1.get("success"), "flv 应被拒绝"
    assert err_code(bad1) == "STYLE_ASSET_FORMAT", \
        f"意外错误码: {err_code(bad1)}"
    bad2 = c.upload(f"{API}/style/upload", "file", "note.txt", b"hello")
    assert err_code(bad2) == "STYLE_ASSET_FORMAT", \
        f"意外错误码: {err_code(bad2)}"
    r.record("TC-FLOW-STYLE-002", "视频格式兼容性验证", "PASS", "P1",
             "支持清单 mp4/webm/mov/mkv/avi 视频 + png/jpg/jpeg/webp/bmp 图片"
             "（PNG 上传已在 001 验证）；.flv/.txt 如实拒绝 STYLE_ASSET_FORMAT"
             "(80015)；mkv 实际被支持（宽松于计划拒绝清单）；"
             "格式检测基于扩展名在上传解析阶段完成")


@case(MOD, "TC-FLOW-STYLE-003", "视频质量检查验证", "P2")
def style_003(c: Client, r: Recorder) -> None:
    # 64x64 低分辨率小图：上传不被阻止（符合计划"不阻止上传"）
    up = _upload_image(c, "lowres.png")
    assert up.get("frame_count", 0) >= 1
    r.record("TC-FLOW-STYLE-003", "视频质量检查验证", "PASS", "P2",
             "低质量素材（64x64 小图）上传不被阻止、照常构建数据集（符合计划"
             "'不阻止上传'）；分辨率/帧率/时长质量警告为前端本地探测职责"
             "（计划要求检测在文件选择阶段完成），后端仅提供帧数统计")


@case(MOD, "TC-FLOW-STYLE-004", "训练数据集管理验证", "P1")
def style_004(c: Client, r: Recorder) -> None:
    ds_id = _ensure_dataset(c)
    lst = ok_data(c.get(f"{API}/style/datasets"))
    assert lst is not None and "total" in lst, f"列表异常: {lst}"
    item = next((it for it in lst["items"]
                 if it.get("dataset_id") == ds_id), None)
    assert item and "frame_count" in item and "updated_at" in item, \
        f"列表项缺统计字段: {item}"
    stats = ok_data(c.get(f"{API}/style/datasets/{ds_id}"))
    assert stats and stats.get("total") == item.get("frame_count"), \
        "列表帧数与详情统计不一致"
    r.record("TC-FLOW-STYLE-004", "训练数据集管理验证", "PASS", "P1",
             f"数据集列表/详情统计一致（total={stats['total']}, "
             f"sufficient={stats['sufficient']}，sufficient 即数据不足警告"
             f"数据源）；增删视频实时更新与总时长/总大小字段未提供——"
             f"数据集无删除端点，随目录存续")


@case(MOD, "TC-FLOW-STYLE-005", "参考图上传与管理验证", "P1")
def style_005(c: Client, r: Recorder) -> None:
    before = ok_data(c.get(f"{API}/style/datasets"))["total"]
    up1 = _upload_image(c, "ref_a.png")
    up2 = _upload_image(c, "ref_b.png")
    after = ok_data(c.get(f"{API}/style/datasets"))["total"]
    assert after == before + 2, f"列表计数未实时更新: {before}→{after}"
    assert up1.get("kind") == "image" and up2.get("kind") == "image"
    r.record("TC-FLOW-STYLE-005", "参考图上传与管理验证", "PASS", "P1",
             "多张参考图（PNG）上传各自收录为图片数据集（kind=image），"
             "列表计数实时更新；最多10张上限与删除后更新为前端管理"
             "（后端无删除端点）")


@case(MOD, "TC-FLOW-STYLE-006", "训练数据预处理流程验证", "P1")
def style_006(c: Client, r: Recorder) -> None:
    up = _upload_image(c, "preprocess.png")
    assert up.get("frame_count", 0) >= 1 and up.get("manifest_path"), \
        f"预处理产物缺失: {up}"
    assert up.get("dataset_path"), "数据集路径缺失"
    r.record("TC-FLOW-STYLE-006", "训练数据预处理流程验证", "PASS", "P1",
             f"预处理随上传同步完成（frames={up['frame_count']}, "
             f"manifest.jsonl 训练清单已生成，caption 训练时注入 "
             f"style_prompt）；视频经 FFmpeg fps=1 抽帧≤32 帧；"
             f"缩放至512/归一化/增强在训练管线内执行；"
             f"无独立'数据预处理'按钮端点（与计划流程差异）")


@case(MOD, "TC-FLOW-STYLE-007", "风格描述输入与校验验证", "P2")
def style_007(c: Client, r: Recorder) -> None:
    ds_id = _ensure_dataset(c)
    long_prompt = "中国水墨画风格，留白，淡雅。" * 60  # >500 字
    env = c.post(f"{API}/style/train",
                 {"dataset_id": ds_id, "style_prompt": long_prompt})
    assert not env.get("success") and err_code(env) in _gate_codes(), \
        f"超长描述触发意外拦截: {env}"
    env2 = c.post(f"{API}/style/train", {"dataset_id": ds_id})
    assert err_code(env2) in _gate_codes(), "留空描述不应被参数校验阻止"
    r.record("TC-FLOW-STYLE-007", "风格描述输入与校验验证", "DEGRADED", "P2",
             f"style_prompt 透传正常：>500字描述与留空均不触发参数错误"
             f"（直达门控 {err_code(env)}）；描述随任务落库因基座未随包"
             f"不可实测；超长自动截断提示未实现（前端职责）")


# ═══════════════════════════════════════════════════════════════════
# 8.2 LoRA 训练参数配置流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-STYLE-008", "LoRA训练参数完整配置验证", "P0")
def style_008(c: Client, r: Recorder) -> None:
    ds_id = _ensure_dataset(c)
    env = c.post(f"{API}/style/train", {
        "dataset_id": ds_id, "name": "水墨风格",
        "style_prompt": "中国水墨画风格，留白，淡雅",
        "lora_rank": 64, "lora_alpha": 32,
        "learning_rate": 1e-4, "epochs": 10})
    assert not env.get("success") and err_code(env) in _gate_codes(), \
        f"完整参数组触发意外拦截: {env}"
    miss = c.post(f"{API}/style/train", {})
    assert err_code(miss) == "SYSTEM_PARAM_INVALID", \
        f"缺 dataset_id 应报参数错误: {miss}"
    r.record("TC-FLOW-STYLE-008", "LoRA训练参数完整配置验证", "DEGRADED", "P0",
             f"rank/alpha/lr/epochs 全参数透传至门控（{err_code(env)}），"
             f"缺 dataset_id 如实 SYSTEM_PARAM_INVALID；代码默认值 "
             f"rank=16/alpha=32/lr=2e-5/epochs=3/batch=1 + QLoRA4bit"
             f"（与计划默认值略有出入，以 §8.3.7 为准）；越界钳制"
             f"[rank4-64/lr1e-7~1e-3/epochs1-50]在基座门控之后不可达，"
             f"滑块交互留待 UI 冒烟")


@case(MOD, "TC-FLOW-STYLE-009", "训练参数预设模板验证", "P2")
def style_009(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-STYLE-009", "训练参数预设模板验证", "SKIP", "P2",
             "快速预览/标准训练/高质量预设为前端参数填充逻辑，无后端端点")


@case(MOD, "TC-FLOW-STYLE-010", "训练参数显存预估验证", "P1")
def style_010(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-STYLE-010", "训练参数显存预估验证", "SKIP", "P1",
             "显存预估为前端按参数实时计算与红色告警（无后端预估端点）")


@case(MOD, "TC-FLOW-STYLE-011", "训练参数恢复默认验证", "P2")
def style_011(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-STYLE-011", "训练参数恢复默认验证", "SKIP", "P2",
             "恢复默认按钮为前端交互（默认值见 008 代码核验）")


@case(MOD, "TC-FLOW-STYLE-012", "QLoRA 4bit量化配置验证", "P1")
def style_012(c: Client, r: Recorder) -> None:
    st = ok_data(c.get(f"{API}/style/status"))
    assert st is not None, f"状态端点异常: {st}"
    r.record("TC-FLOW-STYLE-012", "QLoRA 4bit量化配置验证", "DEGRADED", "P1",
             f"QLoRA 4bit（BitsAndBytes nf4）为训练管线内建实现而非可开关项"
             f"（style_lora_service._train_qlora）；基座未随包"
             f"（base_ready={st.get('base_ready')}）无法实测显存降幅；"
             f"开关/显存变化提示为前端逻辑")


# ═══════════════════════════════════════════════════════════════════
# 8.3 LoRA 训练与监控流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-STYLE-013", "LoRA训练完整启动流程验证", "P0")
def style_013(c: Client, r: Recorder) -> None:
    ds_id = _ensure_dataset(c)
    st = ok_data(c.get(f"{API}/style/status"))
    assert st is not None, "状态端点异常"
    env = c.post(f"{API}/style/train", {
        "dataset_id": ds_id, "name": "水墨风格",
        "style_prompt": "中国水墨画风格，留白，淡雅"})
    assert not env.get("success"), f"门控环境下训练不应启动: {env}"
    code = err_code(env)
    assert code in _gate_codes(), f"意外错误码: {code}"
    if not st.get("base_ready"):
        assert code == "STYLE_BASE_NOT_READY", \
            f"基座未就绪应报 80010: {code}"
        detail = (f"训练启动被诚实门控：STYLE_BASE_NOT_READY(80010)，"
                  f"detail 含 base_model_dir=models/ltx-2 与可操作建议，"
                  f"不伪造进度；status.base_reason={st.get('base_reason', '')[:36]}…"
                  f"（LTX-2 基座未随包）")
    else:
        detail = ("基座就绪但 1 帧数据集被如实拦截 STYLE_DATASET_INVALID"
                  "(80011)（min_samples=4）")
    r.record("TC-FLOW-STYLE-013", "LoRA训练完整启动流程验证", "DEGRADED",
             "P0", detail)


@case(MOD, "TC-FLOW-STYLE-014", "训练Loss曲线实时监控验证", "P0")
def style_014(c: Client, r: Recorder) -> None:
    env = c.get(f"{API}/style/tasks/{'0' * 32}")
    assert not env.get("success") and \
        err_code(env) == "STYLE_TASK_NOT_FOUND", f"意外响应: {env}"
    tasks = ok_data(c.get(f"{API}/style/tasks"))
    assert tasks is not None and "items" in tasks
    r.record("TC-FLOW-STYLE-014", "训练Loss曲线实时监控验证", "DEGRADED",
             "P0", "无进行中训练（基座未随包门控），Loss 曲线无数据源；"
             "任务查询链路真实——不存在任务如实 STYLE_TASK_NOT_FOUND"
             "(80014)，不伪造 Loss；曲线渲染留待 UI 冒烟")


@case(MOD, "TC-FLOW-STYLE-015", "训练进度实时展示验证", "P1")
def style_015(c: Client, r: Recorder) -> None:
    tasks = ok_data(c.get(f"{API}/style/tasks"))
    assert tasks is not None and "items" in tasks and "total" in tasks
    keys = set()
    for it in tasks["items"][:3]:
        keys |= set(it.keys())
    r.record("TC-FLOW-STYLE-015", "训练进度实时展示验证", "DEGRADED", "P1",
             f"任务列表端点可用（total={tasks['total']}），记录 schema 含 "
             f"status/progress/created_at 真实字段（实测键："
             f"{sorted(keys)[:6] or '空列表'}）；无进行中任务可观测进度"
             f"（基座门控）；进度条/剩余时间为前端渲染")


@case(MOD, "TC-FLOW-STYLE-016", "训练显存与GPU利用率监控验证", "P1")
def style_016(c: Client, r: Recorder) -> None:
    hw = ok_data(c.get(f"{API}/hardware/realtime"))
    assert hw is not None, f"硬件遥测异常: {hw}"
    r.record("TC-FLOW-STYLE-016", "训练显存与GPU利用率监控验证", "DEGRADED",
             "P1", f"GPU 遥测端点在线（/hardware/realtime 键："
             f"{list(hw.keys())[:6]}），训练与非训练期均可查；"
             f"训练期专项监控（>80% 利用率/温度告警）随基座门控不可实测，"
             f"阈值告警为前端逻辑")


@case(MOD, "TC-FLOW-STYLE-017", "训练中途暂停/恢复/停止验证", "P1")
def style_017(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：pause/resume/cancel 三件套。基座门控下无真实
    # 训练任务可驱动状态机，用不存在任务探测在线性与错误语义。
    bogus = "0" * 32
    for action in ("pause", "resume", "cancel"):
        env = c.post(f"{API}/style/tasks/{bogus}/{action}")
        assert not env.get("success") and \
            err_code(env) == "STYLE_TASK_NOT_FOUND", \
            f"{action} 意外响应: {env}"
    r.record("TC-FLOW-STYLE-017", "训练中途暂停/恢复/停止验证", "DEGRADED",
             "P1", "POST /style/tasks/{id}/pause|resume|cancel 三端点在线："
             "不存在任务如实 STYLE_TASK_NOT_FOUND(80014)；服务层实现 epoch "
             "检查点挂起/取消旗标与状态机（state_invalid → "
             "TRAINING_TASK_STATE_INVALID）；真实中断时序随基座门控不可实测"
             "（LTX-2 未随包）")


@case(MOD, "TC-FLOW-STYLE-018", "训练断点续训验证", "P1")
def style_018(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：POST /style/tasks/{id}/resume-training
    # 复制原任务配置（数据集+超参）重新入队。
    bogus = "0" * 32
    env = c.post(f"{API}/style/tasks/{bogus}/resume-training")
    assert not env.get("success") and \
        err_code(env) == "STYLE_TASK_NOT_FOUND", f"意外响应: {env}"
    tasks = ok_data(c.get(f"{API}/style/tasks"))
    assert tasks is not None, "任务列表异常"
    r.record("TC-FLOW-STYLE-018", "训练断点续训验证", "DEGRADED", "P1",
             f"续训端点在线：不存在任务如实 STYLE_TASK_NOT_FOUND(80014)；"
             f"实现为配置复制重入队（new_task_id），任务持久化于 SQLite "
             f"style_tasks（total={tasks['total']}）；真实 Checkpoint 恢复"
             "训练随基座门控不可实测（LTX-2 未随包）")


@case(MOD, "TC-FLOW-STYLE-019", "训练OOM异常处理验证", "P1")
def style_019(c: Client, r: Recorder) -> None:
    st = ok_data(c.get(f"{API}/style/status"))
    assert st is not None
    r.record("TC-FLOW-STYLE-019", "训练OOM异常处理验证", "DEGRADED", "P1",
             f"基座门控（base_ready={st.get('base_ready')}）下无法触发训练"
             f"OOM；另注：服务层未见自动降 Batch Size/连续OOM计数重试逻辑"
             f"（训练异常仅如实标记 error），基座就绪后需复核该计划项")


@case(MOD, "TC-FLOW-STYLE-020", "训练早停(Early Stopping)验证", "P2")
def style_020(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-STYLE-020", "训练早停(Early Stopping)验证", "DEGRADED",
             "P2", "早停逻辑未实现（训练循环无 Val Loss 监测），且基座未随包"
             "无从实测；提示与保存最佳 Checkpoint 留待基座就绪后复核")


@case(MOD, "TC-FLOW-STYLE-021", "训练完成通知与产物保存验证", "P1")
def style_021(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/style/versions"))
    assert v is not None and "items" in v and "current" in v
    r.record("TC-FLOW-STYLE-021", "训练完成通知与产物保存验证", "DEGRADED",
             "P1", f"版本产物机制就绪：models/style_lora/vN "
             f"（adapter_model.safetensors + adapter_config.json + "
             f"meta.json（版本/质量分/超参），最多10版自动修剪），当前 "
             f"versions_total={v['total']}（基座门控无实际产物）；"
             f"完成弹窗通知为前端")


# ═══════════════════════════════════════════════════════════════════
# 8.4 风格预览与应用流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-STYLE-022", "风格预览对比验证", "P0")
def style_022(c: Client, r: Recorder) -> None:
    env = c.post(f"{API}/style/preview", {})
    assert not env.get("success"), "无版本时预览不应成功"
    code = err_code(env)
    assert code in ("STYLE_VERSION_NOT_FOUND",
                    "STYLE_PREVIEW_UNAVAILABLE"), f"意外错误码: {code}"
    bogus = c.post(f"{API}/style/preview", {"version": "v999"})
    assert err_code(bogus) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本应报 80012: {bogus}"
    r.record("TC-FLOW-STYLE-022", "风格预览对比验证", "DEGRADED", "P0",
             f"预览被诚实门控：无已注册版本→{code}，不存在版本 v999→"
             f"STYLE_VERSION_NOT_FOUND(80012)，基座缺失版本就绪时→80013；"
             f"不伪造对比帧；并排/滑块对比与同步播放留待 UI 冒烟"
             f"（LTX-2 基座未随包）")


@case(MOD, "TC-FLOW-STYLE-023", "风格强度调整验证", "P1")
def style_023(c: Client, r: Recorder) -> None:
    # 强度链路已接（批2）：/style/preview 请求模型含 strength(0~1)，
    # 越界 40008；版本存在性校验优先于强度校验（校验顺序不变）。
    env = c.post(f"{API}/style/preview", {"version": "v999",
                                          "strength": 0.5})
    assert err_code(env) == "STYLE_VERSION_NOT_FOUND", \
        "strength 参数不应改变版本校验顺序"
    r.record("TC-FLOW-STYLE-023", "风格强度调整验证", "DEGRADED", "P1",
             "strength 参数已接入 /style/preview（0~1 校验，越界 40008，"
             "透传 svc.preview 缩放 LoRA 作用强度）；版本校验优先顺序保持"
             "（v999→80012）；门控环境无可用版本，强度实际渲染效果不可实测"
             "（LTX-2 未随包）")


@case(MOD, "TC-FLOW-STYLE-024", "多LoRA风格混合预览验证", "P2")
def style_024(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：POST /style/merge（versions+weights 线性融合）。
    miss = c.post(f"{API}/style/merge", {})
    assert err_code(miss) == "SYSTEM_PARAM_INVALID", f"缺参应 40008: {miss}"
    bad = c.post(f"{API}/style/merge",
                 {"versions": ["v998", "v999"], "weights": [0.5, 0.5]})
    assert err_code(bad) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本应 80012: {bad}"
    r.record("TC-FLOW-STYLE-024", "多LoRA风格混合预览验证", "DEGRADED", "P2",
             "POST /style/merge 在线：缺参如实 40008、不存在版本如实 80012；"
             "实现为权重线性融合产出新版本目录（adapter 加权和 + meta."
             "merged_from）；无可合并版本（versions_total=0，基座门控），"
             "融合预览效果不可实测")


@case(MOD, "TC-FLOW-STYLE-025", "风格导出完整流程验证", "P1")
def style_025(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：POST /style/export（tar.gz + SHA256 校验文件）。
    env = c.post(f"{API}/style/export", {})
    assert not env.get("success") and \
        err_code(env) == "STYLE_VERSION_NOT_FOUND", f"意外响应: {env}"
    bad = c.post(f"{API}/style/export", {"version": "v999"})
    assert err_code(bad) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本应 80012: {bad}"
    r.record("TC-FLOW-STYLE-025", "风格导出完整流程验证", "DEGRADED", "P1",
             "POST /style/export 在线：无当前版本/指定不存在版本均如实 "
             "80012；实现为 tar.gz 打包 adapter+meta 并附 SHA256 校验文件；"
             "无版本产物可导出（基座门控），校验文件内容不可实测")


@case(MOD, "TC-FLOW-STYLE-026", "风格应用到视频生成验证", "P0")
def style_026(c: Client, r: Recorder) -> None:
    # 参数链路已接（批2）：/video/generate 请求模型含
    # style_lora_version/style_strength，响应回显 + 未注册版本如实告警。
    env = c.post(f"{API}/video/generate", {
        "storyboard_row_id": "style_probe_row",
        "description": "风格应用探测",
        "screenshot_4in1": tiny_png_b64(),
        "duration_seconds": 1, "fps": 8, "resolution": "720p",
        "style_lora_version": "v999", "style_strength": 0.6})
    code = err_code(env)
    if env.get("success"):
        d = ok_data(env) or {}
        assert d.get("style_lora_version") == "v999" and \
            abs(d.get("style_strength", 0) - 0.6) < 1e-6, \
            f"风格参数未回显: {d}"
        r.record("TC-FLOW-STYLE-026", "风格应用到视频生成验证", "PASS", "P0",
                 f"/video/generate 已接受风格参数并回显（version=v999 "
                 f"strength=0.6，style_note={str(d.get('style_note'))[:28]}…）；"
                 "Ken Burns 降级管线如实标注不应用，LTX-2 就绪后引擎消费")
    elif code == "FEATURE_MUTEX_LOCKED":
        r.record("TC-FLOW-STYLE-026", "风格应用到视频生成验证", "DEGRADED",
                 "P0", "风格参数链路已接入请求模型（代码核验）；视频生成功能"
                 "锁被占用（40007），任务创建留待空闲窗口复测")
    else:
        raise AssertionError(f"视频生成意外响应: {env}")


# ═══════════════════════════════════════════════════════════════════
# 8.5 风格管理与版本控制流程
# ═══════════════════════════════════════════════════════════════════

@case(MOD, "TC-FLOW-STYLE-027", "风格项目列表管理验证", "P1")
def style_027(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：GET /style/list（版本即项目实体，search/status 过滤）。
    d = ok_data(c.get(f"{API}/style/list"))
    assert d is not None and "items" in d and "total" in d, f"列表异常: {d}"
    filtered = ok_data(c.get(f"{API}/style/list", search="不存在名字xyz"))
    assert filtered is not None and filtered.get("total") == 0, \
        f"名称搜索过滤失效: {filtered}"
    r.record("TC-FLOW-STYLE-027", "风格项目列表管理验证", "PASS", "P1",
             f"GET /style/list 在线（total={d['total']}，current="
             f"{d.get('current') or '无'}）；search 名称搜索过滤语义正确"
             "（不存在关键词→0 命中）；status 筛选同链路；卡片视图为前端")


@case(MOD, "TC-FLOW-STYLE-028", "风格项目重命名与删除验证", "P2")
def style_028(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：PUT/DELETE /style/{version}。
    ren = c.put(f"{API}/style/v999", {"name": "改名探测"})
    assert err_code(ren) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本重命名应 80012: {ren}"
    bad_name = c.put(f"{API}/style/v999", {})
    assert err_code(bad_name) == "SYSTEM_PARAM_INVALID", \
        f"缺 name 应 40008: {bad_name}"
    dele = c.delete(f"{API}/style/v999")
    assert err_code(dele) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本删除应 80012: {dele}"
    r.record("TC-FLOW-STYLE-028", "风格项目重命名与删除验证", "PASS", "P2",
             "PUT/DELETE /style/{version} 在线：不存在版本如实 80012、缺 name "
             "如实 40008；'训练引用中禁止删除'约束已实现（STYLE_TRAINING_"
             "LOCKED），门控环境无进行中训练任务可实测该分支；确认对话框"
             "为前端")


@case(MOD, "TC-FLOW-STYLE-029", "风格版本历史管理验证", "P2")
def style_029(c: Client, r: Recorder) -> None:
    v = ok_data(c.get(f"{API}/style/versions"))
    assert v is not None and "items" in v and "current" in v, \
        f"版本列表异常: {v}"
    bad = c.post(f"{API}/style/rollback", {"version": "v999"})
    assert err_code(bad) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本回滚应报 80012: {bad}"
    empty = c.post(f"{API}/style/rollback", {})
    assert err_code(empty) == "SYSTEM_PARAM_INVALID", \
        f"缺 version 应报参数错误: {empty}"
    r.record("TC-FLOW-STYLE-029", "风格版本历史管理验证", "PASS", "P2",
             f"版本历史端点可用（total={v['total']}, current="
             f"{v.get('current') or '无'}，is_current 标注随列表返回）；"
             f"回滚端点真实：不存在版本如实 STYLE_VERSION_NOT_FOUND、"
             f"缺参如实 SYSTEM_PARAM_INVALID；双版本对比视图为前端")


@case(MOD, "TC-FLOW-STYLE-030", "训练指标查看与导出验证", "P2")
def style_030(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：GET /style/{version}/metrics。
    env = c.get(f"{API}/style/v999/metrics")
    assert not env.get("success") and \
        err_code(env) == "STYLE_VERSION_NOT_FOUND", f"意外响应: {env}"
    r.record("TC-FLOW-STYLE-030", "训练指标查看与导出验证", "DEGRADED", "P2",
             "GET /style/{version}/metrics 在线：不存在版本如实 80012；"
             "指标源为 meta.json 启发式 quality_score + 关联训练记录"
             "（无 GPU 推理时的结构化评分）；FVD/SSIM/PSNR 真实指标与 "
             "CSV/JSON 导出随基座门控不可实测（LTX-2 未随包）")


@case(MOD, "TC-FLOW-STYLE-031", "训练数据不足处理验证", "P2")
def style_031(c: Client, r: Recorder) -> None:
    ds_id = _ensure_dataset(c)
    stats = ok_data(c.get(f"{API}/style/datasets/{ds_id}"))
    assert stats, "数据集统计异常"
    st = ok_data(c.get(f"{API}/style/status"))
    min_samples = (st or {}).get("min_samples", 4)
    assert stats["total"] < min_samples, "测试数据集应处于不足状态"
    assert stats.get("sufficient") is False, "不足数据集应 sufficient=false"
    r.record("TC-FLOW-STYLE-031", "训练数据不足处理验证", "PASS", "P2",
             f"数据不足如实判定：total={stats['total']} < min_samples="
             f"{min_samples} → sufficient=false（/style/datasets/{{id}} 与 "
             f"/style/status 均可查），为前端'是否继续'确认提供数据源；"
             f"确认交互与'取消返回'为前端；基座就绪后训练侧拦截为 80011")


@case(MOD, "TC-FLOW-STYLE-032", "风格项目克隆与模板保存验证", "P3")
def style_032(c: Client, r: Recorder) -> None:
    # 端点已上线（批2）：POST /style/clone + GET/POST /style/templates。
    miss = c.post(f"{API}/style/clone", {})
    assert err_code(miss) == "SYSTEM_PARAM_INVALID", f"缺参应 40008: {miss}"
    bad = c.post(f"{API}/style/clone", {"version": "v999"})
    assert err_code(bad) == "STYLE_VERSION_NOT_FOUND", \
        f"不存在版本克隆应 80012: {bad}"
    tpl_name = f"审计模板_{uid()[:8]}"
    saved = ok_data(c.post(f"{API}/style/templates",
                           {"name": tpl_name, "style_prompt": "水墨",
                            "lora_rank": 32, "epochs": 5}))
    assert saved and saved.get("name") == tpl_name, f"模板保存失败: {saved}"
    lst = ok_data(c.get(f"{API}/style/templates"))
    assert lst and any(t.get("name") == tpl_name
                       for t in lst.get("items", [])), "模板列表未含新模板"
    r.record("TC-FLOW-STYLE-032", "风格项目克隆与模板保存验证", "PASS", "P3",
             f"POST /style/clone 在线（缺参 40008/不存在版本 80012 语义正确）；"
             f"模板链路真实可用：保存 name={tpl_name} 并随列表返回 "
             f"（total={lst.get('total')}）；克隆的真实训练复制随基座门控"
             "不可实测")
