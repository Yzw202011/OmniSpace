"""codec — 编解码层 (真实世界 ↔ 脉冲信号的翻译官)

    spike_codec.py        SpikeEncoder: 数值/文本/时序 → 16 维脉冲信号
                          (文本走代码感知 TF-IDF, crc32 确定性哈希);
                          MultiModalCodec + 脉冲 → 动作解码
    multimodal_codec.py   多模态编解码器 (CubeGPT 4 模态面配套)

只做"信号翻译", 不含任何学习或路由逻辑。
"""
