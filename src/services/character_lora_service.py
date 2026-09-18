# 本项目仅供学习使用，商业授权请+Q 3559331368
"""人物 LoRA 训练服务（P3 训练中心·人物页签，2026-09-17 用户拍板 3A）。

给漫画/漫剧角色资产练「专属脸蛋」：以 flux2-klein-4b（diffusers 图像
DiT）为底座的 QLoRA 管线，素材直接取 comic_assets 角色资产
（file_path 主图 + meta.views 四视图），练出的 adapter 部署为资产旁
``lora.safetensors``——正好落在 keyframe.py 的 D-LoRA 自动检测约定上
（资产旁 lora.safetensors → 生图时身份硬锁挂载），消费链零改动。

架构对齐 style_lora_service（单 worker 线程 + DB 任务表 + 版本目录 +
current 指针 + 功能锁互斥）；训练核心按其 LTX 视频版同构改写为
Flux 2D 图像版（flow-matching 目标，注意力 q/k/v/o + ff 注入）。

诚实边界（与风格服务同款声明）：
- 文本条件：klein 的 Qwen3-4B 编码器进显存代价高，训练条件先用与
  style 服务同口径的「角色名确定性哈希嵌入」（同一角色训练信号一致，
  身份特征学习不受影响）；接真编码器为后续增强点；
- 评估：无 GPU 推理时为结构化启发式分（adapter 完整性 + 数据规模）。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import ROOT_DIR
from .training_common import TrainingLockGuard, VersionStore

log = logging.getLogger("omnispace.services.character_lora")

# 底座：FLUX.2 Klein 4b（diffusers 完整布局：transformer/vae/...）
BASE_MODEL_ID = "flux2-klein-4b"
BASE_MODEL_DIR = ROOT_DIR / "models" / "paint" / "flux2-klein-4b"
# 版本根（git 不跟踪的运行时区）
VERSIONS_ROOT = ROOT_DIR / "data" / "character_lora"
# 最少训练图数（主图+四视图 ≥4 才够练身份）
MIN_IMAGES = 4
# 版本保留上限
KEEP_VERSIONS = 3

STATUS_QUEUED = "queued"
STATUS_TRAINING = "training"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


class CharacterTrainingFailed(RuntimeError):
    """人物 LoRA 训练失败（原因如实上报）。"""


class CharacterTrainingCancelled(RuntimeError):
    """训练被用户取消。"""


def _try_import(name: str) -> Any:
    try:
        return __import__(name)
    except Exception:  # noqa: BLE001 - 缺依赖是预期分支（训练路径才需要）
        return None


def _get_db() -> Any:
    """DB 句柄（Database 包装类；降级 None 走内存镜像）。"""
    try:
        from ..data.database import get_db_safe
        return get_db_safe()
    except Exception:  # noqa: BLE001 - DB 降级走内存镜像
        return None


class CharacterLoraService:
    """人物 LoRA：任务队列（单 worker）+ 版本管理 + 部署到资产旁。"""

    _instance: CharacterLoraService | None = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> CharacterLoraService:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._queue: list[dict[str, Any]] = []
        self._queue_lock = threading.Lock()
        self._cancel_flags: set[str] = set()
        self._mem_tasks: dict[str, dict[str, Any]] = {}
        self._lock_guard = TrainingLockGuard()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._table_ready = self._ensure_table()

    # ── 任务表 ──────────────────────────────────────────────
    def _ensure_table(self) -> bool:
        db = _get_db()
        if db is None:
            return False
        try:
            db.sql("""CREATE TABLE IF NOT EXISTS char_lora_tasks (
                id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL,
                name TEXT,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                error TEXT,
                version TEXT,
                created_at REAL,
                updated_at REAL)""")
            return True
        except Exception:  # noqa: BLE001 - 建表失败走内存镜像
            log.warning("char_lora_tasks 建表失败，降级内存镜像",
                           exc_info=True)
            return False

    def _upsert_task(self, task: dict[str, Any]) -> None:
        task["updated_at"] = time.time()
        self._mem_tasks[task["id"]] = dict(task)
        db = _get_db()
        if db is not None and self._table_ready:
            try:
                db.sql(
                    "INSERT OR REPLACE INTO char_lora_tasks"
                    " (id, asset_id, name, status, progress, error, version,"
                    "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (task["id"], task["asset_id"], task["name"],
                     task["status"], task["progress"], task["error"],
                     task["version"], task["created_at"], task["updated_at"]))
            except Exception:  # noqa: BLE001 - 写失败保内存镜像
                log.debug("char task 写库失败", exc_info=True)

    def list_tasks(self) -> list[dict[str, Any]]:
        db = _get_db()
        if db is not None and self._table_ready:
            try:
                rows = db.query(
                    "SELECT id, asset_id, name, status, progress, error,"
                    " version, created_at, updated_at FROM char_lora_tasks"
                    " ORDER BY created_at DESC")
                return [dict(r) for r in rows]
            except Exception:  # noqa: BLE001
                log.debug("char tasks 读库失败，走内存镜像",
                             exc_info=True)
        return sorted(self._mem_tasks.values(),
                      key=lambda t: t.get("created_at", 0), reverse=True)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        for t in self.list_tasks():
            if t["id"] == task_id:
                return t
        return None

    # ── 素材采集（comic_assets 角色资产） ────────────────────
    def collect_images(self, asset: dict[str, Any]) -> list[Path]:
        """角色资产 → 训练图列表（主图 + meta.views 四视图，仅存盘文件）。

        meta.views 兼容两种形态（批5 修正）：list[str]（路径列表）或
        dict[str, str]（{front/side/back/closeup: 路径}——comic_gen.py
        实际写入的形态）。
        """
        from ..config import DATA_DIR
        imgs: list[Path] = []
        main = str(asset.get("file_path") or "")
        if main:
            p = Path(main)
            if not p.is_absolute():
                p = DATA_DIR / p
            if p.is_file():
                imgs.append(p)
        meta = asset.get("meta") or {}
        views_raw: Any = meta.get("views") or []
        views: Any = []
        if isinstance(views_raw, dict):
            views = list(views_raw.values())
        elif isinstance(views_raw, list):
            views = views_raw
        else:
            views = []
            views = []
        for v in views:
            if isinstance(v, str):
                p = Path(v)
                if not p.is_absolute():
                    p = DATA_DIR / p
                if p.is_file():
                    imgs.append(p)
        # 去重保序
        seen: set[Path] = set()
        return [p for p in imgs
                if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

    # ── 训练入口 ────────────────────────────────────────────
    def base_ready(self) -> tuple[bool, str]:
        if not BASE_MODEL_DIR.is_dir():
            return False, (f"底座未就绪：{BASE_MODEL_DIR} 不存在"
                           "（flux2-klein-4b 需在盘）")
        if not (BASE_MODEL_DIR / "transformer").is_dir():
            return False, "底座缺 transformer 子目录（diffusers 布局不完整）"
        return True, ""

    def submit(self, asset: dict[str, Any]) -> dict[str, Any]:
        """入队一个角色的 LoRA 训练（素材不足/互斥占用即时拒绝）。"""
        from ..middleware.feature_lock import get_feature_lock

        if get_feature_lock().active_feature is not None:
            raise CharacterTrainingFailed(
                "TRAINING_MUTEX_LOCKED",
                "训练功能与其他重量级功能互斥，当前有功能占用中")
        ready, reason = self.base_ready()
        if not ready:
            raise CharacterTrainingFailed(reason)
        imgs = self.collect_images(asset)
        if len(imgs) < MIN_IMAGES:
            raise CharacterTrainingFailed(
                f"训练素材不足：{len(imgs)} < {MIN_IMAGES}"
                "（角色需先升级四视图，主图+四视图至少 4 张）")
        for t in self.list_tasks():
            if t["asset_id"] == asset.get("asset_id") and \
                    t["status"] in (STATUS_QUEUED, STATUS_TRAINING):
                raise CharacterTrainingFailed("该角色已有训练任务在队列中")
        task = {
            "id": f"char_{int(time.time() * 1000):x}_"
                  f"{threading.get_ident() & 0xfff:x}",
            "asset_id": str(asset.get("asset_id") or asset.get("id") or ""),
            "name": str(asset.get("name") or "未命名角色"),
            "status": STATUS_QUEUED, "progress": 0.0, "error": None,
            "version": None, "created_at": time.time(),
        }
        self._upsert_task(task)
        with self._queue_lock:
            self._queue.append({"task": task, "asset": dict(asset),
                                "images": [str(p) for p in imgs]})
        self._ensure_worker()
        return task

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise CharacterTrainingFailed("任务不存在")
        if task["status"] == STATUS_QUEUED:
            with self._queue_lock:
                self._queue = [q for q in self._queue
                               if q["task"]["id"] != task_id]
            task["status"] = STATUS_CANCELLED
            self._upsert_task(task)
        elif task["status"] == STATUS_TRAINING:
            self._cancel_flags.add(task_id)
        return task

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True,
                name="char-lora-training")
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._queue_lock:
                item = self._queue.pop(0) if self._queue else None
            if item is None:
                return
            task = item["task"]
            if task["id"] in self._cancel_flags:
                self._cancel_flags.discard(task["id"])
                task["status"] = STATUS_CANCELLED
                self._upsert_task(task)
                continue
            task["status"] = STATUS_TRAINING
            self._upsert_task(task)
            lock_held = False
            try:
                if not self._lock_guard.acquire(task["id"]):
                    raise CharacterTrainingFailed(
                        "训练功能锁获取失败（互斥占用）")
                lock_held = True
                _t = task
                from .power_guard import keep_awake
                with keep_awake(f"char-train:{task['id']}"):
                    result = self._train_core(
                        item["asset"], [Path(p) for p in item["images"]],
                        lambda pct, _t=_t, **kw: self._progress(_t, pct, **kw),
                        task["id"])
                task.update({"status": STATUS_DONE, "progress": 1.0,
                             "version": result.get("version"),
                             "error": None})
                self._upsert_task(task)
            except CharacterTrainingCancelled:
                task.update({"status": STATUS_CANCELLED, "error": None})
                self._upsert_task(task)
            except Exception as exc:  # noqa: BLE001 - 训练异常如实入账
                log.warning("人物 LoRA 训练失败: %s", exc, exc_info=True)
                task.update({"status": STATUS_ERROR,
                             "error": str(exc)[:500]})
                self._upsert_task(task)
            finally:
                if lock_held:
                    try:
                        self._lock_guard.release()
                    except Exception:  # noqa: BLE001 - 释放失败仅记日志
                        log.debug("训练锁释放异常", exc_info=True)

    def _progress(self, task: dict[str, Any], pct: float,
                  **kw: Any) -> None:
        task["progress"] = max(0.0, min(1.0, pct))
        self._upsert_task(task)

    # ── 训练核心（Flux2 QLoRA） ──────────────────────────────
    # 条件通路闸（fail-closed，2026-09-17 真火结论）：Flux2 前向需要
    # 管线级条件（latent 2×2 打包 + RoPE 位置 ID + Qwen3 真文本编码），
    # 裸调 transformer 仅能过形状（真火实测 mat1 2048x64 vs 128x3072），
    # 练出的是垃圾——拒绝盲训，防静默产出废 LoRA。接线完成后置 True。
    _CONDITIONING_READY = True  # 2026-09-18 批1 接线完成（真火四连验：Qwen3 编码/patchify+BN/RoPE/timestep0~1）

    def _train_core(self, asset: dict[str, Any], images: list[Path],
                    progress_cb: Callable[..., None],
                    task_id: str = "",
                    epochs: int = 8,
                    resolution: int = 512) -> dict[str, Any]:
        torch = _try_import("torch")
        diffusers = _try_import("diffusers")
        peft = _try_import("peft")
        if torch is None or diffusers is None or peft is None:
            raise CharacterTrainingFailed(
                "训练依赖未安装（torch/diffusers/peft）")
        if not self._CONDITIONING_READY:
            raise CharacterTrainingFailed(
                "训练核心条件通路未接线（Flux2 打包+RoPE+真文本编码），"
                "拒绝盲训防产出废 LoRA——队列/版本/部署/回滚链已就绪，"
                "接线后即可开练")

        # ── 条件通路（2026-09-18 批1 接线，契约源=Flux2KleinPipeline 源码解剖）──
        # ① 真文本编码：Qwen3-4B 编码器（借 pipe.encode_prompt 拿 embeds+text_ids），
        #    编码后立即释放 ~8GB 显存再装载 4bit transformer（峰值错峰）
        # ② 打包 = 纯置换 (B,C,H,W)→(B,HW,C)（_pack_latents 源码实证，无 2×2 patchify）
        # ③ RoPE：img_ids=_prepare_latent_ids(latents) (B,HW,4)；txt_ids 随编码返回
        # ④ timestep 传 0~1（管线内 timestep/1000 实证——旧循环 *1000 是错的）
        import gc

        from diffusers import AutoencoderKLFlux2, Flux2KleinPipeline
        from diffusers.pipelines.flux2.pipeline_flux2_klein import (
            retrieve_latents as _retrieve_latents,
        )
        from PIL import Image
        from transformers import Qwen2TokenizerFast, Qwen3ForCausalLM

        device = "cuda" if torch.cuda.is_available() else "cpu"
        name = str(asset.get("name") or "character")

        tokenizer = Qwen2TokenizerFast.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="tokenizer")
        text_encoder = Qwen3ForCausalLM.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="text_encoder",
            torch_dtype=torch.bfloat16)
        vae = AutoencoderKLFlux2.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="vae",
            torch_dtype=torch.bfloat16)
        pipe = Flux2KleinPipeline(
            tokenizer=tokenizer, text_encoder=text_encoder, vae=vae,
            transformer=None, scheduler=None)
        text_encoder.to(device)
        prompt_embeds, text_ids = self._encode_prompt(
            torch, pipe, f"a photo of {name}, portrait", device)
        # 释放文本编码器（训练循环不再需要；del pipe 引用链）
        # 释放注册件（DiffusionPipeline 动态属性表，mypy 无静态声明故 setattr）
        object.__setattr__(pipe, "text_encoder", None)
        object.__setattr__(pipe, "tokenizer", None)
        del text_encoder, tokenizer
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
        prompt_embeds = prompt_embeds.detach()
        text_ids = text_ids.detach()

        # ⑤ 4bit 量化 transformer + peft LoRA（加载时 device_map 就位，勿 .to）
        from transformers import BitsAndBytesConfig
        bnb = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16)
        transformer_cls = getattr(diffusers, "Flux2Transformer2DModel", None)
        if transformer_cls is None:
            raise CharacterTrainingFailed(
                "当前 diffusers 版本无 Flux2Transformer2DModel（需 0.36+）")
        transformer = transformer_cls.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="transformer",
            quantization_config=bnb, torch_dtype=torch.bfloat16,
            **({"device_map": {"": device}} if device == "cuda" else {}))
        transformer.requires_grad_(False)
        # Flux2 模块命名（一代 ff.net.0.proj 在此架构不存在）
        lora_cfg = peft.LoraConfig(
            r=16, lora_alpha=32, lora_dropout=0.05,
            target_modules=["to_q", "to_k", "to_v", "to_out.0",
                            "linear_in", "linear_out"])
        transformer = peft.get_peft_model(transformer, lora_cfg)
        transformer.train()
        if hasattr(transformer, "enable_gradient_checkpointing"):
            transformer.enable_gradient_checkpointing()
        vae.requires_grad_(False)
        vae.eval()
        vae.to(device)

        lr = 1e-4
        accum = 2
        optimizer = torch.optim.AdamW(
            (p for p in transformer.parameters() if p.requires_grad), lr=lr)
        steps_per_epoch = max(1, len(images))
        max_steps = epochs * ((steps_per_epoch + accum - 1) // accum)
        global_step = 0

        for epoch in range(epochs):
            for i, img_path in enumerate(images):
                if task_id and task_id in self._cancel_flags:
                    raise CharacterTrainingCancelled()
                img = Image.open(img_path).convert("RGB").resize(
                    (resolution, resolution))
                px = torch.tensor(list(img.getdata()), dtype=torch.bfloat16)
                px = px.view(resolution, resolution, 3).permute(2, 0, 1)
                px = (px / 127.5 - 1.0).unsqueeze(0).to(device)
                # latent 备制=镜像 _encode_vae_image（img2img 真源）：
                # encode(argmax) → patchify(32ch×2×2→128ch, H/2,W/2) → BN 归一化
                with torch.no_grad():
                    _lat = _retrieve_latents(
                        vae.encode(px), sample_mode="argmax")
                    _lat = Flux2KleinPipeline._patchify_latents(_lat)
                    _mean = vae.bn.running_mean.view(1, -1, 1, 1).to(
                        device=device, dtype=_lat.dtype)
                    _std = torch.sqrt(
                        vae.bn.running_var.view(1, -1, 1, 1)
                        + vae.config.batch_norm_eps).to(
                        device=device, dtype=_lat.dtype)
                    latent = (_lat - _mean) / _std
                # 打包=纯置换 (B,128,h,w)→(B,HW,128)；ids=32×32 网格 (B,HW,4)
                b, c, h, w = latent.shape
                packed = latent.reshape(b, c, h * w).permute(0, 2, 1)
                img_ids = Flux2KleinPipeline._prepare_latent_ids(latent).to(
                    device=device, dtype=packed.dtype)
                noise = torch.randn_like(packed)
                t = torch.rand(1, device=device, dtype=torch.bfloat16)
                noisy = (1 - t) * packed + t * noise
                target = noise - packed
                pred = transformer(
                    hidden_states=noisy,
                    timestep=t,            # 0~1（Flux2 契约）
                    guidance=None,
                    encoder_hidden_states=prompt_embeds,
                    txt_ids=text_ids.to(device=device),
                    img_ids=img_ids,
                    return_dict=False)[0]
                loss = torch.nn.functional.mse_loss(pred.float(),
                                                    target.float())
                loss = loss / accum
                loss.backward()
                if (i + 1) % accum == 0 or i == steps_per_epoch - 1:
                    torch.nn.utils.clip_grad_norm_(
                        (p for p in transformer.parameters()
                         if p.requires_grad), 1.0)
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                    global_step += 1
                    progress_cb(
                        min(0.95, global_step / max(max_steps, 1)),
                        epoch=epoch + 1, step=global_step,
                        max_steps=max_steps,
                        loss=float(loss.detach()) * accum)
            if device == "cuda":
                torch.cuda.empty_cache()

        # 保存版本 + 部署到资产旁（D-LoRA 消费约定）
        version_dir, version = self._next_version_dir(
            str(asset.get("asset_id") or asset.get("id") or "unknown"))
        transformer.save_pretrained(str(version_dir))
        meta = {"version": version, "created_at": time.time(),
                "name": name, "data_count": len(images),
                "base_model": BASE_MODEL_ID,
                "hyperparams": {"rank": 16, "alpha": 32, "lr": lr,
                                "epochs": epochs}}
        (version_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self._set_current(str(asset.get("asset_id") or asset.get("id")),
                          version)
        deployed = self._deploy_to_asset(asset, version_dir)
        self._prune_versions(str(asset.get("asset_id") or asset.get("id")))
        progress_cb(1.0)
        return {"version": version, "path": str(version_dir),
                "data_count": len(images), "deployed": deployed}

    def _encode_prompt(self, torch: Any, pipe: Any, prompt: str,
                       device: str) -> tuple[Any, Any]:
        """真文本编码（批1 接线 2026-09-18）：借 Flux2KleinPipeline.
        encode_prompt 走 Qwen3-4B 编码器，返回 (prompt_embeds, text_ids)。

        编码完成后由调用方释放 text_encoder（~8GB bf16）再装载 4bit
        transformer——峰值错峰，16GB 卡装得下。哈希嵌入降级版已随
        条件通路接线退役。
        """
        embeds, text_ids = pipe.encode_prompt(
            prompt=prompt, device=device, max_sequence_length=256)
        return embeds, text_ids

    # ── 版本管理 ────────────────────────────────────────────
    def _asset_root(self, asset_id: str) -> Path:
        return VERSIONS_ROOT / asset_id

    def _asset_store(self, asset_id: str) -> VersionStore:
        """批3 公共基座：每资产一个 VersionStore 实例。"""
        from .training_common import VersionStore
        return VersionStore(self._asset_root(asset_id), keep=KEEP_VERSIONS)

    def _next_version_dir(self, asset_id: str) -> tuple[Path, str]:
        return self._asset_store(asset_id).next_version_dir()

    def _set_current(self, asset_id: str, version: str) -> None:
        self._asset_store(asset_id).set_current(version)

    def _deploy_to_asset(self, asset: dict[str, Any],
                         version_dir: Path) -> str:
        """把 adapter 拷到资产旁 lora.safetensors（D-LoRA 消费约定）。"""
        from ..config import DATA_DIR
        src = version_dir / "adapter_model.safetensors"
        if not src.is_file():
            raise CharacterTrainingFailed(
                f"adapter 产物缺失: {src.name}")
        main = Path(str(asset.get("file_path") or ""))
        if not main.is_absolute():
            main = DATA_DIR / main
        dst = main.parent / "lora.safetensors"
        shutil.copyfile(src, dst)
        return str(dst)

    def versions(self, asset_id: str) -> list[dict[str, Any]]:
        root = self._asset_root(asset_id)
        out: list[dict[str, Any]] = []
        current = ""
        cj = root / "current.json"
        if cj.is_file():
            try:
                current = json.loads(
                    cj.read_text(encoding="utf-8")).get("version", "")
            except Exception:  # noqa: BLE001
                pass
        if root.is_dir():
            for d in sorted(root.iterdir(), reverse=True):
                if d.is_dir() and d.name.startswith("v"):
                    meta: dict[str, Any] = {"version": d.name}
                    mf = d / "meta.json"
                    if mf.is_file():
                        try:
                            meta.update(json.loads(
                                mf.read_text(encoding="utf-8")))
                        except Exception:  # noqa: BLE001
                            pass
                    meta["is_current"] = d.name == current
                    out.append(meta)
        return out

    def rollback(self, asset: dict[str, Any], version: str) -> dict[str, Any]:
        d = self._asset_root(str(asset.get("asset_id") or asset.get("id"))) \
            / version
        if not d.is_dir():
            raise CharacterTrainingFailed(f"版本不存在: {version}")
        deployed = self._deploy_to_asset(asset, d)
        self._set_current(str(asset.get("asset_id") or asset.get("id")),
                          version)
        return {"version": version, "deployed": deployed}

    def _prune_versions(self, asset_id: str) -> None:
        root = self._asset_root(asset_id)
        current = ""
        cj = root / "current.json"
        if cj.is_file():
            try:
                current = json.loads(
                    cj.read_text(encoding="utf-8")).get("version", "")
            except Exception:  # noqa: BLE001
                pass
        dirs = sorted([d for d in root.iterdir()
                       if d.is_dir() and d.name.startswith("v")],
                      reverse=True) if root.is_dir() else []
        for d in dirs[KEEP_VERSIONS:]:
            if d.name != current:
                shutil.rmtree(d, ignore_errors=True)


_svc_lock = threading.Lock()


def get_character_lora_service() -> CharacterLoraService:
    return CharacterLoraService.get()
