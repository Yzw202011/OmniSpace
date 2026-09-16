"""training — 训练与验证协议 (知识的源头)

    readout.py            训练方法学验证 (reservoir computing 范式):
                          冻结 CubeGPT 水库特征 + 线性 softmax 读出层;
                          CubeFeatureExtractor — v0.8.6 主模型知识迁移
                          的特征源头 (同 seed 逐位可复现)
    trainer.py / train.py 监督/STDP 训练器与入口

v0.8.6 起, RustCodingPlugin.migrate_from_main_model() 从这里取
CubeFeatureExtractor / LinearReadout, 把主模型知识迁移成插件权重。
"""
