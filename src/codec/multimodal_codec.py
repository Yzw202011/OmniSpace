"""
多模态脉冲编解码器
将图像/视频/文本/数值编码为脉冲信号

16单元通道分配:
- 单元 0-3: 视觉通道 (图像/视频)
- 单元 4-7: 文本通道
- 单元 8-11: 数值通道
- 单元 12-15: 时序通道
"""

import numpy as np
import time
from typing import Dict, List, Tuple, Optional, Any
from collections import deque

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.codec.spike_codec import SpikeEncoder, MultiModalCodec


class MultimodalSpikeEncoder:
    """
    多模态脉冲编码器
    
    16个单元通道分配:
    - visual_dims (0-3):   视觉通道 - 图像/视频特征
    - text_dims (4-7):     文本通道 - TF-IDF稀疏编码
    - numeric_dims (8-11): 数值通道 - 标量强度
    - temporal_dims (12-15): 时序通道 - 差分/运动
    """
    
    def __init__(self, dim: int = 16):
        self.dim = dim
        # 16个单元分配：每模态4个单元
        self.visual_dims = list(range(0, 4))       # 单元 0-3 视觉
        self.text_dims = list(range(4, 8))         # 单元 4-7 文本
        self.numeric_dims = list(range(8, 12))     # 单元 8-11 数值
        self.temporal_dims = list(range(12, 16))   # 单元 12-15 时序
        
        # 基础编码器
        self.base_encoder = SpikeEncoder(dim=dim)
        
        # 历史缓存 (用于时序和运动检测)
        self.last_image = None
        self.last_frame_time = 0.0
    
    # ═══════════════════════════════════════════════════════════════
    # 视觉编码 (图像/视频)
    # ═══════════════════════════════════════════════════════════════
    
    def encode_image(self, image_array: np.ndarray) -> np.ndarray:
        """
        图像 → 空间脉冲拓扑
        
        流程:
        1. 将图像 resize/池化到 4x4
        2. 展平为 16 维，取前4维映射到 visual_dims
        3. 归一化
        """
        signal = np.zeros(self.dim)
        
        if image_array is None or image_array.size == 0:
            return signal
        
        # 确保 2D 图像
        if image_array.ndim == 1:
            image_array = image_array.reshape(int(np.sqrt(len(image_array))), -1)
        
        # 平均池化到 4x4
        h, w = image_array.shape[:2]
        
        # 计算 4x4 块的平均值
        block_h = max(1, h // 4)
        block_w = max(1, w // 4)
        
        pooled = []
        for i in range(4):
            for j in range(4):
                y0, y1 = i * block_h, min((i + 1) * block_h, h)
                x0, x1 = j * block_w, min((j + 1) * block_w, w)
                block = image_array[y0:y1, x0:x1]
                pooled.append(np.mean(block))
        
        pooled = np.array(pooled[:4])  # 取前4个值
        
        # 归一化
        p_norm = np.linalg.norm(pooled)
        if p_norm > 0:
            pooled = pooled / p_norm
        
        # 映射到 visual_dims
        for i, dim_idx in enumerate(self.visual_dims):
            if i < len(pooled):
                signal[dim_idx] = pooled[i]
        
        # 保存当前图像用于运动检测
        self.last_image = image_array.copy()
        
        return signal
    
    def encode_video_frame(self, frame: np.ndarray, 
                           prev_frame: Optional[np.ndarray] = None) -> np.ndarray:
        """
        视频帧 → 时空脉冲
        
        编码当前帧 + 帧间运动 (光流近似)
        """
        signal = self.encode_image(frame)
        self.last_image = frame.copy()
        
 # 运动检测: 与上一帧的差分
        if prev_frame is not None and self.last_image is not None:
            try:
                # 统一尺寸
                curr = self._resize_to(frame, (8, 8))
                prev = self._resize_to(prev_frame, (8, 8))
                
                # 计算帧间差分 (运动强度)
                diff = np.abs(curr.astype(float) - prev.astype(float))
                motion = np.mean(diff)
                
                # 运动编码到时序通道
                motion_signal = self.base_encoder.encode_numeric(motion, 0, 255)
                for i, dim_idx in enumerate(self.temporal_dims):
                    signal[dim_idx] = motion_signal[i % len(motion_signal)] * 0.5
                
            except Exception:
                pass
        
        return signal
    
    def _resize_to(self, arr: np.ndarray, target: Tuple[int, int]) -> np.ndarray:
        """简单 resize (最近邻)"""
        if arr.ndim > 2:
            arr = np.mean(arr, axis=2)
        h, w = arr.shape
        th, tw = target
        
        # 简单下采样
        result = np.zeros(target)
        for i in range(th):
            for j in range(tw):
                y = int(i * h / th)
                x = int(j * w / tw)
                result[i, j] = arr[min(y, h-1), min(x, w-1)]
        return result
    
    # ═══════════════════════════════════════════════════════════════
    # 多模态融合编码
    # ═══════════════════════════════════════════════════════════════
    
    def encode_multimodal(self, image=None, text=None, 
                          numeric=None, timeseries=None) -> np.ndarray:
        """
        多模态融合编码
        
        将各模态信号分别编码到对应通道，然后合并
        
        Returns:
            16 维脉冲信号
        """
        signal = np.zeros(self.dim)
        
        # 视觉通道
        if image is not None:
            visual_signal = self.encode_image(np.array(image))
            for dim in self.visual_dims:
                signal[dim] = visual_signal[dim]
        
        # 文本通道
        if text is not None:
            text_signal = self.base_encoder.encode_text(str(text))
            for dim in self.text_dims:
                signal[dim] = text_signal[dim]
        
        # 数值通道
        if numeric is not None:
            numeric_signal = self.base_encoder.encode_numeric(float(numeric))
            for dim in self.numeric_dims:
                signal[dim] = numeric_signal[dim]
        
        # 时序通道
        if timeseries is not None:
            ts_values = list(timeseries)
            if len(ts_values) >= 2:
                # 差分编码
                diff = ts_values[-1] - ts_values[-2]
                ts_signal = self.base_encoder.encode_numeric(diff, -10, 10)
                for dim in self.temporal_dims:
                    signal[dim] = ts_signal[dim % len(ts_signal)]
        
        # 归一化
        norm = np.linalg.norm(signal)
        if norm > 0:
            signal = signal / norm
        
        return signal
    
    def get_channel_assignment(self) -> Dict[str, List[int]]:
        """获取通道分配方案"""
        return {
            "visual (图像/视频)": self.visual_dims,
            "text (文本)": self.text_dims,
            "numeric (数值)": self.numeric_dims,
            "temporal (时序/运动)": self.temporal_dims
        }


# ═══════════════════════════════════════════════════════════════
# 图像/视频模拟生成器
# ═══════════════════════════════════════════════════════════════

class ImageSimulator:
    """模拟图像/视频数据生成器 (纯 numpy，无外部依赖)"""
    
    def generate_random_image(self, size: Tuple[int, int] = (32, 32)) -> np.ndarray:
        """生成随机灰度图像"""
        return np.random.randint(0, 256, size, dtype=np.uint8)
    
    def generate_gradient_image(self, size: Tuple[int, int] = (32, 32),
                                 direction: str = "horizontal") -> np.ndarray:
        """生成渐变图像"""
        h, w = size
        if direction == "horizontal":
            img = np.linspace(0, 255, w).reshape(1, -1).repeat(h, axis=0)
        elif direction == "vertical":
            img = np.linspace(0, 255, h).reshape(-1, 1).repeat(w, axis=1)
        elif direction == "radial":
            y, x = np.ogrid[:h, :w]
            center_y, center_x = h // 2, w // 2
            img = np.sqrt((x - center_x)**2 + (y - center_y)**2)
            img = (img / img.max() * 255).astype(np.uint8)
        else:
            img = np.random.randint(0, 256, size, dtype=np.uint8)
        return img.astype(np.uint8)
    
    def generate_video_sequence(self, num_frames: int = 10,
                                 size: Tuple[int, int] = (16, 16)) -> List[np.ndarray]:
        """
        生成模拟视频序列
        一个移动的亮点
        """
        frames = []
        h, w = size
        
        for t in range(num_frames):
            frame = np.zeros((h, w), dtype=np.uint8)
            
            # 移动的正弦波模式
            for i in range(h):
                for j in range(w):
                    val = 128 + 127 * np.sin(j / w * 2 * np.pi + t * 0.5)
                    frame[i, j] = int(val)
            
            # 添加一个移动的高斯斑点
            cx = int(w * (0.3 + 0.4 * (t / num_frames)))
            cy = h // 2
            for dy in range(-3, 4):
                for dx in range(-3, 4):
                    y, x = cy + dy, cx + dx
                    if 0 <= y < h and 0 <= x < w:
                        dist = np.sqrt(dx**2 + dy**2)
                        intensity = int(200 * np.exp(-dist**2 / 2))
                        frame[y, x] = min(255, frame[y, x] + intensity)
            
            frames.append(frame)
        
        return frames
    
    def generate_scene_change(self, size: Tuple[int, int] = (16, 16)) -> List[np.ndarray]:
        """生成场景变化序列 (模拟镜头切换)"""
        frames = []
        # 前半段: 水平渐变
        for _ in range(5):
            frames.append(self.generate_gradient_image(size, "horizontal"))
        # 后半段: 垂直渐变 (场景切换)
        for _ in range(5):
            frames.append(self.generate_gradient_image(size, "vertical"))
        return frames


# ═══════════════════════════════════════════════════════════════
# 多模态感知智能体
# ═══════════════════════════════════════════════════════════════

class MultimodalPerceptionAgent:
    """
    多模态感知智能体
    可以同时处理图像、视频、文本、数值输入
    """
    
    def __init__(self, agent_id: str, dim: int = 16):
        self.agent_id = agent_id
        self.dim = dim
        self.encoder = MultimodalSpikeEncoder(dim)
        self.image_sim = ImageSimulator()
        self.frame_buffer: deque = deque(maxlen=10)
        
    def process_image(self, image: np.ndarray) -> np.ndarray:
        """处理图像输入"""
        return self.base_encoder.encode_image(image)
    
    def process_video(self, frames: List[np.ndarray]) -> List[np.ndarray]:
        """处理视频输入，返回每帧的脉冲序列"""
        signals = []
        prev = None
        for frame in frames:
            signal = self.encoder.encode_video_frame(frame, prev)
            signals.append(signal)
            prev = frame
        return signals
    
    def process_multimodal(self, image=None, text=None, 
                           numeric=None, timeseries=None) -> np.ndarray:
        """处理多模态融合输入"""
        return self.encoder.encode_multimodal(image, text, numeric, timeseries)
    
    def get_channel_info(self) -> str:
        """获取通道分配信息"""
        info = self.encoder.get_channel_assignment()
        lines = ["\n📡 多模态通道分配 (顶层16单元):", "-" * 40]
        for name, dims in info.items():
            lines.append(f"  {name:25s} → 单元 {dims}")
        lines.append("-" * 40)
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 自测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("多模态脉冲编码器测试")
    print("=" * 60)
    
    encoder = MultimodalSpikeEncoder(dim=16)
    sim = ImageSimulator()
    
    # 1. 通道分配
    print("\n📡 通道分配方案:")
    for name, dims in encoder.get_channel_assignment().items():
        print(f"  {name:25s} → 单元 {dims}")
    
    # 2. 图像编码测试
    print("\n🖼️  图像编码测试:")
    img = sim.generate_gradient_image((16, 16), "horizontal")
    signal_img = encoder.encode_image(img)
    print(f"  输入: 16x16 水平渐变图像")
    print(f"  输出: 16维脉冲, 非零维 {np.count_nonzero(signal_img)}")
    print(f"  视觉通道(0-3): {signal_img[0:4]}")
    
    # 3. 视频编码测试
    print("\n🎬 视频编码测试:")
    frames = sim.generate_video_sequence(num_frames=5, size=(8, 8))
    signals = encoder.encode_video_frame(frames[0])
    for i, frame in enumerate(frames[1:], 1):
        sig = encoder.encode_video_frame(frame, frames[i-1])
        print(f"  帧 {i}: 视觉通道={sig[0:4].round(2)}, 运动时序通道={sig[12:16].round(2)}")
    
    # 4. 多模态融合测试
    print("\n🔄 多模态融合测试:")
    signal_fusion = encoder.encode_multimodal(
        image=sim.generate_random_image((8, 8)),
        text="Stock market surges 5% today",
        numeric=175.50,
        timeseries=[150, 152, 155, 160, 175]
    )
    print(f"  融合信号: 非零维 {np.count_nonzero(signal_fusion)}, 范数 {np.linalg.norm(signal_fusion):.3f}")
    print(f"  视觉(0-3):    {signal_fusion[0:4].round(2)}")
    print(f"  文本(4-7):    {signal_fusion[4:8].round(2)}")
    print(f"  数值(8-11):   {signal_fusion[8:12].round(2)}")
    print(f"  时序(12-15):  {signal_fusion[12:16].round(2)}")
    
    # 5. 多模态感知智能体测试
    print("\n🤖 多模态感知智能体测试:")
    agent = MultimodalPerceptionAgent("multimodal_0", dim=16)
    print(agent.get_channel_info())
    
    # 处理混合输入
    mixed_signal = agent.process_multimodal(
        image=sim.generate_gradient_image((12, 12), "radial"),
        text="Breaking news: AI breakthrough",
        numeric=-2.5,
        timeseries=[100, 102, 99, 105, 110]
    )
    print(f"  混合输入脉冲: {mixed_signal.round(3)}")
    
    print("\n" + "=" * 60)
    print("多模态编码器测试通过!")
    print("=" * 60)
