# -*- coding: utf-8 -*-
"""OmniSpace AI — 后端 + 内核统一扁平包 (src/)

════════════════════════════════════════════════════════════════
 目录怎么读 (一图流)
════════════════════════════════════════════════════════════════

src/ 把原来的 backend/ 与 DistributedFormer 内核统一收拢到一个
扁平结构里, 分四层:

    ┌────────────────────────────────────────────────────────┐
    │ HTTP 层  api/            FastAPI 路由 (dialog/comic/   │
    │                         manga/novel/learn/models ...)  │
    ├────────────────────────────────────────────────────────┤
    │ 业务层  services/        业务逻辑 + 推理引擎 + 模型管理 │
    │         middleware/      横切层 (错误/限流/互斥/CORS)   │
    ├────────────────────────────────────────────────────────┤
    │ 资源层  engines/         GPU / VRAM / 内存管理          │
    │         data/            存储 (DB/向量/全文/图谱/加密)  │
    │         + Rust 训练语料 (rust_coding / real_dataset)    │
    ├────────────────────────────────────────────────────────┤
    │ 内核层  core/            DistributedFormer 脉冲主模型   │
    │                         (CubeGPT, KV-stack 记忆)        │
    │         cutemamen/       CuteMamen 插件内核             │
    │                         (路由/生命周期/三级记忆/包格式) │
    │         codec/           脉冲/多模态编解码              │
    │         training/        主模型 → 插件 知识迁移读出层   │
    └────────────────────────────────────────────────────────┘

    入口: src/main.py (uvicorn src.main:app)
    配置: src/config.py + src/config.yaml

内核层 (DistributedFormer 移植, "新阶段 · 分布式架构"):

    主模型 (core/distributedformer.py, 冻结 CubeGPT 水库)
        │ training/readout.py 线性读出层训练
        ▼
    思考插件 (cutemamen/rust_coding.py 等, .CuteMamen 包交付,
              运行时由 cutemamen/kernel.py 路由激活)
        │ cutemamen/bridge.py
        ▼
    Transformer 宿主 (LoRA 适配器导出, 与外部大模型互联)

插件包 (自包含 tar.gz: manifest + weights + 三级记忆) 放 ./plugin/,
RustCoding.CuteMamen 携带主模型迁移的 Rust 开发知识 (502 段真实
语料, 5 类编译错误分类), VideoMaking.CuteMamen 是零 GPU 轻量视频
生成内核。插件系统规范见 cutemamen/pkg.py 模块注释。

各子包的 __init__.py 均有本包的架构说明, 逐层阅读即可。
"""

__version__ = "2.1.0"
