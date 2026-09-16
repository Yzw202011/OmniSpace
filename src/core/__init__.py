"""core — 神经底座 (脉冲计算的最底层)

本目录是整个框架的"硬件层", 只描述**如何计算**, 不关心智能体/插件语义:

    distributedformer.py   全部核心类的单文件实现:
                           · SpikingUnit   — 16 参数脉冲神经元
                             (w_in/b_in/w_state/w_out/... 详见类文档)
                           · 分形皮层层    — 小世界连接 + 向量化并行 step
                           · KVStack       — 固定容量堆记忆, 注意力检索
                           · DistributedFormer — 经典形态 (思考层 + 输出头)
                           · CubeGPT       — 立方体连接的多模态主模型
                             (4 模态面环形互注入, 必要思考的最小内核)
    face_pkg.py            模态面独立存档 (.dfpkg): 权重逐位可复现的
                           导出/导入, v0.7.0 起作为 .CuteMamen 首个特例

依赖方向: core 不依赖本包其他子目录 (codec/data/... 均可依赖 core)。
"""
