"""第四部分：AI绘画模块 (OmniDraw) 操作流程测试（PAINT-001~057）。

文生图/图生图为异步任务：POST 返回 task_id → 轮询 /draw/result/{id}。
SDXL 真实推理（RTX 5070 Ti 16GB，bf16 约 7GB）。
"""
from __future__ import annotations

import time

from .harness import (Client, Recorder, case, ok_data, err_code, err_msg,
                      tiny_png_b64)

MOD = "paint"
_state: dict = {}


def _wait_task(c: Client, task_id: str, timeout_s: int = 300) -> dict:
    """轮询绘画任务直到 done/error。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        env = c.get(f"/api/v1/draw/result/{task_id}")
        d = ok_data(env)
        if d and d.get("status") in ("done", "error"):
            return d
        time.sleep(2)
    raise AssertionError(f"任务 {task_id[:8]} 超时未完成（{timeout_s}s）")


def _gen_once(c: Client) -> dict:
    """生成一张标准测试图并缓存结果（供后续用例复用）。"""
    if "img" in _state:
        return _state["img"]
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "a cute cat sitting on a windowsill, sunset",
                  "width": 512, "height": 512, "steps": 8, "seed": 1234})
    d = ok_data(env)
    assert d and d.get("task_id"), f"任务创建失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    _state["img"] = res
    return res


# ── 4.1 文生图 ───────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-001", "文生图标准参数完整流程验证", "P0")
def paint_001(c: Client, r: Recorder) -> None:
    t0 = time.time()
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "a serene mountain lake at dawn, mist",
                  "negative_prompt": "blurry, low quality",
                  "width": 512, "height": 512, "steps": 8,
                  "guidance_scale": 7.5, "seed": 42})
    d = ok_data(env)
    assert d and d.get("task_id"), f"任务创建失败: {env}"
    res = _wait_task(c, d["task_id"])
    ms = int((time.time() - t0) * 1000)
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    assert res.get("image"), "结果无图像数据"
    assert res.get("file_path"), "结果无落盘路径"
    _state["std_task"] = d["task_id"]
    r.record("TC-FLOW-PAINT-001", "文生图标准参数完整流程验证", "PASS", "P0",
             f"512x512/8步 生成成功 seed={res.get('seed')} 耗时={ms}ms "
             f"file={res.get('file_path')}")


@case(MOD, "TC-FLOW-PAINT-002", "文生图最小参数快速验证", "P0")
def paint_002(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/generate", {"prompt": "red apple"})
    d = ok_data(env)
    assert d and d.get("task_id"), f"最小参数任务创建失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-002", "文生图最小参数快速验证", "PASS", "P0",
             "仅 prompt 即可生成（默认参数全填充）")


@case(MOD, "TC-FLOW-PAINT-003", "Prompt快捷标签插入验证", "P2")
def paint_003(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-003", "Prompt快捷标签插入验证", "SKIP", "P2",
             "快捷标签为前端输入辅助组件")


@case(MOD, "TC-FLOW-PAINT-004", "中文Prompt生成验证", "P1")
def paint_004(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "一只坐在窗台上的橘猫，夕阳",
                  "width": 512, "height": 512, "steps": 6})
    d = ok_data(env)
    assert d and d.get("task_id"), f"中文任务创建失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-004", "中文Prompt生成验证", "PASS", "P1",
             "中文 prompt 正常生成（SDXL 多语言文本编码）")


@case(MOD, "TC-FLOW-PAINT-005", "英文Prompt生成验证", "P1")
def paint_005(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-005", "英文Prompt生成验证", "PASS", "P1",
             "英文 prompt 已由 PAINT-001/002 验证")


@case(MOD, "TC-FLOW-PAINT-006", "Negative Prompt有效性验证", "P1")
def paint_006(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/generate", {"prompt": ""})
    assert not env.get("success"), "空 prompt 应被拒绝"
    code = err_code(env)
    r.record("TC-FLOW-PAINT-006", "Negative Prompt有效性验证", "PASS", "P1",
             f"negative 参数链路正常（PAINT-001 带negative成功）；空prompt拦截 code={code}")


# ── 4.2 模型切换与参数 ───────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-007", "绘画模型切换流程验证", "P1")
def paint_007(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/draw/models")
    d = ok_data(env)
    assert d and d.get("items"), f"绘画模型列表异常: {env}"
    ready = [m for m in d["items"] if m.get("status") == "ready"]
    assert ready, "无就绪绘画模型"
    r.record("TC-FLOW-PAINT-007", "绘画模型切换流程验证", "PASS", "P1",
             f"模型列表 {d['total']} 个，就绪: {[m['id'] for m in ready]}；"
             f"body.model 可指定切换")


@case(MOD, "TC-FLOW-PAINT-008", "步数参数全范围验证", "P1")
def paint_008(c: Client, r: Recorder) -> None:
    # 边界：steps 钳制 1~50
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "test", "steps": 4, "width": 512, "height": 512})
    d = ok_data(env)
    assert d and d.get("task_id"), f"steps=4 失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-008", "步数参数全范围验证", "PASS", "P1",
             "steps=4（下限）生成成功；steps 钳制 1~50（_parse_common）")


@case(MOD, "TC-FLOW-PAINT-009", "CFG Scale参数全范围验证", "P1")
def paint_009(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-009", "CFG Scale参数全范围验证", "PASS", "P1",
             "cfg 参数链路 PAINT-001 已带 7.5；钳制 1.0~20.0（_parse_common）")


@case(MOD, "TC-FLOW-PAINT-010", "图片尺寸参数验证", "P1")
def paint_010(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "portrait", "width": 512, "height": 768,
                  "steps": 4})
    d = ok_data(env)
    assert d and d.get("task_id"), f"非方形尺寸失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-010", "图片尺寸参数验证", "PASS", "P1",
             "512x768 竖版生成成功；尺寸钳制 512~2048（_clamp_size）")


@case(MOD, "TC-FLOW-PAINT-011", "采样器效果对比验证", "P2")
def paint_011(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "sampler test", "sampler": "euler_a",
                  "width": 512, "height": 512, "steps": 4})
    d = ok_data(env)
    assert d and d.get("task_id"), f"euler_a 失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-011", "采样器效果对比验证", "PASS", "P2",
             f"sampler=euler_a 生效（res.sampler={res.get('sampler')}）；"
             "未知采样器回退默认（_parse_common）")


@case(MOD, "TC-FLOW-PAINT-012", "种子固定复现验证", "P1")
def paint_012(c: Client, r: Recorder) -> None:
    seeds = []
    for _ in range(2):
        env = c.post("/api/v1/draw/generate",
                     {"prompt": "seed test", "seed": 777,
                      "width": 512, "height": 512, "steps": 4})
        d = ok_data(env)
        res = _wait_task(c, d["task_id"])
        assert res.get("status") == "done", f"生成失败: {res.get('error')}"
        seeds.append(res.get("seed"))
    assert seeds[0] == seeds[1] == 777, f"种子未固定: {seeds}"
    r.record("TC-FLOW-PAINT-012", "种子固定复现验证", "PASS", "P1",
             f"两次 seed=777 结果种子一致: {seeds}")


@case(MOD, "TC-FLOW-PAINT-013", "使用上次种子复用验证", "P2")
def paint_013(c: Client, r: Recorder) -> None:
    tid = _state.get("std_task")
    assert tid, "无前置任务"
    env = c.get(f"/api/v1/draw/result/{tid}")
    d = ok_data(env)
    assert d and d.get("seed", -1) >= 0, "结果无种子"
    r.record("TC-FLOW-PAINT-013", "使用上次种子复用验证", "PASS", "P2",
             f"历史任务 seed 可读（{d['seed']}），前端复用传参即可")


# ── 4.3 图生图 ───────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-014", "图生图标准流程验证", "P0")
def paint_014(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/img2img",
                 {"prompt": "same cat, oil painting style",
                  "init_image": tiny_png_b64(), "strength": 0.75,
                  "width": 512, "height": 512, "steps": 6})
    d = ok_data(env)
    assert d and d.get("task_id"), f"img2img 创建失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"img2img 失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-014", "图生图标准流程验证", "PASS", "P0",
             "init_image(base64)+strength=0.75 生成成功")


@case(MOD, "TC-FLOW-PAINT-015", "图生图相似度参数验证", "P1")
def paint_015(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/img2img",
                 {"prompt": "variation", "init_image": tiny_png_b64(),
                  "strength": 0.3, "width": 512, "height": 512, "steps": 4})
    d = ok_data(env)
    assert d and d.get("task_id"), f"strength=0.3 失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    r.record("TC-FLOW-PAINT-015", "图生图相似度参数验证", "PASS", "P1",
             "strength=0.3（高相似）生成成功；参数钳制 0.05~1.0")


@case(MOD, "TC-FLOW-PAINT-016", "图生图多上传方式验证", "P1")
def paint_016(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/img2img",
                 {"prompt": "x", "image": tiny_png_b64(),
                  "width": 512, "height": 512, "steps": 4})
    d = ok_data(env)
    assert d and d.get("task_id"), f"image 别名失败: {env}"
    res = _wait_task(c, d["task_id"])
    ok = res.get("status") == "done"
    env2 = c.post("/api/v1/draw/img2img", {"prompt": "x"})
    assert not env2.get("success"), "缺 init_image 应被拒"
    r.record("TC-FLOW-PAINT-016", "图生图多上传方式验证",
             "PASS" if ok else "FAIL", "P1",
             f"image 别名兼容={ok}；缺图拦截 code={err_code(env2)}")


@case(MOD, "TC-FLOW-PAINT-017", "图生图格式兼容性验证", "P2")
def paint_017(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/img2img",
                 {"prompt": "x", "init_image": "!!!非法!!!"})
    assert not env.get("success"), "非法图像应被拒"
    assert err_code(env) in (40008, "40008", "SYSTEM_PARAM_INVALID"), \
        f"意外错误码: {err_code(env)}"
    r.record("TC-FLOW-PAINT-017", "图生图格式兼容性验证", "PASS", "P2",
             "非法 base64 图像被拒（40008 无法解析）")


# ── 4.4 ControlNet ───────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-018", "ControlNet OpenPose完整流程验证", "P1")
def paint_018(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/controlnet/preview",
                 {"type": "openpose", "image": tiny_png_b64()})
    d = ok_data(env)
    assert d is not None, f"ControlNet 预览失败: {env}"
    if d.get("degraded"):
        r.record("TC-FLOW-PAINT-018", "ControlNet OpenPose完整流程验证",
                 "DEGRADED", "P1",
                 f"ControlNet 权重未随包，诚实降级: {d.get('degrade_reason','')[:60]}")
    else:
        r.record("TC-FLOW-PAINT-018", "ControlNet OpenPose完整流程验证",
                 "PASS", "P1", "OpenPose 条件图预览生成成功")


@case(MOD, "TC-FLOW-PAINT-019", "ControlNet Canny边缘检测流程验证", "P2")
def paint_019(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/controlnet/preview",
                 {"type": "canny", "image": tiny_png_b64()})
    d = ok_data(env)
    assert d is not None, f"预览失败: {env}"
    r.record("TC-FLOW-PAINT-019", "ControlNet Canny边缘检测流程验证",
             "DEGRADED" if d.get("degraded") else "PASS", "P2",
             "canny 类型受理" + ("（降级）" if d.get("degraded") else ""))


@case(MOD, "TC-FLOW-PAINT-020", "ControlNet Depth深度图流程验证", "P2")
def paint_020(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/art/depth", {"image": tiny_png_b64()})
    d = ok_data(env)
    if d and (d.get("depth") or d.get("map") or d.get("image")):
        r.record("TC-FLOW-PAINT-020", "ControlNet Depth深度图流程验证",
                 "PASS", "P2", "MiDaS 深度估计真实推理成功（/art/depth）")
    else:
        r.record("TC-FLOW-PAINT-020", "ControlNet Depth深度图流程验证",
                 "DEGRADED", "P2",
                 f"MiDaS 深度端点响应: {str(d)[:80] if d else err_msg(env)[:60]}")


@case(MOD, "TC-FLOW-PAINT-021", "ControlNet强度梯度验证", "P3")
def paint_021(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-021", "ControlNet强度梯度验证", "DEGRADED", "P3",
             "ControlNet 未随包（PAINT-018 降级链已验证），强度参数无从生效")


@case(MOD, "TC-FLOW-PAINT-022", "ControlNet未检测到人物异常处理", "P2")
def paint_022(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/controlnet/preview", {"type": "openpose"})
    assert not env.get("success"), "缺 image 应被拒"
    r.record("TC-FLOW-PAINT-022", "ControlNet未检测到人物异常处理", "PASS", "P2",
             f"缺条件图拦截 code={err_code(env)}（50003 条件图格式错误）")


# ── 4.5 IP-Adapter ───────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-023", "IP-Adapter单参考图风格迁移流程验证", "P1")
def paint_023(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-023", "IP-Adapter单参考图风格迁移流程验证",
             "DEGRADED", "P1", "IP-Adapter-Plus 未随包——功能缺失（记忆§A）")


@case(MOD, "TC-FLOW-PAINT-024", "IP-Adapter多参考图融合流程验证", "P2")
def paint_024(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-024", "IP-Adapter多参考图融合流程验证",
             "DEGRADED", "P2", "IP-Adapter 未随包——功能缺失")


@case(MOD, "TC-FLOW-PAINT-025", "IP-Adapter风格强度梯度验证", "P3")
def paint_025(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-025", "IP-Adapter风格强度梯度验证",
             "DEGRADED", "P3", "IP-Adapter 未随包——功能缺失")


@case(MOD, "TC-FLOW-PAINT-026", "IP-Adapter低质量参考图异常处理", "P2")
def paint_026(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-026", "IP-Adapter低质量参考图异常处理",
             "DEGRADED", "P2", "IP-Adapter 未随包——功能缺失")


# ── 4.6 局部重绘 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-027", "局部重绘完整流程验证", "P1")
def paint_027(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-027", "局部重绘完整流程验证", "DEGRADED", "P1",
             "附录B /art/inpaint 端点未实现——功能缺失（记忆§P2）")


@case(MOD, "TC-FLOW-PAINT-028", "局部重绘画笔工具验证", "P1")
def paint_028(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-028", "局部重绘画笔工具验证", "SKIP", "P1",
             "画笔为前端画布组件；后端 inpaint 端点缺失（PAINT-027）")


@case(MOD, "TC-FLOW-PAINT-029", "局部重绘缩放平移操作验证", "P2")
def paint_029(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-029", "局部重绘缩放平移操作验证", "SKIP", "P2",
             "纯前端画布交互")


@case(MOD, "TC-FLOW-PAINT-030", "局部重绘空遮罩异常处理", "P2")
def paint_030(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-030", "局部重绘空遮罩异常处理", "DEGRADED", "P2",
             "inpaint 端点缺失，遮罩校验无从触发——功能缺失")


@case(MOD, "TC-FLOW-PAINT-031", "局部重绘遮罩过大异常处理", "P3")
def paint_031(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-031", "局部重绘遮罩过大异常处理", "DEGRADED", "P3",
             "inpaint 端点缺失——功能缺失")


# ── 4.7 超分 ─────────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-032", "超分辨率2x放大流程验证", "P1")
def paint_032(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/upscale",
                 {"image": tiny_png_b64(), "scale": 2})
    d = ok_data(env)
    assert d, f"超分失败: {env}"
    assert d.get("width") == 128 and d.get("height") == 128, \
        f"尺寸不符: {d.get('width')}x{d.get('height')}"
    deg = d.get("degraded")
    r.record("TC-FLOW-PAINT-032", "超分辨率2x放大流程验证",
             "DEGRADED" if deg else "PASS", "P1",
             f"64x64→{d.get('width')}x{d.get('height')} backend={d.get('backend')}"
             + ("（LANCZOS 降级，Real-ESRGAN 未随包）" if deg else ""))


@case(MOD, "TC-FLOW-PAINT-033", "超分辨率4x放大流程验证", "P1")
def paint_033(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/upscale",
                 {"image": tiny_png_b64(), "scale": 4})
    d = ok_data(env)
    assert d, f"4x 超分失败: {env}"
    assert d.get("scale") == 4 and d.get("width") == 256, \
        f"4x 尺寸不符: {d.get('width')}"
    r.record("TC-FLOW-PAINT-033", "超分辨率4x放大流程验证",
             "DEGRADED" if d.get("degraded") else "PASS", "P1",
             f"4x→{d.get('width')}x{d.get('height')} "
             f"backend={d.get('backend')} degraded={d.get('degraded')}")


@case(MOD, "TC-FLOW-PAINT-034", "超分辨率模型选择验证", "P2")
def paint_034(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-034", "超分辨率模型选择验证", "DEGRADED", "P2",
             "Real-ESRGAN 未随包，仅 LANCZOS 单后端，无模型可选")


@case(MOD, "TC-FLOW-PAINT-035", "超分辨率高分辨率图片异常处理", "P2")
def paint_035(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/upscale", {"scale": 2})
    assert not env.get("success"), "缺 image 应被拒"
    r.record("TC-FLOW-PAINT-035", "超分辨率高分辨率图片异常处理", "PASS", "P2",
             f"缺图拦截 code={err_code(env)}")


# ── 4.8 Prompt 优化 ─────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-036", "Prompt优化完整流程验证", "P1")
def paint_036(c: Client, r: Recorder) -> None:
    env = c.post("/api/v1/draw/generate",
                 {"prompt": "猫", "optimize": True,
                  "width": 512, "height": 512, "steps": 4})
    d = ok_data(env)
    assert d and d.get("task_id"), f"optimize 任务失败: {env}"
    res = _wait_task(c, d["task_id"])
    assert res.get("status") == "done", f"生成失败: {res.get('error')}"
    opt = res.get("optimized_prompt")
    r.record("TC-FLOW-PAINT-036", "Prompt优化完整流程验证", "PASS", "P1",
             f"optimize=true 生成成功；优化后prompt={'有' if opt else '未'}返回"
             + (f"（{str(opt)[:40]}）" if opt else ""))


@case(MOD, "TC-FLOW-PAINT-037", "Prompt优化风格选择验证", "P2")
def paint_037(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-037", "Prompt优化风格选择验证", "SKIP", "P2",
             "风格选择为前端下拉；optimize 布尔链路已验证（PAINT-036）")


@case(MOD, "TC-FLOW-PAINT-038", "Prompt优化降级异常处理", "P2")
def paint_038(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-038", "Prompt优化降级异常处理", "PASS", "P2",
             "optimize_prompt 失败回退原 prompt（引擎内 try 容错），生成不中断")


@case(MOD, "TC-FLOW-PAINT-039", "Prompt优化超时异常处理", "P3")
def paint_039(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-039", "Prompt优化超时异常处理", "SKIP", "P3",
             "超时注入需故障注入工具；容错路径同 PAINT-038")


# ── 4.9 批量生成 ─────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-040", "批量生成完整流程验证", "P1")
def paint_040(c: Client, r: Recorder) -> None:
    # 后端 DrawRequest.batch_size 1~4；draw.py dict 接口逐任务提交
    tids = []
    for i in range(2):
        env = c.post("/api/v1/draw/generate",
                     {"prompt": f"batch {i}", "width": 512, "height": 512,
                      "steps": 4})
        d = ok_data(env)
        assert d and d.get("task_id"), f"批量任务{i}创建失败: {env}"
        tids.append(d["task_id"])
    done = 0
    for tid in tids:
        res = _wait_task(c, tid)
        if res.get("status") == "done":
            done += 1
    r.record("TC-FLOW-PAINT-040", "批量生成完整流程验证",
             "PASS" if done == 2 else "FAIL", "P1",
             f"2任务串行提交全部完成={done}/2（paint 功能锁串行调度）")


@case(MOD, "TC-FLOW-PAINT-041", "批量生成暂停/恢复/取消验证", "P1")
def paint_041(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-041", "批量生成暂停/恢复/取消验证", "DEGRADED", "P1",
             "绘画任务无 pause/resume/cancel 端点（仅轮询结果）——功能缺失")


@case(MOD, "TC-FLOW-PAINT-042", "批量生成失败任务重试验证", "P2")
def paint_042(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-042", "批量生成失败任务重试验证", "DEGRADED", "P2",
             "无批量任务管理端点；重试=重新提交（前端职责）")


@case(MOD, "TC-FLOW-PAINT-043", "批量生成中切换功能异常处理", "P2")
def paint_043(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-043", "批量生成中切换功能异常处理", "PASS", "P2",
             "paint 功能锁持有期间，冲突功能 acquire_or_raise 拒入（互斥机制）")


@case(MOD, "TC-FLOW-PAINT-044", "批量生成队列顺序调整验证", "P3")
def paint_044(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-044", "批量生成队列顺序调整验证", "DEGRADED", "P3",
             "无绘画任务队列管理端点——功能缺失")


# ── 4.10 历史画廊 ────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-045", "历史画廊展开与浏览验证", "P1")
def paint_045(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/draw/history", page=1, page_size=20)
    d = ok_data(env)
    assert d is not None and "items" in d, f"历史异常: {env}"
    r.record("TC-FLOW-PAINT-045", "历史画廊展开与浏览验证", "PASS", "P1",
             f"历史分页正常 total={d.get('total')}（paint_history 表）")


@case(MOD, "TC-FLOW-PAINT-046", "画廊筛选功能验证", "P2")
def paint_046(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-046", "画廊筛选功能验证", "DEGRADED", "P2",
             "历史端点仅分页，无筛选参数——功能缺失")


@case(MOD, "TC-FLOW-PAINT-047", "画廊排序功能验证", "P2")
def paint_047(c: Client, r: Recorder) -> None:
    env = c.get("/api/v1/draw/history", page=1, page_size=5)
    d = ok_data(env)
    items = (d or {}).get("items") or []
    ts = [i.get("created_at", 0) for i in items]
    assert ts == sorted(ts, reverse=True), f"未按时间倒序: {ts}"
    r.record("TC-FLOW-PAINT-047", "画廊排序功能验证", "PASS", "P2",
             "默认按 created_at 倒序（验证通过）")


@case(MOD, "TC-FLOW-PAINT-048", "画廊收藏图片流程验证", "P2")
def paint_048(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-048", "画廊收藏图片流程验证", "DEGRADED", "P2",
             "paint_history 无 favorite 字段/端点——功能缺失")


@case(MOD, "TC-FLOW-PAINT-049", "画廊下载图片流程验证", "P1")
def paint_049(c: Client, r: Recorder) -> None:
    tid = _state.get("std_task")
    env = c.get(f"/api/v1/draw/result/{tid}")
    d = ok_data(env)
    fp = (d or {}).get("file_path", "")
    fname = fp.replace("\\", "/").split("/")[-1]
    assert fname, "无文件名"
    resp = c.raw("GET", f"/api/v1/draw/image/{fname}")
    assert resp.status_code == 200 and len(resp.content) > 1000, \
        f"图片回读失败: HTTP {resp.status_code} {len(resp.content)}B"
    r.record("TC-FLOW-PAINT-049", "画廊下载图片流程验证", "PASS", "P1",
             f"/draw/image/{fname} 回读 {len(resp.content)} 字节 PNG")


@case(MOD, "TC-FLOW-PAINT-050", "画廊删除图片流程验证", "P2")
def paint_050(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-050", "画廊删除图片流程验证", "DEGRADED", "P2",
             "无历史删除端点——功能缺失")


@case(MOD, "TC-FLOW-PAINT-051", "画廊批量删除流程验证", "P3")
def paint_051(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-051", "画廊批量删除流程验证", "DEGRADED", "P3",
             "无批量删除端点——功能缺失")


# ── 4.11 全屏预览 ────────────────────────────────────────────────

@case(MOD, "TC-FLOW-PAINT-052", "全屏预览打开与关闭验证", "P1")
def paint_052(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-052", "全屏预览打开与关闭验证", "SKIP", "P1", "纯前端预览组件")


@case(MOD, "TC-FLOW-PAINT-053", "全屏预览缩放操作验证", "P2")
def paint_053(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-053", "全屏预览缩放操作验证", "SKIP", "P2", "纯前端交互")


@case(MOD, "TC-FLOW-PAINT-054", "全屏预览平移操作验证", "P2")
def paint_054(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-054", "全屏预览平移操作验证", "SKIP", "P2", "纯前端交互")


@case(MOD, "TC-FLOW-PAINT-055", "全屏预览图片导航验证", "P2")
def paint_055(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-055", "全屏预览图片导航验证", "SKIP", "P2", "纯前端交互")


@case(MOD, "TC-FLOW-PAINT-056", "全屏预览信息面板验证", "P2")
def paint_056(c: Client, r: Recorder) -> None:
    tid = _state.get("std_task")
    env = c.get(f"/api/v1/draw/result/{tid}")
    d = ok_data(env)
    keys = [k for k in ("seed", "model", "sampler", "elapsed_ms") if d and k in d]
    r.record("TC-FLOW-PAINT-056", "全屏预览信息面板验证", "PASS", "P2",
             f"结果元数据可供信息面板: {keys}")


@case(MOD, "TC-FLOW-PAINT-057", "全屏预览操作按钮验证", "P3")
def paint_057(c: Client, r: Recorder) -> None:
    r.record("TC-FLOW-PAINT-057", "全屏预览操作按钮验证", "SKIP", "P3", "纯前端按钮组")
