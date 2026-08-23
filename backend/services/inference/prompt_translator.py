"""OmniSpace AI 绘画提示词中译英模块。

背景：SDXL/SD1.5 等 CLIP 文本编码器只理解英文，中文描述词直接送入会
塌缩为模板词 + 噪声（漫库资产图"跟描述词不符"的根因）。

实现路径（按优先级）：
1. 对话引擎（Qwen3-VL 8B/4B，中英双语质量高）——已加载时零加载开销；
   未加载时经 ensure_loaded() 拉起，翻译后绘画引擎按需腾挪显存。
   调用方约定：翻译必须发生在 paint ensure_loaded() 之前，且批量生成
   时先在端点层整批译完再逐项生成，避免 dialog/paint 反复换载。
2. 对话引擎不可用 → 原样返回中文原文（诚实降级，不阻断生成；
   生成效果退化为模板词主导，与未接入翻译时一致）。

仅含 CJK 字符的输入才触发翻译，纯英文直通零开销。
"""

from __future__ import annotations

import logging
import re
import sys
import threading

logger = logging.getLogger("omnispace.inference.prompt_translator")


def shrink_working_set() -> None:
    """Windows：收缩本进程 working set（翻译路径资源卫生）。

    2026-08-23 实测：翻译用 transformers 后端对话引擎（qwen3-vl-4b，
    ~9GB），任务后腾挪逻辑只释放显存；torch/pymalloc 卸载后进程
    RAM 不归还 OS，python 进程滞留 ~9GB → 系统可用 RAM 被压低 →
    中文绘画任务永远过不了 qwen 双语底座的 RAM 门槛 → 永远走翻译
    兜底（恶性循环）。EmptyWorkingSet 把不再访问的死页挤出物理
    RAM（回落 pagefile/standby），调用点须在对话引擎卸载之后。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.psapi.EmptyWorkingSet(
            ctypes.windll.kernel32.GetCurrentProcess())
        logger.debug("working set 已收缩（翻译引擎死页挤出物理 RAM）")
    except Exception as exc:  # noqa: BLE001 - 收缩失败无碍主流程
        logger.debug("working set 收缩跳过: %s", exc)

# CJK 统一表意文字 + 常用中文标点
_CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")

_SYSTEM_PROMPT = (
    "你是绘画提示词翻译器。把用户的中文绘画描述翻译成英文 Stable Diffusion "
    "提示词。硬性要求：\n"
    "1. 总长度不超过 40 个英文单词——SDXL 文本编码器 77 token 硬截断，"
    "超出的内容会完全丢失；\n"
    "2. 逗号分隔的关键词/短语。第一个词组必须是画面主体（用户描述的核心"
    "人物/物体/身体部位）——SDXL 按词序分配注意力，主体前置才能锁定构图；"
    "若主体是人物某部位或物品的特写，主体用标签词组 + 构图强化词开头"
    "（如脚→'feet, foot focus'，手→'hands'，脸→'face, portrait'），"
    "标签写法对构图的控制力远强于自然语言描述。其后依次：主体属性"
    "（发色/服装）> 美术风格 > 用户显式指定的背景 > 其他细节"
    "（放不下从尾部舍弃）；\n"
    "3. 风格词翻译对照：中文\"真实/写实/照片感\"是绘画风格指令，译为 "
    "'realistic photo' 或 'photorealistic'（不是 real/true）；"
    "\"动漫/二次元/卡通\"译为 'anime style'/'cartoon'；\n"
    "4. 严格忠实原文：禁止添加原文未提及的元素（服饰/道具/背景一律不"
    "脑补）；一次只输出一种风格，写实与动漫词禁止同时出现；\n"
    "5. 忽略视图排版指令（如\"生成四视图\"\"正面侧面背面\"），版式由模板控制，"
    "只翻译外观、风格、背景描述；\n"
    "6. 只输出英文提示词本身，不要解释、不要输出中文、不要引号。"
)

_lock = threading.Lock()


def contains_cjk(text: str) -> bool:
    """文本是否含中文字符（决定是否需翻译）。"""
    return bool(text) and bool(_CJK_RE.search(text))


def _clean_output(text: str) -> str:
    """清洗翻译输出：去引号/换行/偶发前缀，压平为单行提示词。"""
    text = (text or "").strip().strip('"').strip("'").strip()
    text = re.sub(r"^(prompt|english|translation|英文(提示词)?)[:：]\s*",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\n\s*", ", ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().strip(",").strip()


def _dialog_translate(prompt: str, max_tokens: int,
                      system_prompt: str | None = None) -> str:
    """经对话引擎翻译；不可用/失败返回空串（调用方回退原文）。"""
    from .dialog_engine import get_dialog_engine
    engine = get_dialog_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        logger.warning("对话引擎不可用，提示词翻译跳过: %s",
                       engine.get_status().get("last_error"))
        return ""
    text = engine.chat(
        [{"role": "system", "content": system_prompt or _SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
        temperature=0.3,
        max_new_tokens=max_tokens,
    )
    return _clean_output(text)


def translate_prompt_zh2en(prompt: str, max_tokens: int = 120,
                           system_prompt: str | None = None) -> str:
    """中文提示词 → 英文提示词；非中文或失败时原样返回。

    Args:
        prompt: 用户原始描述词（可中可英）
        max_tokens: 翻译输出上限（系统提示词约束 ≤40 词，120 token 足够）
        system_prompt: 覆盖默认绘画模板（如视频生成需保留动作动词，
            2026-08-22 视频链路接入；None 用默认绘画模板）

    Returns:
        英文提示词；含 CJK 但翻译失败时返回原文（诚实降级）。
    """
    prompt = (prompt or "").strip()
    if not prompt or not contains_cjk(prompt):
        return prompt
    with _lock:
        try:
            translated = _dialog_translate(prompt, max_tokens,
                                           system_prompt=system_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("提示词翻译异常（回退原文）: %s", exc)
            return prompt
        # 翻译后仍含大量中文视为失败，回退原文
        if not translated or (
                contains_cjk(translated)
                and len(_CJK_RE.findall(translated)) > 8):
            logger.warning("提示词翻译结果异常（空或仍含中文），回退原文")
            return prompt
        logger.info("提示词中译英: %d 字 -> %d 字符",
                    len(prompt), len(translated))
        return translated


def translate_batch_zh2en(prompts: list[str],
                          max_tokens: int = 120) -> list[str]:
    """批量翻译（单锁整批，配合端点层"先整批译、再整批生成"的调用约定）。

    逐项复用 translate_prompt_zh2en 的降级语义；非中文项直通。
    """
    return [translate_prompt_zh2en(p, max_tokens) for p in prompts]
