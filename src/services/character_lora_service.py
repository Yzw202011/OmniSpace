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

logger = logging.getLogger("omnispace.services.character_lora")

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
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._table_ready = self._ensure_table()

    # ── 任务表 ──────────────────────────────────────────────
    def _ensure_table(self) -> bool:
        db = _get_db()
        if db is None:
            return False
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS char_lora_tasks (
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
            logger.warning("char_lora_tasks 建表失败，降级内存镜像",
                           exc_info=True)
            return False

    def _upsert_task(self, task: dict[str, Any]) -> None:
        task["updated_at"] = time.time()
        self._mem_tasks[task["id"]] = dict(task)
        db = _get_db()
        if db is not None and self._table_ready:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO char_lora_tasks"
                    " (id, asset_id, name, status, progress, error, version,"
                    "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (task["id"], task["asset_id"], task["name"],
                     task["status"], task["progress"], task["error"],
                     task["version"], task["created_at"], task["updated_at"]))
            except Exception:  # noqa: BLE001 - 写失败保内存镜像
                logger.debug("char task 写库失败", exc_info=True)

    def list_tasks(self) -> list[dict[str, Any]]:
        db = _get_db()
        if db is not None and self._table_ready:
            try:
                rows = db.query(
                    "SELECT id, asset_id, name, status, progress, error,"
                    " version, created_at, updated_at FROM char_lora_tasks"
                    " ORDER BY created_at DESC") if hasattr(db, "query") \
                    else db.execute(
                    "SELECT id, asset_id, name, status, progress, error,"
                    " version, created_at, updated_at FROM char_lora_tasks"
                    " ORDER BY created_at DESC").fetchall()
                return [dict(r) for r in rows]
            except Exception:  # noqa: BLE001
                logger.debug("char tasks 读库失败，走内存镜像",
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
        """角色资产 → 训练图列表（主图 + meta.views 四视图，仅存盘文件）。"""
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
        for v in (meta.get("views") or []):
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
        from ..middleware.feature_lock import get_feature_lock
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
                lock = get_feature_lock()
                # worker 线程为同步上下文：走 acquire_sync（与风格服务
                # 的同步降级同一条状态，勿调 async acquire——协程对象
                # 恒真会造成假持锁）
                if not lock.acquire_sync("training", task_id=task["id"]):
                    raise CharacterTrainingFailed(
                        "训练功能锁获取失败（互斥占用）")
                lock_held = True
                _t = task
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
                logger.warning("人物 LoRA 训练失败: %s", exc, exc_info=True)
                task.update({"status": STATUS_ERROR,
                             "error": str(exc)[:500]})
                self._upsert_task(task)
            finally:
                if lock_held:
                    try:
                        get_feature_lock().release_sync("training")
                    except Exception:  # noqa: BLE001 - 释放失败仅记日志
                        logger.debug("训练锁释放异常", exc_info=True)

    def _progress(self, task: dict[str, Any], pct: float,
                  **kw: Any) -> None:
        task["progress"] = max(0.0, min(1.0, pct))
        self._upsert_task(task)

    # ── 训练核心（Flux2 QLoRA） ──────────────────────────────
    # 条件通路闸（fail-closed，2026-09-17 真火结论）：Flux2 前向需要
    # 管线级条件（latent 2×2 打包 + RoPE 位置 ID + Qwen3 真文本编码），
    # 裸调 transformer 仅能过形状（真火实测 mat1 2048x64 vs 128x3072），
    # 练出的是垃圾——拒绝盲训，防静默产出废 LoRA。接线完成后置 True。
    _CONDITIONING_READY = False

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
        from PIL import Image

        name = str(asset.get("name") or "character")
        lr = 1e-4
        accum = 2

        from transformers import BitsAndBytesConfig
        bnb = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16)
        # Flux2（二代）架构：model_index.json 声明 Flux2Transformer2DModel
        # + AutoencoderKLFlux2（一代 FluxTransformer2DModel 权重名不匹配
        # =静默随机初始化，真火冒烟实锤）
        transformer_cls = getattr(diffusers, "Flux2Transformer2DModel", None)
        if transformer_cls is None:
            raise CharacterTrainingFailed(
                "当前 diffusers 版本无 Flux2Transformer2DModel（需 0.36+）")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # bnb 4bit 参数不可 .to 迁移（meta tensor）：加载时直接就位目标卡
        transformer = transformer_cls.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="transformer",
            quantization_config=bnb, torch_dtype=torch.bfloat16,
            **({"device_map": {"": device}} if device == "cuda" else {}))
        transformer.requires_grad_(False)
        # Flux2 模块命名：attn.to_q/k/v/out + ff.linear_in/out（一代的
        # ff.net.0.proj 在此架构不存在）
        lora_cfg = peft.LoraConfig(
            r=16, lora_alpha=32, lora_dropout=0.05,
            target_modules=["to_q", "to_k", "to_v", "to_out.0",
                            "linear_in", "linear_out"])
        transformer = peft.get_peft_model(transformer, lora_cfg)
        transformer.train()
        if hasattr(transformer, "enable_gradient_checkpointing"):
            transformer.enable_gradient_checkpointing()

        vae_cls = getattr(diffusers, "AutoencoderKLFlux2", None)
        vae = vae_cls.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="vae",
            torch_dtype=torch.bfloat16) if vae_cls else None
        if vae is not None:
            vae.requires_grad_(False)
            vae.eval()

        if vae is not None:
            vae.to(device)  # 量化 transformer 已就位，勿再 .to（meta 崩）

        optimizer = torch.optim.AdamW(
            (p for p in transformer.parameters() if p.requires_grad), lr=lr)
        steps_per_epoch = max(1, len(images))
        max_steps = epochs * ((steps_per_epoch + accum - 1) // accum)
        global_step = 0
        prompt_embeds = self._encode_prompt(torch, name, device)

        for epoch in range(epochs):
            for i, img_path in enumerate(images):
                if task_id and task_id in self._cancel_flags:
                    raise CharacterTrainingCancelled()
                img = Image.open(img_path).convert("RGB").resize(
                    (resolution, resolution))
                px = torch.tensor(list(img.getdata()), dtype=torch.bfloat16)
                px = px.view(resolution, resolution, 3).permute(2, 0, 1)
                px = (px / 127.5 - 1.0).unsqueeze(0).to(device)
                with torch.no_grad():
                    if vae is not None:
                        latent = vae.encode(px).latent_dist.sample()
                    else:
                        latent = px
                noise = torch.randn_like(latent)
                t = torch.rand(1, device=device, dtype=torch.bfloat16)
                tv = t.view(-1, 1, 1, 1)
                noisy = (1 - tv) * latent + tv * noise
                target = noise - latent
                pred = transformer(hidden_states=noisy,
                                   encoder_hidden_states=prompt_embeds,
                                   timestep=t * 1000,
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

    def _encode_prompt(self, torch: Any, prompt: str, device: str) -> Any:
        """文本条件（诚实降级）：角色名确定性哈希嵌入（与风格服务同口径）。

        klein 的 Qwen3-4B 编码器进显存代价高；同一角色名 → 同一嵌入，
        身份特征训练信号一致。接真编码器为后续增强点。
        """
        import hashlib

        # 联合注意力维从底座 config 动态读（klein-4b=7680；硬编码必错型号）
        import json as _json
        dim = 7680
        cfg = BASE_MODEL_DIR / "transformer" / "config.json"
        if cfg.is_file():
            try:
                dim = int(_json.loads(
                    cfg.read_text(encoding="utf-8")).get(
                    "joint_attention_dim", dim))
            except Exception:  # noqa: BLE001 - 读失败用保守默认
                pass
        seed = int.from_bytes(
            hashlib.md5(prompt.encode("utf-8")).digest()[:8], "little")
        gen = torch.Generator(device="cpu").manual_seed(seed)
        emb = torch.randn(1, 128, dim, generator=gen)
        return emb.to(device=device, dtype=torch.bfloat16)

    # ── 版本管理 ────────────────────────────────────────────
    def _asset_root(self, asset_id: str) -> Path:
        return VERSIONS_ROOT / asset_id

    def _next_version_dir(self, asset_id: str) -> tuple[Path, str]:
        root = self._asset_root(asset_id)
        root.mkdir(parents=True, exist_ok=True)
        n = 1
        while (root / f"v{n}").exists():
            n += 1
        d = root / f"v{n}"
        d.mkdir(parents=True)
        return d, f"v{n}"

    def _set_current(self, asset_id: str, version: str) -> None:
        root = self._asset_root(asset_id)
        (root / "current.json").write_text(
            json.dumps({"version": version, "updated_at": time.time()},
                       ensure_ascii=False), encoding="utf-8")

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
