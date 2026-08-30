"""为 AIGODLIKE-ComfyUI-Translation 补齐漫剧工作流所用节点的中文翻译。

覆盖:
  1. internal.json 补 12 个核心节点(ReferenceLatent/ResolutionSelector/
     Flux2Scheduler/CreateVideo/SaveVideo 等漫画工作流在用而插件未翻的);
  2. 新建 ComfyUI-MiniMaxH3-Contex-Loop.json(链系统+Ref2VA 全套);
  3. 新建 comfyui-minimax-h3-turbo.json(Turbo 采样/LoRA);
  4. Impact-Pack / Easy-Use / KJNodes 三个既有文件追加缺失条目。
被修改文件均先备份为 *.bak-20260830。幂等:已存在的键不覆盖。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / (
    "tools/ComfyUI_windows_portable/ComfyUI/custom_nodes"
    "/AIGODLIKE-ComfyUI-Translation")
NODES = PLUGIN / "zh-CN" / "Nodes"
BAK = ".bak-20260830"
STAMP = "2026-08-30 漫剧工作流补翻"


def load_merge(path: Path, additions: dict) -> str:
    added = 0
    if path.exists():
        shutil.copy2(path, str(path) + BAK)
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {}
    for k, v in additions.items():
        if k not in data:
            data[k] = v
            added += 1
    data["_omnispace_patch"] = STAMP
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return f"{path.name}: +{added}"


CORE_ADD = {
    "ReferenceLatent": {"title": "参考潜空间注入", "inputs": {
        "conditioning": "条件", "latent": "参考图像潜空间"},
        "outputs": {"CONDITIONING": "条件"}},
    "ResolutionSelector": {"title": "分辨率选择器", "widgets": {
        "aspect_ratio": "宽高比", "megapixels": "百万像素",
        "multiple": "取整步长"}, "outputs": {"width": "宽", "height": "高"}},
    "EmptyFlux2LatentImage": {"title": "Flux2空白画布", "widgets": {
        "width": "宽", "height": "高", "batch_size": "张数"}},
    "Flux2Scheduler": {"title": "Flux2步数表", "widgets": {
        "steps": "步数", "width": "宽", "height": "高"},
        "outputs": {"SIGMAS": "信号表"}},
    "CreateVideo": {"title": "创建视频(图像+音频)", "widgets": {
        "fps": "帧率", "audio": "音频", "bit_depth": "位深",
        "color_space": "色彩空间"}, "outputs": {"VIDEO": "视频"}},
    "SaveVideo": {"title": "保存视频", "widgets": {
        "filename_prefix": "保存前缀", "format": "容器", "codec": "编码器"}},
    "GetVideoComponents": {"title": "视频拆组件(帧/音轨/帧率)",
                          "outputs": {"images": "帧序列", "audio": "音轨",
                                      "fps": "帧率"}},
    "PrimitiveInt": {"title": "整数值", "widgets": {"value": "值"}},
    "PrimitiveStringMultiline": {"title": "多行文本", "widgets": {"value": "文本"}},
    "PrimitiveString": {"title": "单行文本", "widgets": {"value": "文本"}},
    "MarkdownNote": {"title": "Markdown说明便签", "widgets": {"value": "内容"}},
    "ImagePass": {"title": "图像直通", "inputs": {"image": "图像"},
                  "outputs": {"IMAGE": "图像"}},
}

CTX_LOOP = {
    "MiniMaxH3ReferenceToVideo": {"title": "H3全能参考引擎（文字+多图→音画）",
        "widgets": {"prompt": "提示词", "width": "宽", "height": "高",
                    "length": "帧数", "ref_image_size": "参考图尺寸"},
        "inputs": {"clip": "文本编码器", "vae": "画面VAE",
                   "audio_vae": "音频VAE", "prompt": "提示词"},
        "outputs": {"positive": "正向条件", "LATENT": "音画潜空间"}},
    "MiniMaxH3ImageToVideo": {"title": "H3首帧图生视频（带音画）",
        "widgets": {"prompt": "提示词", "width": "宽", "height": "高",
                    "length": "帧数", "first_frame": "首帧"},
        "inputs": {"clip": "文本编码器", "vae": "画面VAE", "audio_vae": "音频VAE",
                   "first_frame": "首帧"},
        "outputs": {"positive": "正向条件", "LATENT": "音画潜空间"}},
    "MiniMaxH3AddGuide": {"title": "H3锚定注入（图像+音频）"},
    "MiniMaxH3SigmaShift": {"title": "Sigma偏移（官方12/3，勿动）",
        "widgets": {"shift": "偏移", "override": "覆盖"}},
    "MiniMaxH3LoopTrim": {"title": "裁头+音画校准", "widgets": {
        "trim_frames": "裁剪帧数", "fps": "帧率", "match_tail": "校准音画尾部",
        "retain_overlap_frames": "保留重叠帧"}},
    "MiniMaxH3ChainPlan": {"title": "① 分镜计划（整部片子的JSON）",
        "widgets": {"plan_json": "计划JSON", "run_name": "运行名",
                    "generation_fingerprint": "生成指纹", "width": "宽",
                    "height": "高", "context_length": "上下文帧数",
                    "encode_mode": "编码模式", "anchor_mode": "锚定模式",
                    "crop": "裁剪", "audio_mode": "音频模式",
                    "audio_context_length": "音频上下文帧数",
                    "default_duration_seconds": "默认每镜秒数",
                    "default_steps": "默认步数", "base_seed": "基础种子",
                    "segment_crf": "分段质量CRF",
                    "video_blend_frames": "拼装融合帧数",
                    "continuation_mode": "段间接续模式"},
        "outputs": {"PLAN": "计划"}},
    "MiniMaxH3ChainScenePromptEditor": {"title": "① 场景提示词编辑器"},
    "MiniMaxH3ChainPreflight": {"title": "③ 预检（加载模型前校验计划）",
        "inputs": {"plan": "计划"}},
    "MiniMaxH3ChainLoopStart": {"title": "③ 链循环起点（断点续跑）",
        "widgets": {"start_clip": "从第几镜开始", "checkpoint": "检查点"},
        "inputs": {"plan": "计划"}},
    "MiniMaxH3ChainCurrent": {"title": "③ 当前镜（下发提示词/宽高/帧数/种子/步数）",
        "outputs": {"SHOT": "当前镜", "PROMPT": "提示词", "SEED": "种子"}},
    "MiniMaxH3ChainContext": {"title": "③ 自动上下文（首镜旁路，续镜注入）",
        "inputs": {"conditioning": "条件", "latent": "潜空间",
                   "context_frames": "上下文帧", "context_latent": "上下文潜空间",
                   "audio_vae": "音频VAE", "context_audio": "上下文音频"},
        "outputs": {"conditioning": "条件", "trim_frames": "裁剪帧数"}},
    "MiniMaxH3ChainSegmentSave": {"title": "③ 分段落盘（成片+接续段）",
        "inputs": {"images": "图像", "audio": "音频"},
        "outputs": {"SEGMENT": "分段"}},
    "MiniMaxH3ChainLoopEnd": {"title": "③ 循环推进（末镜出清单）",
        "outputs": {"MANIFEST": "清单", "LOOP": "循环"}},
    "MiniMaxH3ChainAssemble": {"title": "④ 拼装成片（单条MP4带音轨）",
        "widgets": {"audio_source": "音轨来源", "filename": "文件名",
                    "audio_bitrate": "音轨码率"},
        "inputs": {"manifest": "清单"}, "outputs": {"VIDEO": "视频"}},
    "MiniMaxH3ChainManifestLoad": {"title": "④ 读取清单（恢复拼装）"},
    "MiniMaxH3ChainReview": {"title": "④ 审查门（每镜人工把关）",
        "widgets": {"enabled": "启用审查",
                    "play_notification_sound": "提示音",
                    "auto_continue_timeout_minutes": "自动继续(分钟)",
                    "unload_models_while_waiting": "等待时卸载权重",
                    "assemble_partial_on_stop": "停止时拼装已完成部分",
                    "partial_audio_source": "部分成片音轨"},
        "inputs": {"state": "状态", "segment": "分段"}},
    "MiniMaxH3ChainPolicy": {"title": "③ 链策略"},
}

TURBO = {
    "MiniMaxH3TurboSampler": {"title": "Turbo快采（4-6步）",
        "outputs": {"SAMPLER": "采样器"}},
    "MiniMaxH3TurboLoRA": {"title": "Turbo加速LoRA（v4-600）",
        "widgets": {"model": "模型", "lora_name": "LoRA文件",
                    "strength": "强度", "low_vram": "低显存合并"},
        "inputs": {"model": "模型"}, "outputs": {"MODEL": "模型"}},
}

IMPACT_ADD = {
    "ImpactExecutionOrderController": {"title": "执行顺序闸门（直通）",
        "inputs": {"signal": "信号", "value": "数值"},
        "outputs": {"signal": "信号", "value": "数值"}},
    "ImpactQueueTrigger": {"title": "队列触发器（自动排单）",
        "widgets": {"mode": "是否触发"}},
    "ImpactConditionalBranch": {"title": "条件二选一",
        "inputs": {"cond": "条件", "tt_value": "真值", "ff_value": "假值"}},
    "ImpactSetWidgetValue": {"title": "改写控件值",
        "widgets": {"node_id": "目标节点编号", "widget_name": "控件名称"}},
}

EASY_ADD = {
    "easy indexAnything": {"title": "万能索引选择器",
        "widgets": {"index": "索引", "any": "输入"},
        "inputs": {"any": "输入", "index": "索引"},
        "outputs": {"out": "输出"}},
    "easy compare": {"title": "数值比较",
        "widgets": {"comparison": "比较方式"}, "inputs": {"a": "数A", "b": "数B"}},
    "easy mathInt": {"title": "整数运算",
        "widgets": {"operation": "运算"}, "inputs": {"a": "数A", "b": "数B"}},
    "JoinStringMulti": {"title": "合并字符串列表",
        "widgets": {"inputcount": "接入条数", "delimiter": "分隔符",
                    "return_list": "输出为列表"}},
}

KJ_ADD = {
    "ImagePass": {"title": "图像直通", "inputs": {"image": "图像"}},
    "SaveImageKJ": {"title": "保存图像KJ", "widgets": {
        "filename_prefix": "保存前缀", "output_folder": "输出目录",
        "caption_file_extension": "说明文本后缀"}},
    "MiniMaxLowVRAMAttention": {"title": "低显存注意力（H3）",
        "widgets": {"head_chunks": "头分组数"}},
    "PathchSageAttentionKJ": {"title": "SageAttention提速",
        "widgets": {"sage_attention": "模式", "allow_compile": "允许编译"}},
    "ComfyMathExpression": {"title": "数学表达式",
        "widgets": {"expression": "表达式"}},
    "SomethingToString": {"title": "转文本", "widgets": {"prefix": "前缀",
        "suffix": "后缀", "input": "输入"}},
    "JoinStrings": {"title": "连接文本", "widgets": {"delimiter": "分隔符"},
        "inputs": {"string1": "串1", "string2": "串2"}},
}


def main() -> int:
    results = [
        load_merge(NODES / "internal.json", CORE_ADD),
        load_merge(NODES / "ComfyUI-MiniMaxH3-Contex-Loop.json", CTX_LOOP),
        load_merge(NODES / "comfyui-minimax-h3-turbo.json", TURBO),
        load_merge(NODES / "ComfyUI-Impact-Pack.json", IMPACT_ADD),
        load_merge(NODES / "ComfyUI-Easy-Use.json", EASY_ADD),
        load_merge(NODES / "ComfyUI-KJNodes.json", KJ_ADD),
    ]
    for r in results:
        print(r)
    # 启用中文语言
    settings = (PLUGIN.parent.parent / "user/default/comfy.settings.json")
    data = json.loads(settings.read_text(encoding="utf-8"))
    if str(data.get("Comfy.Locale")) != "zh":
        shutil.copy2(settings, str(settings) + BAK)
        data["Comfy.Locale"] = "zh"
        settings.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print("已设置 Comfy.Locale=zh（重启页面生效）")
    else:
        print("语言已是 zh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
