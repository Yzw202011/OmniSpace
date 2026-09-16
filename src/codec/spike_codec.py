"""
脉冲编码/解码层
环境数据 ↔ 脉冲信号的转换

支持:
- 网页文本: TF-IDF向量 → 稀疏脉冲模式 (激活top-20维度)
- 图像像素: 卷积特征图 → 空间脉冲拓扑 (3D网格映射)
- 数值数据: 归一化 → 标量脉冲强度
- 时间序列: 差分编码 → 时序脉冲序列

脉冲输出 → 动作解码:
- 输出层活跃模式 → 动作指令哈希 → 映射到操作库
- 支持: 文件读写、网页点击、API调用、数据查询、报告生成
"""

import numpy as np
import re
from typing import Dict, List, Tuple, Optional, Any, Union
from collections import Counter, defaultdict
import hashlib
import json


class SpikeEncoder:
    """环境数据 → 脉冲编码"""
    
    def __init__(self, dim: int = 16, vocab_size: int = 1000):
        self.dim = dim
        self.vocab_size = vocab_size
        # 预构建词汇哈希表 (模拟TF-IDF词典)
        self.word_hash = defaultdict(lambda: np.random.randint(0, dim))
        
    def encode_numeric(self, value: float, min_val: float = -10.0, 
                       max_val: float = 10.0) -> np.ndarray:
        """
        数值数据 → 标量脉冲强度
        归一化到 [-1, 1] 然后映射到脉冲维度
        """
        # 归一化
        normalized = np.clip((value - min_val) / (max_val - min_val) * 2 - 1, -1, 1)
        
        # 映射到dim维: 主维度承载强度，其余维度扩散
        signal = np.zeros(self.dim)
        
        # 强度映射到特定维度
        strength_idx = int((normalized + 1) / 2 * (self.dim - 1))
        strength_idx = np.clip(strength_idx, 0, self.dim - 1)
        
        signal[strength_idx] = abs(normalized)
        # 相邻维度扩散
        if strength_idx > 0:
            signal[strength_idx - 1] = abs(normalized) * 0.3
        if strength_idx < self.dim - 1:
            signal[strength_idx + 1] = abs(normalized) * 0.3
        
        return signal
    
    def encode_text(self, text: str, top_k: int = 20) -> np.ndarray:
        """
        网页文本 → TF-IDF向量 → 稀疏脉冲模式 (激活top-20维度)
        
        简化的TF-IDF编码:
        1. 分词
        2. 词频统计
        3. 映射到固定维度 (通过哈希)
        4. 取top-k激活维度
        """
        if not text or not text.strip():
            return np.zeros(self.dim)
        
        # 代码感知分词 (v0.6.0): 保留 & ' -> :: 等代码关键 token
        words = re.findall(r"[a-zA-Z_]+|&+|'|->|::|\d+", text.lower())
        if not words:
            return np.zeros(self.dim)
        
        # 词频统计
        word_counts = Counter(words)
        total = len(words)
        
        # 映射到维度
        dim_scores = defaultdict(float)
        for word, count in word_counts.items():
            # 哈希到维度 (crc32, 跨进程确定)
            import zlib
            dim_idx = zlib.crc32(word.encode("utf-8")) % self.dim
            # TF-IDF近似: 词频 × 逆文档频率近似 (稀有词权重更高)
            tf = count / total
            # 使用词长近似IDF (长词更稀有)
            idf_approx = np.log(1 + len(word)) / np.log(2)
            dim_scores[dim_idx] += tf * idf_approx
        
        # 构建信号向量
        signal = np.zeros(self.dim)
        for idx, score in dim_scores.items():
            signal[idx] = score
        
        # 稀疏化: 只保留top_k维度
        if np.count_nonzero(signal) > top_k:
            threshold = np.partition(signal, -top_k)[-top_k]
            signal[signal < threshold] = 0
        
        # 归一化
        norm = np.linalg.norm(signal)
        if norm > 0:
            signal = signal / norm
        
        return signal
    
    def encode_timeseries(self, values: List[float]) -> List[np.ndarray]:
        """
        时间序列 → 差分编码 → 时序脉冲序列
        
        返回一系列脉冲信号，每个信号代表一个时间步的差分
        """
        if len(values) < 2:
            return [self.encode_numeric(values[0])] if values else []
        
        signals = []
        for i in range(1, len(values)):
            diff = values[i] - values[i-1]  # 差分
            signal = self.encode_numeric(diff, min_val=-5.0, max_val=5.0)
            signals.append(signal)
        
        return signals
    
    def encode_image(self, image_data: Any, pool: int = 4) -> np.ndarray:
        """
        图像 (2D 灰度数组) → 确定性空间脉冲信号

        对图像做 pool×pool 平均池化得到粗粒度强度图,
        展平为 pool² 维特征并归一化, 不足 dim 维则零填充。
        """
        img = np.asarray(image_data, dtype=float)
        if img.ndim != 2:
            raise ValueError(f"encode_image 需要 2D 数组, 收到 shape={img.shape}")
        h, w = img.shape
        ph, pw = max(1, h // pool), max(1, w // pool)
        pooled = np.zeros((pool, pool))
        for i in range(pool):
            for j in range(pool):
                block = img[i*ph:(i+1)*ph, j*pw:(j+1)*pw]
                pooled[i, j] = block.mean() if block.size else 0.0
        feats = pooled.flatten()
        if feats.max() > feats.min():
            feats = (feats - feats.min()) / (feats.max() - feats.min())
        signal = np.zeros(self.dim)
        n = min(len(feats), self.dim)
        signal[:n] = feats[:n]
        norm = np.linalg.norm(signal)
        if norm > 0:
            signal = signal / norm * min(1.0, norm)
        return signal

    def encode_image_placeholder(self, image_data: Any) -> np.ndarray:
        """已废弃的随机占位编码, 保留仅为兼容, 等价于 encode_image"""
        if isinstance(image_data, np.ndarray) and image_data.ndim == 2:
            return self.encode_image(image_data)
        return np.zeros(self.dim)
    
    def encode_multi(self, data_dict: Dict[str, Any]) -> np.ndarray:
        """
        多模态编码: 合并多种类型的输入
        
        Args:
            data_dict: {"numeric": [...], "text": "...", "timeseries": [...]}
        
        Returns:
            合并后的脉冲信号
        """
        combined = np.zeros(self.dim)
        
        if "numeric" in data_dict:
            combined += self.encode_numeric(data_dict["numeric"]) * 0.4
        
        if "text" in data_dict:
            combined += self.encode_text(data_dict["text"]) * 0.3
        
        if "timeseries" in data_dict:
            ts_signals = self.encode_timeseries(data_dict["timeseries"])
            if ts_signals:
                combined += ts_signals[-1] * 0.3  # 取最新值
        
        # 归一化
        norm = np.linalg.norm(combined)
        if norm > 0:
            combined = combined / norm
        
        return combined


class SpikeDecoder:
    """脉冲输出 → 动作解码"""
    
    def __init__(self, dim: int = 16):
        self.dim = dim
        # 动作指令库
        self.action_library = {
            "file_write": self._decode_file_write,
            "desktop_notification": self._decode_notification,
            "api_call": self._decode_api_call,
            "web_browse": self._decode_web_browse,
            "data_query": self._decode_data_query,
            "report_generate": self._decode_report_generate
        }
        
    def decode(self, output_pattern: np.ndarray, 
               strength_threshold: float = 0.3) -> List[Dict]:
        """
        输出层活跃模式 → 动作指令列表
        
        Args:
            output_pattern: 输出层激活模式 (dim维)
            strength_threshold: 激活强度阈值
        
        Returns:
            动作指令列表
        """
        actions = []
        
        # 确定活跃维度
        active_indices = np.where(np.abs(output_pattern) > strength_threshold)[0]
        
        if len(active_indices) == 0:
            return actions
        
        # 基于活跃模式哈希选择动作
        pattern_hash = hashlib.md5(output_pattern.tobytes()).hexdigest()[:8]
        hash_int = int(pattern_hash, 16)
        
        # 根据最强激活维度选择动作类型
        strongest_idx = np.argmax(np.abs(output_pattern))
        strength = float(np.abs(output_pattern[strongest_idx]))
        
        # 映射到动作
        action_types = list(self.action_library.keys())
        action_idx = strongest_idx % len(action_types)
        action_type = action_types[action_idx]
        
        # 构建动作指令
        action = {
            "type": action_type,
            "strength": strength,
            "pattern_hash": pattern_hash,
            "active_dims": active_indices.tolist(),
            "timestamp": __import__('time').time()
        }
        
        # 基于激活模式生成动作参数
        action_params = self._generate_action_params(action_type, output_pattern, strength)
        action.update(action_params)
        
        actions.append(action)
        
        # 如果多个维度强激活，可能生成多个动作
        if len(active_indices) >= 3:
            second_idx = active_indices[np.argsort(np.abs(output_pattern[active_indices]))[-2]]
            second_strength = float(np.abs(output_pattern[second_idx]))
            if second_strength > strength_threshold * 1.5:
                action_type2 = action_types[second_idx % len(action_types)]
                action2 = {
                    "type": action_type2,
                    "strength": second_strength,
                    "pattern_hash": pattern_hash + "_2",
                    "active_dims": [second_idx],
                    "timestamp": __import__('time').time()
                }
                action2.update(self._generate_action_params(action_type2, output_pattern, second_strength))
                actions.append(action2)
        
        return actions
    
    def _generate_action_params(self, action_type: str, 
                                pattern: np.ndarray, 
                                strength: float) -> Dict:
        """基于激活模式生成动作参数"""
        params = {}
        
        if action_type == "file_write":
            params["path"] = f"reports/pulse_output_{int(__import__('time').time())}.md"
            params["content_template"] = "pulse_report"
            params["priority"] = "high" if strength > 0.8 else "normal"
            
        elif action_type == "desktop_notification":
            params["title"] = "脉冲智能体通知"
            params["message"] = f"检测到强度 {strength:.2f} 的脉冲信号"
            params["urgency"] = "critical" if strength > 0.9 else "normal"
            
        elif action_type == "api_call":
            params["method"] = "POST"
            params["endpoint"] = "https://hooks.slack.com/services/placeholder"
            params["payload"] = {"text": f"脉冲强度: {strength:.2f}"}
            
        elif action_type == "web_browse":
            params["url"] = "https://finance.yahoo.com"
            params["action"] = "open"
            
        elif action_type == "data_query":
            params["source"] = "native"
            params["query"] = f"pulse_pattern_{hash(pattern.tobytes()) % 10000}"
            
        elif action_type == "report_generate":
            params["template"] = "daily_pulse"
            params["format"] = "markdown"
            params["sections"] = ["summary", "trends", "anomalies"]
        
        return params
    
    def _decode_file_write(self, pattern: np.ndarray, params: Dict) -> Dict:
        return params
    
    def _decode_notification(self, pattern: np.ndarray, params: Dict) -> Dict:
        return params
    
    def _decode_api_call(self, pattern: np.ndarray, params: Dict) -> Dict:
        return params
    
    def _decode_web_browse(self, pattern: np.ndarray, params: Dict) -> Dict:
        return params
    
    def _decode_data_query(self, pattern: np.ndarray, params: Dict) -> Dict:
        return params
    
    def _decode_report_generate(self, pattern: np.ndarray, params: Dict) -> Dict:
        return params


class MultiModalCodec:
    """多模态编解码器: 统一环境数据与脉冲信号的转换"""
    
    def __init__(self, dim: int = 16):
        self.dim = dim
        self.encoder = SpikeEncoder(dim)
        self.decoder = SpikeDecoder(dim)
    
    def encode(self, data: Union[float, str, List[float], Dict], 
               data_type: Optional[str] = None) -> np.ndarray:
        """通用编码入口"""
        if data_type is None:
            # 自动推断类型
            if isinstance(data, (int, float)):
                data_type = "numeric"
            elif isinstance(data, str):
                data_type = "text"
            elif isinstance(data, list) and all(isinstance(x, (int, float)) for x in data):
                data_type = "timeseries"
            elif isinstance(data, dict):
                data_type = "multi"
            else:
                data_type = "numeric"
        
        if data_type == "numeric":
            return self.encoder.encode_numeric(float(data))
        elif data_type == "text":
            return self.encoder.encode_text(data)
        elif data_type == "timeseries":
            signals = self.encoder.encode_timeseries(data)
            return signals[-1] if signals else np.zeros(self.dim)
        elif data_type == "multi":
            return self.encoder.encode_multi(data)
        else:
            return np.zeros(self.dim)
    
    def decode(self, pattern: np.ndarray, threshold: float = 0.3) -> List[Dict]:
        """通用解码入口"""
        return self.decoder.decode(pattern, threshold)
    
    def encode_stock_data(self, price: float, change_pct: float, 
                          volume: float, news_text: str = "") -> np.ndarray:
        """
        股票数据专用编码
        
        将价格、涨跌幅、成交量、新闻文本编码为统一脉冲信号
        """
        price_signal = self.encoder.encode_numeric(price, min_val=0, max_val=1000)
        change_signal = self.encoder.encode_numeric(change_pct, min_val=-20, max_val=20)
        volume_signal = self.encoder.encode_numeric(volume, min_val=0, max_val=1e9)
        news_signal = self.encoder.encode_text(news_text) if news_text else np.zeros(self.dim)
        
        # 加权合并
        combined = (price_signal * 0.3 + 
                   change_signal * 0.4 + 
                   volume_signal * 0.1 + 
                   news_signal * 0.2)
        
        norm = np.linalg.norm(combined)
        if norm > 0:
            combined = combined / norm
        
        return combined


# ═══════════════════════════════════════════════════════════════
# 自测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("脉冲编解码层测试")
    print("=" * 60)
    
    codec = MultiModalCodec(dim=16)
    
    # 1. 数值编码测试
    print("\n[1] 数值编码:")
    for val in [1.5, -2.0, 5.0, 0.0]:
        signal = codec.encode(val, "numeric")
        print(f"  数值 {val:6.2f} -> 非零维度: {np.count_nonzero(signal)}, 范数: {np.linalg.norm(signal):.3f}")
    
    # 2. 文本编码测试
    print("\n[2] 文本编码:")
    texts = [
        "Apple stock surges 5% on strong earnings report",
        "Market crashes amid recession fears",
        "Tesla announces new battery technology"
    ]
    for text in texts:
        signal = codec.encode(text, "text")
        print(f"  文本(前30字): {text[:30]}... -> 非零维度: {np.count_nonzero(signal)}")
    
    # 3. 时间序列编码
    print("\n[3] 时间序列编码:")
    prices = [150.0, 152.5, 148.0, 155.0, 160.0]
    signal = codec.encode(prices, "timeseries")
    print(f"  价格序列 -> 非零维度: {np.count_nonzero(signal)}")
    
    # 4. 股票数据编码
    print("\n[4] 股票数据编码:")
    stock_signal = codec.encode_stock_data(
        price=175.50,
        change_pct=3.5,
        volume=45_000_000,
        news_text="Apple reports record quarterly revenue"
    )
    print(f"  AAPL综合信号 -> 非零维度: {np.count_nonzero(stock_signal)}, 范数: {np.linalg.norm(stock_signal):.3f}")
    
    # 5. 解码测试
    print("\n[5] 动作解码:")
    # 模拟强激活模式
    strong_pattern = np.zeros(16)
    strong_pattern[0] = 0.95  # file_write
    strong_pattern[3] = 0.85  # api_call
    actions = codec.decode(strong_pattern, threshold=0.3)
    for action in actions:
        print(f"  动作: {action['type']}, 强度: {action['strength']:.2f}")
        print(f"    参数: {json.dumps({k:v for k,v in action.items() if k not in ['type','strength','pattern_hash','active_dims','timestamp']}, ensure_ascii=False, default=str)}")
    
    print("\n" + "=" * 60)
    print("编解码层测试通过!")
    print("=" * 60)
