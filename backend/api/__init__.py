"""OmniSpace AI v2.1 API 路由包（规格 §4 API接口完整定义）。

本包按业务域拆分为多个路由模块，每个模块导出 ``router``（APIRouter）。
main.py 统一以 ``config.API_PREFIX``（/v1）前缀挂载，故各模块 router 不自带 prefix。

子模块：
    dialog    —— 对话 API（§4.2）
    draw      —— 绘画 API（§4.3）
    manga     —— 漫剧 API（§4.4）
    learn     —— 知识学习 API
    models    —— 模型管理 API（§4.5）
    hardware  —— 硬件 API（§4.6）
    system    —— 系统 API（§4.7）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
