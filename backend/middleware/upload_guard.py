"""上传文件类型闸门（TASK-P0-03，审计发现 P27）。

三个上传端点（knowledge/import-document、learn/dataset/upload、style/upload）
此前只查大小不查类型，伪装扩展名的任意文件可落盘。本模块提供双层校验：

  第一层（后缀白名单）：扩展名必须命中端点声明表，无扩展名直接拒绝
  第二层（内容嗅验）：文件头魔数须与扩展名类别匹配；
                      纯文本类验"首 512 字节无 NUL"（UTF-16 编码文本会被拒，
                      错误信息引导转 UTF-8）

绝对黑名单：PE/ELF/Mach-O 可执行体（MZ 头等）无论声明何种类别一律拒绝——
这是 ".exe 改名 .jsonl" 攻击面的核心拦截层。

拒绝方式：抛 UploadRejected(ValueError 子类)，由端点转 ApiError 走统一信封。
本模块不依赖 FastAPI，可独立单测。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

Sniffer = Callable[[bytes], bool]


class UploadRejected(ValueError):
    """上传文件未通过类型闸门；message 面向用户，reason 供日志与测试断言。"""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


# ── 可执行体黑名单（PE32/PE32+/ELF/Mach-O fat/Mach-O）──────────────
_EXEC_MAGICS: tuple[bytes, ...] = (
    b"MZ",                    # PE（.exe/.dll/.sys）
    b"\x7fELF",               # Linux ELF
    b"\xca\xfe\xba\xbe",      # Mach-O fat / Java class
    b"\xfe\xed\xfa\xce",      # Mach-O 32
    b"\xfe\xed\xfa\xcf",      # Mach-O 64
    b"\xcf\xfa\xed\xfe",      # Mach-O 64 反向
)

# ── 魔数嗅验器 ─────────────────────────────────────────────────────
def _pdf(d: bytes) -> bool:      return d[:5] == b"%PDF-"
def _zip(d: bytes) -> bool:      return d[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
def _png(d: bytes) -> bool:      return d[:8] == b"\x89PNG\r\n\x1a\n"
def _jpeg(d: bytes) -> bool:     return d[:3] == b"\xff\xd8\xff"
def _webp(d: bytes) -> bool:     return d[:4] == b"RIFF" and d[8:12] == b"WEBP"
def _bmp(d: bytes) -> bool:      return d[:2] == b"BM"
def _ftyp(d: bytes) -> bool:     return d[4:8] == b"ftyp"          # mp4/mov/m4v
def _ebml(d: bytes) -> bool:     return d[:4] == b"\x1a\x45\xdf\xa3"  # webm/mkv
def _avi(d: bytes) -> bool:      return d[:4] == b"RIFF" and d[8:12] == b"AVI "
def _utf8_text(d: bytes) -> bool:
    if d[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False          # UTF-16/32 BOM：非 UTF-8 文本
    return b"\x00" not in d[:512]


# ── 端点类别表（扩展名 → 允许的嗅验器）────────────────────────────
# knowledge/import-document：与 api/knowledge.py _SUPPORTED_EXTS 对齐
DOCUMENT_TABLE: dict[str, tuple[Sniffer, ...]] = {
    ".pdf":  (_pdf,),
    ".docx": (_zip,),
    ".txt":  (_utf8_text,),
    ".md":   (_utf8_text,),
}

# learn/dataset/upload：训练数据集（与原端点后缀白名单对齐）
DATASET_TABLE: dict[str, tuple[Sniffer, ...]] = {
    ".jsonl": (_utf8_text,),
    ".json":  (_utf8_text,),
    ".txt":   (_utf8_text,),
}

# style/upload：与 style_lora_service.VIDEO_EXTS/IMAGE_EXTS 对齐
MEDIA_TABLE: dict[str, tuple[Sniffer, ...]] = {
    ".png":  (_png,),
    ".jpg":  (_jpeg,),
    ".jpeg": (_jpeg,),
    ".webp": (_webp,),
    ".bmp":  (_bmp,),
    ".mp4":  (_ftyp,),
    ".webm": (_ebml,),
    ".mov":  (_ftyp,),
    ".mkv":  (_ebml,),
    ".avi":  (_avi,),
}


def validate(filename: str, data: bytes,
             table: dict[str, tuple[Sniffer, ...]]) -> None:
    """校验上传文件；不通过抛 UploadRejected，通过则静默返回。

    空数据由调用方先行校验（端点已有"内容为空"检查），此处只管类型。
    """
    ext = Path(filename).suffix.lower()
    if ext not in table:
        raise UploadRejected(
            f"不支持的文件类型: {ext or '(无扩展名)'}；"
            f"允许 {sorted(table)}", reason="ext")
    if any(data.startswith(m) for m in _EXEC_MAGICS):
        raise UploadRejected(
            f"文件内容为可执行程序，已拒绝（扩展名 {ext} 与内容不符）",
            reason="exec")
    for sniffer in table[ext]:
        if sniffer(data):
            return
    raise UploadRejected(
        f"文件内容与扩展名 {ext} 不符（二进制伪装、文件损坏或文本非 UTF-8 编码）",
        reason="mismatch")
