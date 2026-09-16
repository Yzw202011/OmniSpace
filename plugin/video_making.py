"""VideoMakingPlugin: 轻量视频生成内核思考插件 (v0.8.7)

面向宿主 (如 OmniSpace) 的**无 GPU 视频生成内核**: 不依赖扩散模型
/ ComfyUI / 显存调度, 纯 numpy 帧合成管线 —— 关键帧 + 镜头运动曲线
→ 缓动采样 → 仿射变换帧 → 转场合成 → 帧序列。

    输入 spec (事件 data):
        {"keyframes": [HxW(3) ndarray, ...],      # 关键帧 (漫剧关键帧/分镜图)
         "shots": [{"motion": "zoom_in",           # 每镜头运动曲线
                    "duration_s": 2.0,
                    "transition": "crossfade"}],
         "fps": 12}

    生成路径 (全部 O(H*W) numpy, 零外部依赖):
        运动曲线 (pan/dolly/zoom/orbit/static) × 缓动 (smoothstep)
        → 每帧逆仿射采样 (双线性) → 镜头间 crossfade / dip-to-black

route = "video": on_think 渲染帧序列并发布 "video.rendered"
(只广播摘要与统计, 不往总线塞帧本体)。

权重 = 镜头运动曲线参数表 (motion profile), 可随 .CuteMamen 存档;
未注入权重时使用内置默认运动曲线 (真实运镜参数蒸馏自分镜惯例)。
"""

from typing import Any

import numpy as np

from src.cutemamen.plugin import ExpertPlugin, PluginContext

# ── 内置镜头运动曲线 (真实运镜惯例参数: 每镜头归一化位移/缩放/旋转) ──
# dx/dy: 归一化平移 (画面宽高比例), zoom: 1.0 = 不缩放,
# rot: 度; "from" → 镜头起点, "to" → 镜头终点 (缓动插值)。
DEFAULT_MOTION_PROFILES: dict[str, dict[str, Any]] = {
    "static":      {"from": {"dx": 0.00, "dy": 0.00, "zoom": 1.00, "rot": 0.0},
                    "to":   {"dx": 0.00, "dy": 0.00, "zoom": 1.00, "rot": 0.0}},
    "pan_left":    {"from": {"dx": 0.08, "dy": 0.00, "zoom": 1.05, "rot": 0.0},
                    "to":   {"dx": -0.08, "dy": 0.00, "zoom": 1.05, "rot": 0.0}},
    "pan_right":   {"from": {"dx": -0.08, "dy": 0.00, "zoom": 1.05, "rot": 0.0},
                    "to":   {"dx": 0.08, "dy": 0.00, "zoom": 1.05, "rot": 0.0}},
    "tilt_up":     {"from": {"dx": 0.00, "dy": 0.08, "zoom": 1.05, "rot": 0.0},
                    "to":   {"dx": 0.00, "dy": -0.08, "zoom": 1.05, "rot": 0.0}},
    "tilt_down":   {"from": {"dx": 0.00, "dy": -0.08, "zoom": 1.05, "rot": 0.0},
                    "to":   {"dx": 0.00, "dy": 0.08, "zoom": 1.05, "rot": 0.0}},
    "zoom_in":     {"from": {"dx": 0.00, "dy": 0.00, "zoom": 1.00, "rot": 0.0},
                    "to":   {"dx": 0.00, "dy": 0.00, "zoom": 1.18, "rot": 0.0}},
    "zoom_out":    {"from": {"dx": 0.00, "dy": 0.00, "zoom": 1.18, "rot": 0.0},
                    "to":   {"dx": 0.00, "dy": 0.00, "zoom": 1.00, "rot": 0.0}},
    "dolly_in":    {"from": {"dx": 0.00, "dy": 0.00, "zoom": 1.02, "rot": 0.0},
                    "to":   {"dx": 0.00, "dy": -0.04, "zoom": 1.15, "rot": 0.0}},
    "orbit_right": {"from": {"dx": -0.06, "dy": 0.00, "zoom": 1.08, "rot": -1.5},
                    "to":   {"dx": 0.06, "dy": 0.00, "zoom": 1.08, "rot": 1.5}},
    "orbit_left":  {"from": {"dx": 0.06, "dy": 0.00, "zoom": 1.08, "rot": 1.5},
                    "to":   {"dx": -0.06, "dy": 0.00, "zoom": 1.08, "rot": -1.5}},
}

TRANSITIONS = ("cut", "crossfade", "dip_to_black")
EASINGS = ("smoothstep", "linear", "ease_out")

DEFAULT_FPS = 12
DEFAULT_DURATION_S = 2.0
MAX_FRAMES_PER_SHOT = 600   # 50s @ 12fps, 防失控


class VideoMakingPlugin(ExpertPlugin):
    """轻量视频生成内核: 关键帧 → 运动曲线帧序列 (纯 numpy, 无 GPU)

    事件格式: {"topic": "video", "data": spec} (spec 见模块 docstring;
    也接受 {"spec": spec} 包装)。返回渲染统计与镜头计划, 帧本体通过
    返回值的 "frames" 键交给调用方, 总线只广播摘要。
    """

    BASE_MODEL = "video.making"
    CAPABILITY = ("轻量视频生成内核: 关键帧 + 镜头运动曲线 → 缓动仿射"
                  "帧序列 + 转场合成 (纯 numpy, 零 GPU / 零外部依赖)")

    def __init__(self, name: str = "video-making", *,
                 route: str | None = None, **kwargs):
        super().__init__(name, route=route or "video", **kwargs)
        # 运动曲线权重 (可存档): motion → profile; 未注入用内置默认
        self.motion_profiles: dict[str, dict[str, Any]] = {}
        self.render_count = 0
        self.total_frames = 0

    # ── 运动曲线 ────────────────────────────────────────────
    def _profile(self, motion: str) -> dict[str, Any]:
        if motion in self.motion_profiles:
            return self.motion_profiles[motion]
        return DEFAULT_MOTION_PROFILES.get(
            motion, DEFAULT_MOTION_PROFILES["static"])

    def available_motions(self) -> list[str]:
        merged = dict(DEFAULT_MOTION_PROFILES)
        merged.update(self.motion_profiles)
        return sorted(merged)

    # ── 生命周期 ────────────────────────────────────────────
    def on_load(self, ctx: PluginContext) -> None:
        self.memory.set("base_model", self.BASE_MODEL)
        self.memory.set("motions", self.available_motions())
        self.memory.set("transitions", list(TRANSITIONS))
        super().on_load(ctx)

    def on_think(self, event: dict[str, Any],
                 ctx: PluginContext) -> dict[str, Any] | None:
        """渲染一段视频: spec → 帧序列 + 统计"""
        super().on_think(event, ctx)
        spec = _extract_spec(event.get("data"))
        if spec is None:
            return None
        frames, plan = self.render(spec)
        self.render_count += 1
        self.total_frames += len(frames)
        summary = {
            "n_frames": len(frames),
            "duration_s": round(len(frames) / float(spec.get("fps",
                                    DEFAULT_FPS)), 3),
            "shots": [p["motion"] for p in plan],
            "shape": list(frames[0].shape) if frames else None,
            "kernel": "lightweight-numpy-v1",
        }
        if ctx is not None:
            ctx.emit("video.rendered", summary)
        return {"summary": summary, "plan": plan, "frames": frames}

    def on_unload(self) -> None:
        self.memory.consolidate()
        self.memory.remember("total_frames", self.total_frames)
        super().on_unload()

    # ── 渲染内核 ────────────────────────────────────────────
    def render(self, spec: dict[str, Any]
               ) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
        """spec → (帧序列, 镜头计划)

        每个镜头从对应关键帧出发, 按运动曲线在 duration 内逐帧逆仿射
        采样 (双线性); 镜头之间按 transition 合成。
        """
        keyframes = [_as_frame(k) for k in spec.get("keyframes", [])]
        if not keyframes:
            return [], []
        fps = max(1, int(spec.get("fps", DEFAULT_FPS)))
        shots_spec = spec.get("shots") or [{"motion": "static"}]
        shots_spec = shots_spec[:len(keyframes)] or [{"motion": "static"}]

        frames: list[np.ndarray] = []
        plan: list[dict[str, Any]] = []
        prev_last: np.ndarray | None = None
        for i, shot in enumerate(shots_spec):
            motion = str(shot.get("motion", "static"))
            duration_s = float(shot.get("duration_s", DEFAULT_DURATION_S))
            n = int(min(max(1, round(duration_s * fps)),
                        MAX_FRAMES_PER_SHOT))
            transition = str(shot.get("transition",
                                      "crossfade" if i else "cut"))
            if transition not in TRANSITIONS:
                transition = "crossfade"
            easing = str(shot.get("easing", "smoothstep"))
            if easing not in EASINGS:
                easing = "smoothstep"
            key = keyframes[min(i, len(keyframes) - 1)]

            shot_frames = [_affine_frame(key, *_camera_at(
                self._profile(motion), _ease(t / (n - 1) if n > 1 else 1.0,
                                             easing)))
                for t in range(n)]
            # 转场: 与上一镜头末帧合成 (前 30% 帧渐变)
            if prev_last is not None and transition != "cut" and len(frames):
                shot_frames = _apply_transition(
                    prev_last, shot_frames, transition)
            frames.extend(shot_frames)
            prev_last = shot_frames[-1]
            plan.append({"motion": motion, "frames": n,
                         "duration_s": round(n / fps, 3),
                         "transition": transition, "easing": easing,
                         "keyframe_index": min(i, len(keyframes) - 1)})
        return frames, plan

    # ── 权重序列化 (运动曲线参数表) ──────────────────────────
    def save_weights(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for motion, prof in self.motion_profiles.items():
            out[f"prof_{motion}"] = np.array(
                [[prof["from"]["dx"], prof["from"]["dy"],
                  prof["from"]["zoom"], prof["from"]["rot"]],
                 [prof["to"]["dx"], prof["to"]["dy"],
                  prof["to"]["zoom"], prof["to"]["rot"]]], dtype=float)
        out["__stats__"] = np.array(
            [self.render_count, self.total_frames], dtype=int)
        return out

    def load_weights(self, weights: dict[str, np.ndarray],
                     manifest: dict[str, Any]) -> None:
        self.motion_profiles = {}
        for key, arr in weights.items():
            if not key.startswith("prof_"):
                continue
            motion = key[len("prof_"):]
            a = np.asarray(arr, dtype=float).reshape(2, 4)
            self.motion_profiles[motion] = {
                "from": {"dx": a[0, 0], "dy": a[0, 1],
                         "zoom": a[0, 2], "rot": a[0, 3]},
                "to":   {"dx": a[1, 0], "dy": a[1, 1],
                         "zoom": a[1, 2], "rot": a[1, 3]},
            }
        if "__stats__" in weights:
            self.render_count = int(weights["__stats__"][0])
            self.total_frames = int(weights["__stats__"][1])

    def build_manifest(self, **extra: Any) -> dict[str, Any]:
        extra.setdefault("capability", self.CAPABILITY)
        extra.setdefault("motions", self.available_motions())
        extra.setdefault("transitions", list(TRANSITIONS))
        extra.setdefault("kernel", "lightweight-numpy-v1")
        extra.setdefault("gpu_required", False)
        return super().build_manifest(**extra)

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        s["motions"] = self.available_motions()
        s["render_count"] = self.render_count
        s["total_frames"] = self.total_frames
        s["gpu_required"] = False
        return s


# ═══════════════════════════════════════════════════════════════
# 渲染内核辅助 (纯 numpy, 无 scipy/cv2)
# ═══════════════════════════════════════════════════════════════

def _extract_spec(data: Any) -> dict[str, Any] | None:
    """从事件取渲染 spec: 支持 spec 本体或 {"spec": spec} 包装"""
    if isinstance(data, dict) and "keyframes" in data:
        return data
    if isinstance(data, dict) and isinstance(data.get("spec"), dict):
        return data["spec"]
    return None


def _as_frame(x: Any) -> np.ndarray:
    """关键帧归一化: → float64 HxW 或 HxWxC, 值域 [0,1]"""
    a = np.asarray(x, dtype=np.float64)
    if a.ndim == 1:                       # 平铺向量 → 单通道方形帧
        side = max(2, int(round(np.sqrt(a.size))))
        a = a[: side * side].reshape(side, side)
    elif a.ndim != 2 and a.ndim != 3:
        raise ValueError(f"关键帧维度不支持: {a.shape}")
    if a.max() > 1.5:                     # uint8 值域 → [0,1]
        a = a / 255.0
    return a


def _ease(t: float, kind: str = "smoothstep") -> float:
    """缓动曲线: t∈[0,1] → 缓动后的进度"""
    t = float(min(max(t, 0.0), 1.0))
    if kind == "linear":
        return t
    if kind == "ease_out":
        return 1.0 - (1.0 - t) ** 3
    return t * t * (3.0 - 2.0 * t)        # smoothstep


def _camera_at(profile: dict[str, Any], p: float
               ) -> tuple[float, float, float, float]:
    """运动曲线插值: 进度 p → (dx, dy, zoom, rot)"""
    f, to = profile["from"], profile["to"]
    return (f["dx"] + (to["dx"] - f["dx"]) * p,
            f["dy"] + (to["dy"] - f["dy"]) * p,
            f["zoom"] + (to["zoom"] - f["zoom"]) * p,
            f["rot"] + (to["rot"] - f["rot"]) * p)


def _affine_frame(frame: np.ndarray, dx: float, dy: float,
                  zoom: float, rot_deg: float) -> np.ndarray:
    """逆仿射采样一帧: 相机 (平移/缩放/旋转) → 双线性插值输出

    dx/dy 为归一化平移 (画面宽高比例), zoom>1 放大 (取中间区域),
    rot_deg 绕画面中心旋转; 画面外以边缘像素填充 (clamp)。
    """
    h, w = frame.shape[:2]
    theta = np.deg2rad(rot_deg)
    cos, sin = np.cos(theta), np.sin(theta)
    # 目标网格 → 源坐标 (逆变换: 平移→旋转→缩放, 均绕中心)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    xs = (xs - w / 2) / w
    ys = (ys - h / 2) / h
    # 逆缩放
    xs, ys = xs / max(zoom, 1e-6), ys / max(zoom, 1e-6)
    # 逆旋转
    rx = cos * xs + sin * ys
    ry = -sin * xs + cos * ys
    # 逆平移
    src_x = ((rx - dx) * w + w / 2)
    src_y = ((ry - dy) * h + h / 2)
    # 双线性采样 (clamp 边界)
    x0 = np.clip(np.floor(src_x).astype(np.int64), 0, w - 1)
    y0 = np.clip(np.floor(src_y).astype(np.int64), 0, h - 1)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    fx = np.clip(src_x - x0, 0.0, 1.0)[:, :, None]
    fy = np.clip(src_y - y0, 0.0, 1.0)[:, :, None]
    if frame.ndim == 2:
        f = frame[:, :, None]
    else:
        f = frame
    top = f[y0, x0] * (1 - fx) + f[y0, x1] * fx
    bot = f[y1, x0] * (1 - fx) + f[y1, x1] * fx
    out = top * (1 - fy) + bot * fy
    return out[:, :, 0] if frame.ndim == 2 else out


def _apply_transition(prev_last: np.ndarray,
                      shot_frames: list[np.ndarray],
                      transition: str) -> list[np.ndarray]:
    """镜头间转场: crossfade (叠化) / dip_to_black (黑场过渡)"""
    n = max(2, len(shot_frames) // 3)   # 前 1/3 帧做转场
    n = min(n, len(shot_frames))
    ref = _match_shape(prev_last, shot_frames[0].shape)
    for i in range(n):
        a = (i + 1) / (n + 1)
        if transition == "dip_to_black":
            shot_frames[i] = shot_frames[i] * a
        else:                           # crossfade
            shot_frames[i] = ref * (1 - a) + shot_frames[i] * a
    return shot_frames


def _match_shape(frame: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    """把参考帧裁/扩到目标形状 (转场合成用, 最近邻)"""
    if frame.shape == tuple(shape):
        return frame
    h, w = shape[:2]
    fh, fw = frame.shape[:2]
    ys = np.linspace(0, fh - 1, h).astype(np.int64)
    xs = np.linspace(0, fw - 1, w).astype(np.int64)
    out = frame[np.ix_(ys, xs)]
    if out.ndim == 2 and len(shape) == 3:
        out = np.repeat(out[:, :, None], shape[2], axis=2)
    elif out.ndim == 3 and len(shape) == 2:
        out = out.mean(axis=2)
    return out
