"""人脸身份相似度门禁：YuNet 人脸检测 + DINOv2 嵌入余弦（2026-08-27 P0 落地）。

背景（V41 假阳性事故）：VLM 评审（Qwen2.5-VL 级）只能判别属性相似
（发型/瞳色/服装），对面骨结构身份（脸宽/下颌/眼距/鼻唇形态）无
区分力——面部结构明显漂移的镜头仍稳定输出 95 分。嵌入余弦距离是
硬指标：同人聚集、异人分离，不受「文本设定自相证明」污染。

技术选型：
- 检测：OpenCV YuNet（models/face/face_detection_yunet_2023mar.onnx，
  232KB，CPU 毫秒级；CG 正脸检测实测可用，侧脸弱→无检测回退 None）
- 嵌入：DINOv2-small（models/face/dinov2-small，~88MB，CPU 推理
  ~0.2s/图；结构相似度敏感、画风鲁棒，适配 CG 跨镜身份比对）
- 度量：生图人脸裁剪 vs 资产面部参考嵌入的最大余弦相似度

显存纪律：全程 CPU，不与 vLLM/ComfyUI/diffusers 争显存。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ...config import MODELS_DIR

logger = logging.getLogger("omnispace.inference.face_sim")

_YUNET_PATH = MODELS_DIR / "face" / "face_detection_yunet_2023mar.onnx"
_DINO_PATH = MODELS_DIR / "face" / "dinov2-small"
_FACE_MARGIN = 0.35        # 检测框外扩比例（含发际线/下颌上下文）
_YUNET_SCORE = 0.6

_lock = threading.Lock()
_detector = None           # cv2.FaceDetectorYN
_model = None              # DINOv2
_processor = None


def face_sim_available() -> bool:
    """依赖产物齐备（YuNet onnx + DINOv2 权重目录）。"""
    return _YUNET_PATH.is_file() and (_DINO_PATH / "config.json").is_file()


def _ensure_loaded() -> None:
    global _detector, _model, _processor
    if _detector is not None and _model is not None:
        return
    with _lock:
        if _detector is None:
            _detector = cv2.FaceDetectorYN_create(
                str(_YUNET_PATH), "", (320, 320), _YUNET_SCORE)
        if _model is None:
            from transformers import AutoImageProcessor, AutoModel
            _processor = AutoImageProcessor.from_pretrained(str(_DINO_PATH))
            _model = AutoModel.from_pretrained(str(_DINO_PATH))
            _model.eval()
            for p in _model.parameters():
                p.requires_grad_(False)
            logger.info("人脸相似度门禁就绪（YuNet + DINOv2-small, CPU）")


def _detect_face_crop(img: Image.Image) -> Image.Image | None:
    """最大人脸框外扩裁剪；无检测返回 None。"""
    _ensure_loaded()
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    _detector.setInputSize((w, h))
    _, faces = _detector.detect(bgr)
    if faces is None or len(faces) == 0:
        return None
    # 取最大面积人脸
    areas = faces[:, 2] * faces[:, 3]
    x, y, fw, fh = faces[int(np.argmax(areas))][:4]
    mx, my = fw * _FACE_MARGIN, fh * _FACE_MARGIN
    x0 = max(0, int(x - mx))
    y0 = max(0, int(y - my))
    x1 = min(w, int(x + fw + mx))
    y1 = min(h, int(y + fh + my))
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    return img.crop((x0, y0, x1, y1))


def _embed(img: Image.Image) -> np.ndarray:
    """DINOv2 CLS 嵌入，L2 归一化。"""
    import torch
    _ensure_loaded()
    inputs = _processor(images=img.convert("RGB"), return_tensors="pt")
    with torch.inference_mode():
        out = _model(**inputs)
    vec = out.last_hidden_state[0, 0].numpy()  # CLS token
    return vec / (np.linalg.norm(vec) + 1e-8)


def embed_face(img: Image.Image) -> np.ndarray | None:
    """检测并嵌入人脸；无检测回退整图嵌入（特写资产图本身就是脸）。"""
    crop = _detect_face_crop(img)
    return _embed(crop if crop is not None else img)


def face_similarity(shot_path: str | Path,
                    ref_vecs: list[np.ndarray]) -> float | None:
    """生图镜头帧 vs 资产人脸嵌入列表的最大余弦相似度。

    Returns:
        相似度 0~1；生图中未检测到人脸返回 None（调用方按维度
        不可用处理，不得按 0 分判负——侧脸/远景无脸帧属正常）。
    """
    if not ref_vecs:
        return None
    try:
        img = Image.open(shot_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("相似度评分读图失败 %s: %s", shot_path, exc)
        return None
    crop = _detect_face_crop(img)
    if crop is None:
        return None
    v = _embed(crop)
    return float(max(float(np.dot(v, r)) for r in ref_vecs))


# ═════════════════════════════════════════════════════════════════
#  人脸身份硬度量：ArcFace（glintr100）直跑（2026-08-28 P0-2）
# ═════════════════════════════════════════════════════════════════
#  背景（V41→V80 标定史）：DINOv2 是通用图像嵌入而非人脸识别模型
#  ——同人聚集/异人分离的分离度不足，face 门禁阈值在 0.70~0.75 间
#  反复摇摆仍拦不住「微妙变脸」（日志实测同 prompt 同镜 face_sim
#  0.63~0.91 随机波动）。ArcFace（antelopev2/glintr100，512 维身份
#  嵌入）是人脸识别工业标准度量，且权重已随 PuLID 落盘
#  （models/face/insightface/antelopev2/）——本模块经 onnxruntime
#  直跑（后端 runtime 无 insightface 包，且避免与其 CPU 绑定冲突）。
#
#  对齐：复用 YuNet 检测的 5 点关键帧（双眼/鼻尖/双嘴角）→ 相似
#  变换到 112×112 标准五点模板（insightface arcface_dst）→
#  glintr100 推理（BGR，(x-127.5)/127.5，与 insightface 预处理一致）。
#  显存纪律不变：全程 CPU onnxruntime，不与 vLLM/ComfyUI/diffusers
#  争显存。DINOv2 通道保留（scene 门禁 + ArcFace 不可用时的回退）。

_ARC_RECOG = MODELS_DIR / "face" / "insightface" / "antelopev2" / "glintr100.onnx"
_ARC_SIZE = (112, 112)
# insightface 标准五点模板（左眼/右眼/鼻尖/左嘴角/右嘴角，图像坐标序）
_ARC_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)

_arc_lock = threading.Lock()
_arc_session = None        # onnxruntime InferenceSession（glintr100）


def arc_available() -> bool:
    """ArcFace 识别权重是否就绪（缺权重时门禁回退 DINOv2 通道）。"""
    return _ARC_RECOG.is_file()


def _ensure_arc_loaded():
    global _arc_session
    if _arc_session is not None:
        return _arc_session
    with _arc_lock:
        if _arc_session is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            _arc_session = ort.InferenceSession(
                str(_ARC_RECOG), sess_options=opts,
                providers=["CPUExecutionProvider"])
            logger.info("ArcFace 识别通道就绪（glintr100, CPU onnxruntime）")
    return _arc_session


def _detect_faces(img: Image.Image) -> list[np.ndarray]:
    """YuNet 检测全部人脸，返回原始 15 元组行（框+5 点+分数）。

    关键帧帧内可能有多人（双人对话镜）——身份匹配需逐脸嵌入，
    不再像 embed_face 只取最大脸。
    """
    _ensure_loaded()
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    _detector.setInputSize((w, h))
    _, faces = _detector.detect(bgr)
    if faces is None:
        return []
    return [f for f in faces if f[14] >= _YUNET_SCORE]


def _norm_crop(img: Image.Image, row: np.ndarray) -> Image.Image:
    """YuNet 15 元组行 → 五点相似变换对齐 112×112（ArcFace 标准输入）。

    眼/嘴角按图像 x 排序消除「左右眼」命名歧义（镜像自拍/侧脸
    检测的标记序与模板序解耦）。"""
    pts = row[4:14].reshape(5, 2).astype(np.float32)
    eyes = pts[0:2][np.argsort(pts[0:2, 0])]
    mouth = pts[3:5][np.argsort(pts[3:5, 0])]
    src = np.stack([eyes[0], eyes[1], pts[2], mouth[0], mouth[1]])
    m, _ = cv2.estimateAffinePartial2D(
        src.reshape(-1, 1, 2), _ARC_DST.reshape(-1, 1, 2),
        method=cv2.LMEDS)
    if m is None:
        return img.resize(_ARC_SIZE, Image.LANCZOS)
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    warped = cv2.warpAffine(bgr, m, _ARC_SIZE, flags=cv2.INTER_LINEAR,
                            borderValue=(0, 0, 0))
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))


def _arc_embed(img: Image.Image) -> np.ndarray:
    """112×112 对齐脸 → glintr100 512 维身份嵌入（L2 归一化）。"""
    sess = _ensure_arc_loaded()
    inp = sess.get_inputs()[0]
    x = np.asarray(img.convert("RGB").resize(_ARC_SIZE, Image.LANCZOS))
    blob = cv2.cvtColor(x, cv2.COLOR_RGB2BGR).transpose(2, 0, 1)[None]
    blob = (blob.astype(np.float32) - 127.5) / 127.5
    out = sess.run(None, {inp.name: blob})[0][0]
    return out / (np.linalg.norm(out) + 1e-8)


def embed_faces_arc(img: Image.Image) -> list[np.ndarray]:
    """帧内全部人脸的 ArcFace 嵌入列表（无检测返回空列表）。"""
    if not arc_available():
        return []
    return [_arc_embed(_norm_crop(img, row)) for row in _detect_faces(img)]


def embed_face_arc(img: Image.Image) -> np.ndarray | None:
    """资产参考图的单人 ArcFace 嵌入（取最大脸；无检测回退 None）。"""
    rows = _detect_faces(img)
    if not rows:
        return None
    best = max(rows, key=lambda r: r[2] * r[3])
    return _arc_embed(_norm_crop(img, best))


def arcface_match(shot_path: str | Path,
                  ref_vecs: list) -> tuple[list, int]:
    """逐角色身份匹配：每个角色基准嵌入对帧内全部检测脸取最大余弦。

    Args:
        ref_vecs: 每个绑定角色一个 512 维基准嵌入；缺失基准为 None
            （该角色照实返回 None，不阻断其他角色）。
    Returns:
        (sims, face_count)：sims 与 ref_vecs 等长；帧内无检测脸或
        ArcFace 不可用时逐角色 None（调用方按维度不可用处理，不按
        0 判负——侧脸/远景属正常）。face_count 供多实例告警观察
        （v26 事故「参考主体被复制成多人」的可见信号）。
    """
    if not arc_available():
        return [None] * len(ref_vecs), 0
    try:
        img = Image.open(shot_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ArcFace 匹配读图失败 %s: %s", shot_path, exc)
        return [None] * len(ref_vecs), 0
    faces = embed_faces_arc(img)
    if not faces:
        return [None] * len(ref_vecs), 0
    sims = [None if r is None else
            float(max(float(np.dot(v, r)) for v in faces))
            for r in ref_vecs]
    return sims, len(faces)


# ── 场景一致性门禁（2026-08-27 P0：V46 场景漂移无门禁事故）───────
# 全图嵌入（无人脸检测）。景别混淆警示：特写镜满屏人脸场景相似度
# 天然 0.03~0.09、远景 0.6~0.88（v44/v45/v46 标定）——不得全镜头
# 一刀切；仅对无人脸镜头（face_sim=None 的远景/空镜，场景主导帧）
# 作硬门禁，有人脸镜头由人脸门禁负责。

def embed_scene(img: Image.Image) -> np.ndarray:
    """场景全图嵌入（DINOv2 CLS，L2 归一化）。"""
    return _embed(img)


def scene_similarity(shot_path: str | Path,
                     ref_vec: np.ndarray | None) -> float | None:
    """生图镜头帧全图嵌入 vs 场景资产图嵌入的余弦相似度。

    Returns:
        相似度 -1~1（余弦）；ref 或读图缺失返回 None（门禁降级，
        不得按 0 判负——无场景绑定的行属正常配置）。
    """
    if ref_vec is None:
        return None
    try:
        img = Image.open(shot_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("场景相似度评分读图失败 %s: %s", shot_path, exc)
        return None
    return float(np.dot(_embed(img), ref_vec))
