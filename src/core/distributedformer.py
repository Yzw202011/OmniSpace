"""
DistributedFormer: 分布式脉冲神经网络核心
基于 Kimi Work × DistributedFormer 原型方案实现

核心特征:
- 极简单元: 16个参数，模拟生物神经元动力学
- 异步事件驱动: 无全局同步，脉冲局部传播
- 持续思考: 默认模式网络，无输入仍有自发活动
- 分形递归: 每层16单元，深度指数扩展能力
- 内置注意力: query_kv机制，无需全局注意力矩阵
- KV堆记忆: 分布式持久存储，替代Transformer的KV Cache

版本: v5.2 分形深度2扩展
  分形深度2 => 4,368单元/层 / 69K参数
"""

import json
import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查期导入，防运行时循环依赖
    from ..cutemamen.kernel import CubeGPTKernel

import numpy as np

from ..codec.spike_codec import SpikeEncoder

# ═══════════════════════════════════════════════════════════════
# 1. 基础数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class SpikePayload:
    """脉冲载荷"""
    value: float          # 脉冲值 (可正可负)
    strength: float       # 绝对强度 (0~1)
    timestamp: float      # 发射时间戳
    cycle_phase: int      # 当前节律相位

@dataclass
class SpikeMessage:
    """标准脉冲消息格式"""
    msg_type: str = "spike"
    source_agent_id: str = ""
    source_unit_id: str = ""      # 分形层级定位, e.g. "top_3_L1_7_L0_12"
    source_level: int = 0
    payload: SpikePayload = field(default_factory=lambda: SpikePayload(0.0, 0.0, 0.0, 0))
    target_agents: list[str] = field(default_factory=list)
    hops_remaining: int = 3
    priority: str = "normal"
    kv_query: dict | None = None
    trace: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "msg_type": self.msg_type,
            "source": {
                "agent_id": self.source_agent_id,
                "unit_id": self.source_unit_id,
                "level": self.source_level
            },
            "payload": {
                "value": self.payload.value,
                "strength": self.payload.strength,
                "timestamp": self.payload.timestamp,
                "cycle_phase": self.payload.cycle_phase
            },
            "routing": {
                "target_agents": self.target_agents,
                "hops_remaining": self.hops_remaining,
                "priority": self.priority
            },
            "context": {
                "kv_query": self.kv_query,
                "trace": self.trace
            }
        }


# ═══════════════════════════════════════════════════════════════
# 2. KV堆记忆条目
# ═══════════════════════════════════════════════════════════════

@dataclass
class KVEntry:
    """KV堆中的一个条目"""
    key_signal: np.ndarray    # 键信号向量 (用于query匹配)
    value_state: np.ndarray   # 值状态向量 (存储内容)
    timestamp: float
    access_count: int = 0
    last_access: float = 0.0

    def compute_score(self, query_signal: np.ndarray, key_w: np.ndarray) -> float:
        """计算query与当前条目的匹配分数"""
        if self.key_signal is None or query_signal is None:
            return 0.0
        q_norm = np.linalg.norm(query_signal)
        k_norm = np.linalg.norm(self.key_signal)
        if q_norm == 0 or k_norm == 0:
            return 0.0
        sim = np.abs(np.dot(query_signal, self.key_signal) / (q_norm * k_norm))
        time_decay = np.exp(-0.001 * (time.time() - self.timestamp))
        return float(sim * time_decay)


# ═══════════════════════════════════════════════════════════════
# 3. KV堆: 分布式持久记忆存储
# ═══════════════════════════════════════════════════════════════

class KVStack:
    """
    分布式KV堆记忆系统
    替代Transformer的KV Cache，支持跨智能体注意力查询

    v0.7.4 向量化: entries dict 仍为权威存储 (兼容外部读取),
    内部维护增量矩阵索引 (_keys/_vals 等, 条目对象持有矩阵行视图),
    query/retrieve 打分与 top-k 全程 numpy 批量计算, 无 Python 逐条循环。
    """

    def __init__(self, capacity: int = 100000, dim: int = 16,
                 retention_policy: str = "lru_7d"):
        self.capacity = capacity
        self.dim = dim
        self.entries: dict[str, KVEntry] = {}
        self.retention_policy = retention_policy
        self.lock = threading.Lock()
        self.query_w = np.random.randn(dim) * 0.1
        self.key_w = np.random.randn(dim) * 0.1
        self.value_w = np.random.randn(dim) * 0.1
        # ── 向量化索引 (行号 ↔ entry_id 双向映射) ──
        self._ids: list[str] = []            # 行号 -> entry_id
        self._row: dict[str, int] = {}       # entry_id -> 行号
        self._n = 0                          # 存活条目数
        self._alloc = 0                      # 已分配行数
        self._seq = 0                        # 插入序号 (单调递增, scan_limit 用)
        self._keys = self._vals = None       # (alloc, dim)
        self._ts = self._last_acc = self._acc_cnt = self._seqs = None

    # ── 索引维护 ──────────────────────────────────────────

    def _grow(self, min_rows: int) -> None:
        """按需扩容矩阵 (指数增长, 上限 capacity)"""
        if min_rows <= self._alloc:
            return
        new_alloc = max(min_rows, min(self.capacity, max(64, self._alloc * 2)))
        if self._alloc == 0:
            self._keys = np.zeros((new_alloc, self.dim))
            self._vals = np.zeros((new_alloc, self.dim))
            self._ts = np.zeros(new_alloc)
            self._last_acc = np.zeros(new_alloc)
            self._acc_cnt = np.zeros(new_alloc, dtype=np.int64)
            self._seqs = np.zeros(new_alloc, dtype=np.int64)
        else:
            for name in ("_keys", "_vals"):
                arr = getattr(self, name)
                grown = np.zeros((new_alloc, self.dim))
                grown[:self._alloc] = arr
                setattr(self, name, grown)
            for name in ("_ts", "_last_acc", "_acc_cnt", "_seqs"):
                arr = getattr(self, name)
                grown = np.zeros(new_alloc, dtype=arr.dtype)
                grown[:self._alloc] = arr
                setattr(self, name, grown)
        self._alloc = new_alloc

    def _ensure_sync(self) -> None:
        """外部直接改动 entries (如 entries.clear()) 后自动重建索引"""
        if len(self.entries) != self._n:
            self._rebuild_index()

    def _rebuild_index(self) -> None:
        """从 entries dict 重建矩阵索引 (dict 保持插入序)"""
        n = len(self.entries)
        self._grow(max(n, 1))
        self._ids = []
        self._row = {}
        i = 0
        for eid, entry in self.entries.items():
            self._keys[i] = self._coerce_vec(entry.key_signal)
            self._vals[i] = self._coerce_vec(entry.value_state)
            self._ts[i] = entry.timestamp
            self._last_acc[i] = entry.last_access
            self._acc_cnt[i] = entry.access_count
            self._seqs[i] = self._seq + i    # 按插入序赋递增序号
            self._ids.append(eid)
            self._row[eid] = i
            # 条目改为持有矩阵行视图 (单一数据源)
            entry.key_signal = self._keys[i]
            entry.value_state = self._vals[i]
            i += 1
        self._n = n
        self._seq += n

    def _move_row(self, src: int, dst: int) -> None:
        """行搬移 (swap-remove 用), 同步修正条目视图"""
        self._keys[dst] = self._keys[src]
        self._vals[dst] = self._vals[src]
        self._ts[dst] = self._ts[src]
        self._last_acc[dst] = self._last_acc[src]
        self._acc_cnt[dst] = self._acc_cnt[src]
        self._seqs[dst] = self._seqs[src]
        eid = self._ids[src]
        self._ids[dst] = eid
        self._row[eid] = dst
        entry = self.entries[eid]
        entry.key_signal = self._keys[dst]
        entry.value_state = self._vals[dst]

    # ── 公开接口 ──────────────────────────────────────────

    def _coerce_vec(self, vec: np.ndarray) -> np.ndarray:
        """向量定长化: 不足 dim 补零, 超长截断 (兼容变长载荷, 如 rust 插件)"""
        v = np.asarray(vec, dtype=float).ravel()
        if v.shape == (self.dim,):
            return v
        out = np.zeros(self.dim)
        n = min(len(v), self.dim)
        out[:n] = v[:n]
        return out

    def push(self, entry_id: str, key_signal: np.ndarray,
             value_state: np.ndarray) -> None:
        """推送新条目到KV堆"""
        with self.lock:
            self._ensure_sync()
            if len(self.entries) >= self.capacity:
                self._evict_lru()

            now = time.time()
            if entry_id in self._row:
                # 重复 id: 覆盖原行, 保留插入序 (dict 语义)
                r = self._row[entry_id]
                entry = self.entries[entry_id]
            else:
                self._grow(self._n + 1)
                r = self._n
                self._seq += 1
                self._ids.append(entry_id)
                self._row[entry_id] = r
                entry = KVEntry(
                    key_signal=np.zeros(self.dim),
                    value_state=np.zeros(self.dim),
                    timestamp=now, last_access=now)
                self.entries[entry_id] = entry
                self._n += 1
                self._seqs[r] = self._seq   # 新条目记录插入序 (覆盖旧 id 不刷新)
            self._keys[r] = self._coerce_vec(key_signal)
            self._vals[r] = self._coerce_vec(value_state)
            self._ts[r] = now
            self._last_acc[r] = now
            self._acc_cnt[r] = entry.access_count
            entry.timestamp = now
            entry.last_access = now
            # 条目持有矩阵行视图 (单一数据源)
            entry.key_signal = self._keys[r]
            entry.value_state = self._vals[r]

    def _score_rows(self, rows: np.ndarray, query_signal: np.ndarray) -> np.ndarray:
        """向量化打分: |cos(q,k)| × exp(-0.001·Δt), 零范数记 0 分"""
        q = np.asarray(query_signal, dtype=float)
        K = self._keys[rows]
        q_norm = np.linalg.norm(q)
        k_norms = np.linalg.norm(K, axis=1)
        denom = q_norm * k_norms
        valid = denom > 0
        dots = np.abs(K @ q)
        sims = np.where(valid, dots / np.where(valid, denom, 1.0), 0.0)
        decay = np.exp(-0.001 * (time.time() - self._ts[rows]))
        return sims * decay

    def _top_rows(self, scores: np.ndarray, top_k: int) -> np.ndarray:
        """分数 top-k 行号 (降序; argpartition + 稳定排序)"""
        m = len(scores)
        k = min(top_k, m)
        if k < m:
            sel = np.argpartition(-scores, k - 1)[:k]
        else:
            sel = np.arange(m)
        return sel[np.argsort(-scores[sel], kind="stable")]

    def query(self, query_signal: np.ndarray, top_k: int = 3) -> list[tuple[str, np.ndarray, float]]:
        """
        注意力查询 (query_kv) — 向量化
        """
        # 快速路径: 空堆直接返回
        if not self.entries:
            return []

        with self.lock:
            self._ensure_sync()
            n = self._n
            if n == 0:
                return []
            rows = np.arange(n)
            scores = self._score_rows(rows, query_signal)
            # 更新全部条目访问统计 (与旧实现语义一致)
            now = time.time()
            self._acc_cnt[:n] += 1
            self._last_acc[:n] = now
            for r in range(n):
                entry = self.entries[self._ids[r]]
                entry.access_count += 1
                entry.last_access = now

            top = self._top_rows(scores, top_k)
            total = scores[top].sum() + 1e-8
            results = []
            for r in top:
                normalized = scores[r] / total
                retrieved = self._vals[r] * self.value_w * normalized
                results.append((self._ids[r], retrieved, float(normalized)))
            return results

    def retrieve(self, query_signal: np.ndarray, top_k: int = 3,
                 scan_limit: int = 4096) -> np.ndarray:
        """
        主计算路径用的注意力检索: 返回 dim 维聚合检索向量 (向量化)

        对最近写入的 scan_limit 条记录做相似度打分 (按插入序号 _seqs
        截取, 淘汰搬移不破坏语义), 聚合 top_k 条 value 向量后 tanh 归一化。
        空堆返回零向量。
        """
        if not self.entries:
            return np.zeros(self.dim)
        with self.lock:
            self._ensure_sync()
            n = self._n
            if n == 0:
                return np.zeros(self.dim)
            if n > scan_limit:
                # 插入序号第 scan_limit 大者为阈值, 只扫最近写入的条目
                thr = np.partition(self._seqs[:n], n - scan_limit)[n - scan_limit]
                rows = np.nonzero(self._seqs[:n] >= thr)[0]
            else:
                rows = np.arange(n)
            scores = self._score_rows(rows, query_signal)
            top_local = self._top_rows(scores, top_k)
            top_rows = rows[top_local]
            total = scores[top_local].sum() + 1e-8
            weights = scores[top_local] / total
            # agg = Σ value × value_w × w
            agg = (weights @ self._vals[top_rows]) * self.value_w
            # 更新 top-k 条目的访问统计 (与旧实现语义一致)
            now = time.time()
            self._acc_cnt[top_rows] += 1
            self._last_acc[top_rows] = now
            for r in top_rows:
                entry = self.entries[self._ids[r]]
                entry.access_count += 1
                entry.last_access = now
            return np.tanh(agg)

    def _evict_lru(self) -> None:
        """LRU淘汰最久未访问的条目 (向量化 argmin + swap-remove)"""
        if not self.entries:
            return
        r = int(np.argmin(self._last_acc[:self._n]))
        eid = self._ids[r]
        del self.entries[eid]
        del self._row[eid]
        last = self._n - 1
        if r != last:
            self._move_row(last, r)
        self._ids.pop()
        self._n -= 1

    def clear_expired(self, max_age_days: float = 7.0) -> int:
        """清理过期条目"""
        with self.lock:
            self._ensure_sync()
            if self._n == 0 or self._ts is None:
                return 0
            now = time.time()
            max_age = max_age_days * 86400
            expired = [self._ids[r] for r in np.nonzero(
                (now - self._ts[:self._n]) > max_age)[0]]
            for k in expired:
                del self.entries[k]
            if expired:
                self._rebuild_index()
            return len(expired)

    def get_stats(self) -> dict:
        """获取KV堆统计信息"""
        with self.lock:
            total_access = sum(e.access_count for e in self.entries.values())
            return {
                "capacity": self.capacity,
                "used": len(self.entries),
                "utilization": len(self.entries) / self.capacity,
                "total_access": total_access
            }

    def save_to_disk(self, path: str) -> None:
        """持久化到磁盘"""
        with self.lock:
            data = {}
            for k, v in self.entries.items():
                data[k] = {
                    "key_signal": v.key_signal.tolist(),
                    "value_state": v.value_state.tolist(),
                    "timestamp": v.timestamp,
                    "access_count": v.access_count,
                    "last_access": v.last_access
                }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)

    def load_from_disk(self, path: str) -> None:
        """从磁盘加载"""
        import os
        if not os.path.exists(path):
            return
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        with self.lock:
            self.entries.clear()
            for k, v in data.items():
                self.entries[k] = KVEntry(
                    key_signal=np.array(v["key_signal"]),
                    value_state=np.array(v["value_state"]),
                    timestamp=v["timestamp"],
                    access_count=v.get("access_count", 0),
                    last_access=v.get("last_access", v["timestamp"])
                )


# ═══════════════════════════════════════════════════════════════
# 4. 脉冲神经元单元 (极简单元: 16个参数)
# ═══════════════════════════════════════════════════════════════

class SpikingUnit:
    """
    生物神经元动力学模拟单元
    恰好16个标量参数，极简高效
    脉冲动力学:
      state(t+1) = (input_gated + attn_retrieval + state_feedback + global_modulation) × decay
      output = mean(state) × w_out + b_out (若 ≥ threshold 则发射脉冲)
    """

    UNIT_PARAMS = 16  # 每个单元恰好16个标量参数

    def __init__(self, unit_id: str, dim: int = 16):
        self.unit_id = unit_id
        self.dim = dim

        # 状态 (16维向量，但不是参数)
        self.state = np.zeros(dim)
        self.fatigue = 0.0    # 疲劳度 (0~1, 越高越难发射)
        self.refractory = 0    # 不应期计数器
        # 感受野投影 (v0.5.0): 固定随机投影替代均值池化,
        # crc32(unit_id) 做种子保证跨进程可复现
        self._receptive = None

        # ═══════════════════════════════════════════════════════════════
        # 16个标量参数
        # ═══════════════════════════════════════════════════════════════
        # v0.5.0 配平: 原初始化中 w_global~0.5 的恒定调制项比输入路径
        # (w_in~0.1 × 投影~0.1) 大一个数量级, 输入信号被完全淹没,
        # 状态与输入几乎无关 (训练方法学未验证的根因)。
        # 现令输入路径与调制项量级匹配, 调制只做调制。
        self.w_in       = np.random.randn() * 1.0                 # 1. 输入权重
        self.b_in       = np.random.randn() * 0.1                 # 2. 输入偏置
        self.w_state    = np.random.randn() * 0.1                 # 3. 状态反馈权重
        self.w_out      = np.random.randn() * 0.1                 # 4. 输出权重
        self.b_out      = np.random.randn() * 0.05                # 5. 输出偏置
        self.w_attn     = np.random.randn() * 0.5                 # 6. 注意力权重
        self.b_attn     = np.random.randn() * 0.05                # 7. 注意力偏置
        self.decay      = 0.9 + np.random.random() * 0.09         # 8. 状态衰减 (0.9~0.99)
        self.gain       = max(0.5, 1.0 + np.random.randn() * 0.3) # 9. 增益
        self.w_global   = 0.1 + np.random.randn() * 0.05          # 10. 全局调制权重 (调制而非主导)
        self.threshold  = 0.5 + np.random.random() * 0.3           # 11. 发射阈值 (0.5~0.8)
        self.refractory_period = 2.0 + np.random.random() * 3.0    # 12. 不应期长度
        self.fatigue_rate = 0.05 + np.random.random() * 0.05       # 13. 疲劳积累率
        self.recovery_rate = 0.02 + np.random.random() * 0.03       # 14. 疲劳恢复率
        self.spontaneous_rate = 0.01 + np.random.random() * 0.02  # 15. 自发脉冲率
        self.w_lateral  = np.random.randn() * 0.1                 # 16. 侧向连接权重

        # 连接 (动态，不计入16个固定参数)
        self.outgoing: dict[str, float] = {}  # target_id -> weight
        self.incoming_history: deque = deque(maxlen=50)  # 输入历史

        # 统计
        self.spike_count = 0
        self.last_spike_time = 0.0

        # ═══════════════════════════════════════════════════════════════
        # STDP (Spike-Timing Dependent Plasticity) 学习机制
        # ═══════════════════════════════════════════════════════════════
        self.stdp_enabled = True
        self.stdp_A_plus = 0.01    # LTP 幅度
        self.stdp_A_minus = 0.012  # LTD 幅度
        self.stdp_tau_plus = 0.02  # 20ms
        self.stdp_tau_minus = 0.02  # 20ms
        self.spike_times: deque = deque(maxlen=100)  # 最近发射时间记录
        self.ltp_count = 0         # 长时程增强计数
        self.ltd_count = 0         # 长时程抑制计数
        self.total_weight_change = 0.0

    def count_params(self) -> int:
        """返回该单元的参数数量（应始终为16）"""
        return 16

    def record_spike_time(self, t: float) -> None:
        """记录脉冲发射时间"""
        self.spike_times.append(t)

    def stdp_update(self, pre_spike_time: float, post_spike_time: float) -> float:
        """
        STDP 权重更新

        Δt = t_post - t_pre
        若 Δt > 0 (后突触晚于前突触): LTP (权重增强)
            Δw = A+ * exp(-Δt / τ+)
        若 Δt < 0 (后突触早于前突触): LTD (权重减弱)
            Δw = -A- * exp(Δt / τ-)

        Returns:
            权重变化量 Δw
        """
        dt = post_spike_time - pre_spike_time

        if dt > 0:
            # LTP: 后突触晚于前突触，增强连接
            dw = self.stdp_A_plus * math.exp(-dt / self.stdp_tau_plus)
            self.ltp_count += 1
        elif dt < 0:
            # LTD: 后突触早于前突触，减弱连接
            dw = -self.stdp_A_minus * math.exp(dt / self.stdp_tau_minus)
            self.ltd_count += 1
        else:
            dw = 0.0

        self.total_weight_change += abs(dw)
        return dw

    def apply_stdp(self, current_time: float, all_units_map: dict[str, 'SpikingUnit'] = None) -> int:
        """
        对该单元的所有传出连接应用 STDP 更新

        Args:
            current_time: 当前时间
            all_units_map: 所有单元的映射表 (unit_id -> SpikingUnit)

        Returns:
            更新的连接数
        """
        if not self.stdp_enabled or not self.outgoing:
            return 0

        updated = 0

        # 遍历所有传出连接
        for target_id, current_weight in list(self.outgoing.items()):
            # 获取目标单元的最近发射时间
            target_spike_time = None

            if all_units_map and target_id in all_units_map:
                target_unit = all_units_map[target_id]
                if target_unit.spike_times:
                    target_spike_time = target_unit.spike_times[-1]
            else:
                # 简化：使用当前时间近似（目标单元在当前步也发射了）
                target_spike_time = current_time

            # 获取本单元最近发射时间
            if not self.spike_times:
                continue
            my_spike_time = self.spike_times[-1]

            # 如果目标单元没有发射记录，跳过
            if target_spike_time is None:
                continue

            # 计算 STDP
            dw = self.stdp_update(my_spike_time, target_spike_time)

            # 应用权重更新
            new_weight = current_weight + dw
            new_weight = np.clip(new_weight, -1.0, 1.0)  # 限制范围
            self.outgoing[target_id] = new_weight
            updated += 1

        return updated

    def get_stdp_stats(self) -> dict:
        """获取 STDP 学习统计"""
        return {
            "ltp_count": self.ltp_count,
            "ltd_count": self.ltd_count,
            "total_weight_change": float(self.total_weight_change),
            "avg_weight_change": float(self.total_weight_change / max(1, self.ltp_count + self.ltd_count)),
            "spike_times_recorded": len(self.spike_times)
        }

    def step(self, raw_input: np.ndarray,
             attn_retrieval: np.ndarray,
             global_modulation: float = 1.0,
             dt: float = 1.0) -> SpikeMessage | None:
        """
        单步脉冲动力学

        Args:
            raw_input: 外部输入信号 (dim维)
            attn_retrieval: KV堆注意力检索结果 (dim维)
            global_modulation: 全局调制强度 (节律控制)
            dt: 时间步长

        Returns:
            SpikeMessage 如果发射脉冲，否则 None
        """
        # 不应期检查
        if self.refractory > 0:
            self.refractory -= 1
            self.fatigue = max(0.0, self.fatigue - self.recovery_rate)
            self.state *= self.decay  # 仅衰减
            return None

        # 输入门控 (v0.5.0 感受野投影: 单元只"看到"自己固定的随机投影)
        if self._receptive is None:
            import zlib
            seed = zlib.crc32(self.unit_id.encode("utf-8"))
            self._receptive = np.random.RandomState(seed).randn(self.dim) / np.sqrt(self.dim)
        eff_input = float(self._receptive @ raw_input) if raw_input is not None else 0.0
        mean_input = eff_input
        input_gated = self.gain * np.tanh(self.w_in * mean_input + self.b_in)

        # 注意力检索 (使用均值，标量权重)
        mean_attn = np.mean(attn_retrieval) if attn_retrieval is not None else 0.0
        attn_contrib = self.w_attn * mean_attn + self.b_attn

        # 状态反馈 (使用状态均值，标量权重)
        mean_state = np.mean(self.state)
        state_feedback = self.w_state * mean_state

        # 全局调制
        modulation = global_modulation * self.w_global

        # 自发脉冲 (默认模式网络)
        spontaneous = np.random.random() < self.spontaneous_rate

        # 状态更新: state(t+1) = (input_gated + attn + state_feedback + global) × decay
        total_input = input_gated + attn_contrib + state_feedback + modulation
        if spontaneous:
            total_input += 0.3  # 自发脉冲增加输入

        self.state = total_input * self.decay + self.state * 0.1

        # 输出计算: output = mean(state) * w_out + b_out
        output = mean_state * self.w_out + self.b_out

        # 疲劳影响阈值
        effective_threshold = self.threshold + self.fatigue

        # 检查是否发射脉冲
        if output >= effective_threshold or spontaneous:
            self.spike_count += 1
            self.last_spike_time = time.time()
            self.fatigue = min(1.0, self.fatigue + self.fatigue_rate)
            self.refractory = int(self.refractory_period)

            # 记录脉冲发射时间 (STDP学习)
            self.record_spike_time(self.last_spike_time)

            # 构建脉冲消息
            strength = min(1.0, abs(output))
            msg = SpikeMessage(
                source_unit_id=self.unit_id,
                source_level=0,  # 基础单元
                payload=SpikePayload(
                    value=float(output),
                    strength=strength,
                    timestamp=time.time(),
                    cycle_phase=0
                ),
                hops_remaining=3
            )
            return msg
        else:
            # 疲劳恢复
            self.fatigue = max(0.0, self.fatigue - self.recovery_rate)
            return None

    def get_state_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "spike_count": self.spike_count,
            "fatigue": float(self.fatigue),
            "threshold": float(self.threshold),
            "state_norm": float(np.linalg.norm(self.state))
        }


# ═══════════════════════════════════════════════════════════════
# 5. 分形递归网络层
# ═══════════════════════════════════════════════════════════════

class FractalLayer:
    """
    分形递归层: 每层16个单元，深度指数扩展能力

    基础单元数 = 16 + 16^2 + ... + 16^(depth+1)
    总参数 = 16 × Σ(16^k for k=1 to depth+1)

    v5.2 分形深度2 => 4,368单元/层 / 69K参数
    """

    def __init__(self, layer_id: str, depth: int = 0, dim: int = 16):
        self.layer_id = layer_id
        self.depth = depth
        self.dim = dim
        self.units: list[SpikingUnit] = []
        self.sub_layers: list[FractalLayer] = []

        # 创建16个单元
        for i in range(16):
            uid = f"{layer_id}_U{i}"
            self.units.append(SpikingUnit(uid, dim))

        # 递归创建子层 (深度>0时)
        if depth > 0:
            for i in range(16):
                sub_id = f"{layer_id}_L{i}"
                self.sub_layers.append(FractalLayer(sub_id, depth - 1, dim))

        # 层内连接 (小世界网络)
        self._build_connections()

        # 缓存所有单元引用 (避免每步递归)
        self._all_units_cache: list[SpikingUnit] = self._build_all_units_cache()
        self._build_vec_arrays()

    def _build_vec_arrays(self):
        """构建向量化计算用的批量数组 (适配标量权重)"""
        units = self._all_units_cache
        self.N = len(units)
        if self.N == 0:
            return
        self.dim = units[0].dim
        # 感受野投影矩阵 (v0.5.0): 每单元一个固定的随机投影向量,
        # 替代全局均值池化, 保留输入分布信息 (惰性构建, 种子取自层ID)
        self._receptive = None
        self._vec_state = np.zeros((self.N, self.dim))
        self._vec_threshold = np.zeros(self.N)
        self._vec_fatigue = np.zeros(self.N)
        self._vec_refractory = np.zeros(self.N, dtype=np.int32)
        self._vec_gain = np.zeros(self.N)
        self._vec_decay = np.zeros(self.N)
        self._vec_refractory_period = np.zeros(self.N)
        self._vec_fatigue_rate = np.zeros(self.N)
        self._vec_recovery_rate = np.zeros(self.N)
        self._vec_spontaneous_rate = np.zeros(self.N)
        self._vec_b_in = np.zeros(self.N)
        self._vec_b_out = np.zeros(self.N)
        self._vec_b_attn = np.zeros(self.N)
        self._vec_w_global = np.zeros(self.N)
        # 标量权重 (N,)
        self._vec_w_in = np.zeros(self.N)
        self._vec_w_state = np.zeros(self.N)
        self._vec_w_out = np.zeros(self.N)
        self._vec_w_attn = np.zeros(self.N)
        for i, u in enumerate(units):
            self._vec_state[i] = u.state
            self._vec_threshold[i] = u.threshold
            self._vec_fatigue[i] = u.fatigue
            self._vec_refractory[i] = u.refractory
            self._vec_gain[i] = u.gain
            self._vec_decay[i] = u.decay
            self._vec_refractory_period[i] = u.refractory_period
            self._vec_fatigue_rate[i] = u.fatigue_rate
            self._vec_recovery_rate[i] = u.recovery_rate
            self._vec_spontaneous_rate[i] = u.spontaneous_rate
            self._vec_b_in[i] = u.b_in
            self._vec_b_out[i] = u.b_out
            self._vec_b_attn[i] = u.b_attn
            self._vec_w_global[i] = u.w_global
            self._vec_w_in[i] = u.w_in
            self._vec_w_state[i] = u.w_state
            self._vec_w_out[i] = u.w_out
            self._vec_w_attn[i] = u.w_attn

    def _sync_units_to_vec(self):
        for i, u in enumerate(self._all_units_cache):
            self._vec_state[i] = u.state
            self._vec_fatigue[i] = u.fatigue
            self._vec_refractory[i] = u.refractory

    def _sync_vec_to_units(self):
        for i, u in enumerate(self._all_units_cache):
            u.state = self._vec_state[i].copy()
            u.fatigue = float(self._vec_fatigue[i])
            u.refractory = int(self._vec_refractory[i])

    def _build_all_units_cache(self) -> list[SpikingUnit]:
        """一次性构建所有单元缓存"""
        all_units = self.units[:]
        for sub in self.sub_layers:
            all_units.extend(sub._all_units_cache)
        return all_units

    def get_all_units(self) -> list[SpikingUnit]:
        """获取所有单元 (使用缓存)"""
        return self._all_units_cache

    def get_unit_count(self) -> int:
        """获取总单元数"""
        return len(self._all_units_cache)

    def _build_connections(self) -> None:
        """构建小世界网络连接"""
        n = len(self.units)
        for i, u in enumerate(self.units):
            # 每个单元连接到2-4个其他单元 (小世界特性)
            num_conn = 2 + int(np.random.random() * 3)
            targets = random.sample(range(n), min(num_conn, n - 1))
            for t in targets:
                if t != i:
                    weight = np.random.randn() * 0.1
                    u.outgoing[f"{self.layer_id}_U{t}"] = weight

    def step(self, layer_input: np.ndarray,
             global_kv: KVStack,
             global_modulation: float = 1.0,
             training_mode: bool = False) -> list[SpikeMessage]:
        """
        单步执行: 向量化并行计算 (标量权重版本)
        """
        if not hasattr(self, 'N') or self.N == 0:
            return []

        self._sync_units_to_vec()

        active = self._vec_refractory == 0
        input_signal = layer_input[:self.dim] if len(layer_input) >= self.dim else layer_input
        # KV 堆注意力检索真实接入主计算路径 (v0.5.0):
        # 以本层输入为查询, 检索全局 KV 堆得到 dim 维向量
        attn_retrieval = global_kv.retrieve(input_signal)

        # 感受野投影 (v0.5.0): 每单元用固定随机投影代替全局均值,
        # 单元间输入产生差异, 保留分布信息
        if self._receptive is None:
            import zlib
            seed = zlib.crc32(self.layer_id.encode("utf-8"))
            rng = np.random.RandomState(seed)
            self._receptive = rng.randn(self.N, self.dim) / np.sqrt(self.dim)
        proj_input = self._receptive @ input_signal  # (N,)
        mean_attn = np.mean(attn_retrieval)

        input_gated = self._vec_gain * np.tanh(self._vec_w_in * proj_input + self._vec_b_in)
        attn_contrib = self._vec_w_attn * mean_attn + self._vec_b_attn
        state_feedback = self._vec_w_state * np.mean(self._vec_state, axis=1)
        modulation = global_modulation * self._vec_w_global

        total = input_gated + attn_contrib + state_feedback + modulation
        spontaneous = np.random.random(self.N) < self._vec_spontaneous_rate

        total_exp = total[:, np.newaxis]
        decay_exp = self._vec_decay[:, np.newaxis]
        state_new = total_exp * decay_exp + self._vec_state * 0.1
        self._vec_state[active] = state_new[active]

        # 输出: mean(state) * w_out + b_out
        mean_state = np.mean(self._vec_state, axis=1)
        output = mean_state * self._vec_w_out + self._vec_b_out
        eff_threshold = self._vec_threshold + self._vec_fatigue
        spike_mask = ((output >= eff_threshold) | spontaneous) & active

        self._vec_fatigue[spike_mask] = np.minimum(1.0, self._vec_fatigue[spike_mask] + self._vec_fatigue_rate[spike_mask])
        self._vec_refractory[spike_mask] = self._vec_refractory_period[spike_mask].astype(np.int32)

        not_spike = active & ~spike_mask
        self._vec_fatigue[not_spike] = np.maximum(0.0, self._vec_fatigue[not_spike] - self._vec_recovery_rate[not_spike])
        self._vec_refractory[not_spike] = np.maximum(0, self._vec_refractory[not_spike] - 1)

        inactive = ~active
        self._vec_state[inactive] *= decay_exp[inactive]
        self._vec_refractory[inactive] = np.maximum(0, self._vec_refractory[inactive] - 1)
        self._vec_fatigue[inactive] = np.maximum(0.0, self._vec_fatigue[inactive] - self._vec_recovery_rate[inactive])

        self._sync_vec_to_units()

        spikes = []
        for idx in np.where(spike_mask)[0]:
            unit = self._all_units_cache[idx]
            strength = min(1.0, abs(output[idx]))
            msg = SpikeMessage(
                source_unit_id=unit.unit_id,
                source_level=0,
                payload=SpikePayload(
                    value=float(output[idx]),
                    strength=strength,
                    timestamp=time.time(),
                    cycle_phase=0
                ),
                hops_remaining=3
            )
            msg.source_agent_id = self.layer_id
            spikes.append(msg)

        return spikes

    def get_stats(self) -> dict:
        """获取层统计"""
        all_units = self._all_units_cache
        total_spikes = sum(u.spike_count for u in all_units)
        avg_fatigue = np.mean([u.fatigue for u in all_units])
        active_units = sum(1 for u in all_units if u.fatigue < 0.5)

        return {
            "layer_id": self.layer_id,
            "depth": self.depth,
            "total_units": len(all_units),
            "total_spikes": total_spikes,
            "avg_fatigue": float(avg_fatigue),
            "active_units": active_units,
            "active_ratio": active_units / len(all_units) if all_units else 0
        }


# ═══════════════════════════════════════════════════════════════
# 6. 多模态顶层模块: InputModule / OutputModule
# ═══════════════════════════════════════════════════════════════

SUPPORTED_MODALITIES = ("numeric", "text", "timeseries", "image")


class InputModule:
    """独立顶层输入模块: 一种模态一组脉冲单元 + 绑定编码器

    每种模态拥有自己的 16 个 SpikingUnit, 原始数据在本模块内完成
    编码 → 单元步进 → 脉冲/模式产出, 模态之间互不干扰。
    """

    def __init__(self, modality: str, dim: int = 16, n_units: int = 16):
        if modality not in SUPPORTED_MODALITIES:
            raise ValueError(
                f"不支持的模态: {modality!r}, 可选: {SUPPORTED_MODALITIES}"
            )
        self.modality = modality
        self.dim = dim
        self.encoder = SpikeEncoder(dim=dim)
        self.units = [SpikingUnit(f"{modality}_U{i}", dim) for i in range(n_units)]
        self.last_spikes: list[SpikeMessage] = []

    def encode(self, raw: Any) -> np.ndarray:
        """原始数据 → dim 维脉冲信号 (数值向量直接透传)"""
        if isinstance(raw, np.ndarray) and raw.ndim == 1:
            signal = np.zeros(self.dim)
            n = min(len(raw), self.dim)
            signal[:n] = raw[:n]
            return signal
        if self.modality == "numeric":
            return self.encoder.encode_numeric(float(raw))
        if self.modality == "text":
            return self.encoder.encode_text(str(raw))
        if self.modality == "timeseries":
            signals = self.encoder.encode_timeseries(list(raw))
            return signals[-1] if signals else np.zeros(self.dim)
        # image: 2D 数组 (灰度), 确定性 4×4 平均池化
        return self.encoder.encode_image(np.asarray(raw, dtype=float))

    def step(self, raw: Any, modulation: float = 1.0,
             attn: np.ndarray | None = None) -> list[SpikeMessage]:
        """编码并单步执行本模块的所有单元 (attn: KV 检索向量)"""
        signal = self.encode(raw)
        attn_vec = attn if attn is not None else np.zeros(self.dim)
        spikes = []
        for unit in self.units:
            spike = unit.step(signal, attn_vec, modulation)
            if spike:
                spikes.append(spike)
        self.last_spikes = spikes
        return spikes

    def get_pattern(self) -> np.ndarray:
        """模块激活模式 (长度 = 单元数)"""
        pattern = np.zeros(len(self.units))
        for i, unit in enumerate(self.units):
            pattern[i] = np.linalg.norm(unit.state) * (1 - unit.fatigue)
        return pattern

    def reset_state(self) -> None:
        for unit in self.units:
            unit.state = np.zeros(self.dim)
            unit.fatigue = 0.0
            unit.refractory = 0
        self.last_spikes = []


class OutputModule:
    """独立顶层输出模块: 16 个脉冲单元, 模式提取供动作解码"""

    def __init__(self, dim: int = 16, n_units: int = 16):
        self.dim = dim
        self.units = [SpikingUnit(f"output_U{i}", dim) for i in range(n_units)]
        self.last_spikes: list[SpikeMessage] = []

    def step(self, signal: np.ndarray, modulation: float = 1.0,
             attn: np.ndarray | None = None) -> list[SpikeMessage]:
        attn_vec = attn if attn is not None else np.zeros(self.dim)
        spikes = []
        for unit in self.units:
            spike = unit.step(signal, attn_vec, modulation)
            if spike:
                spike.source_agent_id = "output"
                spikes.append(spike)
        self.last_spikes = spikes
        return spikes

    def get_pattern(self) -> np.ndarray:
        pattern = np.zeros(len(self.units))
        for i, unit in enumerate(self.units):
            pattern[i] = np.linalg.norm(unit.state) * (1 - unit.fatigue)
        return pattern

    def reset_state(self) -> None:
        for unit in self.units:
            unit.state = np.zeros(self.dim)
            unit.fatigue = 0.0
            unit.refractory = 0
        self.last_spikes = []


# ═══════════════════════════════════════════════════════════════
# 6.5 CubeGPT: 立方体连接的多模态脉冲大模型
# ═══════════════════════════════════════════════════════════════

def calculate_cube_scale(depth: int, n_faces: int = 4) -> dict:
    """计算 CubeGPT 规模

    每个面 (CubeFace) = 输入端口 16 单元 + 分形皮层 Σ16^k (k=1..depth+1) 单元。
    深度2时皮层 4,368 单元, 标称 ~4,400 单元/面;
    4 面 × 4,400 单元 × 16 参数 ≈ 281K 参数。
    """
    port_units = 16
    cortex_units = sum(16 ** k for k in range(1, depth + 2))
    units_per_face = port_units + cortex_units
    total_units = units_per_face * n_faces + 16  # + OutputModule 头部
    return {
        "faces": n_faces,
        "depth": depth,
        "cortex_units_per_face": cortex_units,
        "units_per_face": units_per_face,
        "total_units": total_units,
        "total_params": total_units * 16,
        "nominal_params": n_faces * 4400 * 16,
        "description": (
            f"CubeGPT 深度{depth}: {n_faces}面 × {units_per_face}单元 "
            f"/ {total_units * 16 // 1000}K参数 "
            f"(标称 {n_faces * 4400 * 16 // 1000}K)"
        )
    }


class CubeFace:
    """CubeGPT 的一个面: 一种模态的输入端口 + 分形皮层

    - port:   InputModule (编码器 + 16 端口单元)
    - cortex: FractalLayer 分形递归皮层 (深度2 = 4,368 单元)
    - inbox:  来自环形侧连 (立方体棱) 的邻面脉冲缓冲
    """

    def __init__(self, modality: str, depth: int = 2, dim: int = 16):
        self.modality = modality
        self.depth = depth
        self.dim = dim
        self.port = InputModule(modality, dim=dim)
        self.cortex = FractalLayer(f"face_{modality}", depth, dim)
        self.inbox = np.zeros(dim)
        self.last_spikes: list[SpikeMessage] = []

    def step(self, raw: Any, kv_stack: "KVStack", modulation: float = 1.0,
             training_mode: bool = False,
             attn: np.ndarray | None = None) -> list[SpikeMessage]:
        """端口编码 → 皮层计算 (含侧向输入), 返回皮层脉冲 (attn: KV 检索)"""
        self.port.step(raw, modulation, attn)
        port_pattern = self.port.get_pattern()
        face_input = np.tanh(port_pattern + self.inbox)
        spikes = self.cortex.step(face_input, kv_stack, modulation, training_mode)
        self.last_spikes = spikes
        return spikes

    def collect_outgoing(self) -> np.ndarray:
        """将本面脉冲聚合为发往环形邻面的向量"""
        out = np.zeros(self.dim)
        for sp in self.last_spikes:
            idx = hash(sp.source_unit_id) % self.dim
            out[idx] += sp.payload.value * sp.payload.strength
        return np.tanh(out)

    def get_units(self) -> list[SpikingUnit]:
        """端口 + 皮层全部单元 (STDP/重置/统计用)"""
        return self.port.units + self.cortex._all_units_cache

    def reset_state(self) -> None:
        self.port.reset_state()
        self.inbox = np.zeros(self.dim)
        self.last_spikes = []
        layer = self.cortex
        for unit in layer._all_units_cache:
            unit.state = np.zeros(self.dim)
            unit.fatigue = 0.0
            unit.refractory = 0
        if hasattr(layer, '_vec_state') and layer.N > 0:
            layer._vec_state[:] = 0.0
            layer._vec_fatigue[:] = 0.0
            layer._vec_refractory[:] = 0


class CubeGPT:
    """
    CubeGPT — 立方体连接的多模态脉冲大模型

    命名: Cube 指连接方式 (4 个模态面构成立方体侧面, 以"棱"环形侧连);
          GPT 致敬 ChatGPT (Generative Pulse Transformer 的自嘲式缩写)。

    规模 (默认深度2): 4 面 × ~4,400 单元 × 16 参数 ≈ 281K 参数
      - 每面 = 输入端口(16) + 分形皮层(4,368)
      - 面间连接: numeric → text → timeseries → image → numeric 环形棱,
        每步把本面脉冲聚合后注入邻面下一拍的输入
      - 顶层输出模块 OutputModule (16 单元) 作为生成/动作头部

    API 与 v0.3.0 多模态接口一致:
        gpt.step({"numeric": 1.5, "text": "..."})
    """

    CUBE_RING = ("numeric", "text", "timeseries", "image")

    def __init__(self, depth: int = 2, dim: int = 16,
                 kv_capacity: int = 100000,
                 training_mode: bool = False,
                 modalities: list[str] | None = None):
        self.depth = depth
        self.dim = dim
        self.training_mode = training_mode

        mods = list(modalities) if modalities else list(self.CUBE_RING)
        self.ring = [m for m in self.CUBE_RING if m in mods]
        # 面按立方体侧面顺序排列, face[i] 的"棱"指向 face[(i+1) % n]
        self.faces: dict[str, CubeFace] = {
            m: CubeFace(m, depth=depth, dim=dim) for m in mods
        }

        # v0.7.0 随用随载注册表: 模态 → .dfpkg 路径 (面未加载, 用到时热加载)
        self._face_registry: dict[str, str] = {}

        # 顶层输出头部
        self.output_module = OutputModule(dim=dim)

        # 全局共享 KV 堆
        self.kv_stack = KVStack(capacity=kv_capacity, dim=dim)

        # 节律
        self.global_modulation = 1.0
        self.cycle_phase = 0
        self.think_phase = 80
        self.inhibit_phase = 40
        self.cycle_length = 120

        self.total_steps = 0
        self.learning_enabled = True
        self._units_map: dict[str, SpikingUnit] = {}
        self._all_units: list[SpikingUnit] = []
        self._build_units_map()

    # ── 兼容 v0.3.0 多模态接口 ──────────────────────────────
    @property
    def input_modules(self) -> dict[str, InputModule]:
        return {name: face.port for name, face in self.faces.items()}

    @property
    def output_units(self) -> list[SpikingUnit]:
        return self.output_module.units

    def _build_units_map(self) -> None:
        self._units_map.clear()
        self._all_units = []
        for face in self.faces.values():
            for u in face.get_units():
                self._units_map[u.unit_id] = u
                self._all_units.append(u)
        for u in self.output_module.units:
            self._units_map[u.unit_id] = u
            self._all_units.append(u)

    def enable_learning(self, enabled: bool = True) -> None:
        self.learning_enabled = enabled
        for u in self._all_units:
            u.stdp_enabled = enabled

    def _apply_stdp_to_all(self) -> None:
        if not self.learning_enabled:
            return
        for unit in self._all_units:
            if unit.spike_times:
                unit.apply_stdp(time.time(), self._units_map)

    def get_stdp_stats(self) -> dict:
        total_ltp = sum(u.ltp_count for u in self._all_units)
        total_ltd = sum(u.ltd_count for u in self._all_units)
        total_weight_change = sum(u.total_weight_change for u in self._all_units)
        return {
            "learning_enabled": self.learning_enabled,
            "total_ltp": total_ltp,
            "total_ltd": total_ltd,
            "total_weight_change": float(total_weight_change),
            "avg_weight_change": float(total_weight_change / max(1, total_ltp + total_ltd))
        }

    def get_global_modulation(self) -> float:
        if self.cycle_phase < self.think_phase:
            progress = self.cycle_phase / self.think_phase
            return 1.0 - 0.5 * progress
        progress = (self.cycle_phase - self.think_phase) / self.inhibit_phase
        return 0.5 - 0.4 * progress

    def step(self, inputs: dict[str, Any]) -> list[SpikeMessage]:
        """
        CubeGPT 单步执行 (多模态)

        Args:
            inputs: {模态名: 原始数据} 字典, 未提供的面仅处理侧向输入。
        Returns:
            输出模块发射的脉冲消息
        """
        self.total_steps += 1
        self.cycle_phase = (self.cycle_phase + 1) % self.cycle_length
        self.global_modulation = self.get_global_modulation()

        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise TypeError(
                "step() 需要 {模态: 数据} 字典, "
                f"例如 {{'numeric': 1.5, 'text': '...'}}; 收到 {type(inputs).__name__}"
            )
        unknown = set(inputs) - set(self.faces)
        # v0.7.0 随用随载: 输入用到已注册 pkg 的未加载面时, 现场热加载
        for m in list(unknown):
            if m in self._face_registry:
                self.load_face(m)
                unknown.discard(m)
        if unknown:
            raise KeyError(
                f"未知输入模态 {sorted(unknown)}, 已启用: {list(self.faces)}, "
                f"已注册待载: {sorted(set(self._face_registry) - set(self.faces))}"
            )

        # KV 注意力检索 (v0.5.0 接入主路径): 以上一拍融合输入为查询
        attn = self.kv_stack.retrieve(
            getattr(self, "_last_input", np.zeros(self.dim))
        )

        # 1. 各面独立计算 (端口编码 + 皮层 + 收取环形棱传入的邻面脉冲)
        face_spikes: dict[str, list[SpikeMessage]] = {}
        for name, face in self.faces.items():
            raw = inputs.get(name)
            if raw is not None:
                spikes = face.step(raw, self.kv_stack, self.global_modulation, self.training_mode, attn)
            else:
                # 无外部输入的面仍消费侧向脉冲 (持续思考)
                spikes = face.step(
                    np.zeros(self.dim), self.kv_stack,
                    self.global_modulation, self.training_mode, attn
                ) if np.any(face.inbox) else []
            face_spikes[name] = spikes or []

        # 2. 立方体棱: 本面脉冲 → 邻面下一拍的 inbox
        for i, name in enumerate(self.ring):
            nxt = self.ring[(i + 1) % len(self.ring)]
            self.faces[nxt].inbox = self.faces[name].collect_outgoing()

        # 3. 融合各面皮层输出 → 输出头部
        fused = np.zeros(self.dim)
        for name in self.ring:
            contrib = self.faces[name].collect_outgoing()
            fused += 0.5 * contrib
        head_input = np.tanh(fused)
        output_spikes = self.output_module.step(head_input, self.global_modulation, attn)
        self._last_input = head_input

        # 4. KV 堆写入 (非训练模式)
        if not self.training_mode:
            for i, unit in enumerate(self.output_module.units):
                if unit.spike_count > 0:
                    self.kv_stack.push(
                        f"output_{i}_{self.total_steps}", unit.state, unit.state
                    )

        # 5. STDP
        if self.learning_enabled:
            self._apply_stdp_to_all()

        return output_spikes

    def get_output_pattern(self) -> np.ndarray:
        return self.output_module.get_pattern()

    def get_network_stats(self) -> dict:
        total_units = len(self._all_units)
        return {
            "model": "CubeGPT",
            "depth": self.depth,
            "faces": list(self.faces),
            "total_units": total_units,
            "total_params": total_units * 16,
            "total_spikes": sum(u.spike_count for u in self._all_units),
            "cycle_phase": self.cycle_phase,
            "global_modulation": float(self.global_modulation),
            "faces_stats": {
                name: {
                    "cortex_units": len(face.cortex._all_units_cache),
                    "total_spikes": sum(u.spike_count for u in face.get_units()),
                    "last_step_spikes": len(face.last_spikes),
                }
                for name, face in self.faces.items()
            },
            "kv_stats": self.kv_stack.get_stats(),
            "total_steps": self.total_steps,
            "stdp": self.get_stdp_stats()
        }

    def reset_state(self) -> None:
        for face in self.faces.values():
            face.reset_state()
        self.output_module.reset_state()
        self.cycle_phase = 0
        self.global_modulation = 1.0
        self.total_steps = 0
        # v0.8.6 修复: 上一步融合输入也属于运行状态, 不重置会导致
        # reset 后首次 step 的 KV 注意力检索仍查询历史输入 (特征提取
        # 出现跨样本泄漏, 思考插件知识迁移因此不可复现)
        self._last_input = np.zeros(self.dim)

    # ── v0.7.0 模态面独立化: pkg 存档与随用随载热加载 ──────────

    def _rebuild_ring(self) -> None:
        """按立方体侧面顺序重建环形棱 (加载/卸载面后调用)"""
        self.ring = [m for m in self.CUBE_RING if m in self.faces]

    def export_face(self, modality: str, path: str, **manifest_kwargs) -> dict:
        """把一个模态面导出为 .dfpkg 存档 (manifest + weights + memory)"""
        from . import face_pkg
        return face_pkg.export_face(self, modality, path, **manifest_kwargs)

    def import_face(self, path: str, modality: str | None = None) -> dict:
        """导入 .dfpkg: 替换同模态面或新增模态面 (自由导出导入的另一半)"""
        from . import face_pkg
        return face_pkg.import_face(self, path, modality)

    def register_face_pkg(self, path: str, modality: str | None = None) -> dict:
        """注册 pkg 到随用随载注册表 (只记路径, 不加载权重)"""
        from . import face_pkg
        return face_pkg.register_pkg(self, path, modality)

    def unload_face(self, modality: str, pkg_path: str | None = None) -> str:
        """卸载模态面释放内存; 默认先自动导出 pkg 保证可恢复 (随用随载)"""
        if modality not in self.faces:
            raise KeyError(f"模态面 {modality!r} 未加载")
        from . import face_pkg
        if pkg_path is None:
            pkg_path = os.path.join(
                "face_pkgs", f"{modality}{face_pkg.DFPKG_SUFFIX}")
        face_pkg.export_face(self, modality, pkg_path)
        del self.faces[modality]
        self._rebuild_ring()
        self._build_units_map()
        self._face_registry[modality] = pkg_path
        return pkg_path

    def load_face(self, modality: str, pkg_path: str | None = None) -> dict:
        """从 pkg 热加载一个模态面 (注册表里的路径或显式路径)"""
        path = pkg_path or self._face_registry.get(modality)
        if not path:
            raise KeyError(
                f"模态 {modality!r} 无已注册的 pkg, "
                f"请先 register_face_pkg() 或传入 pkg_path")
        return self.import_face(path, modality)

    def list_faces(self) -> dict[str, list[str]]:
        """已加载 / 已注册未加载的模态面"""
        return {
            "loaded": list(self.faces),
            "registered": [m for m in self._face_registry if m not in self.faces],
        }

    # ── v0.7.2 模型精简: 转换为内核形态 (必要思考 + 思考插件) ───

    def to_kernel(self, memory_budget_mb: float | None = None) -> "CubeGPTKernel":
        # 局部导入防循环依赖 (cutemamen.kernel 不回引 core)
        from ..cutemamen.kernel import CubeGPTKernel as _CubeGPTKernel

        """零拷贝转换为 CubeGPTKernel 精简形态 (v0.7.2)

        模态面皮层计算整体外移为 FacePlugin 思考插件 (同一 CubeFace
        对象, 权重/状态零拷贝); 内核只保留必要思考: 棱路由 / KV 工作
        记忆 / 输出头 / 节律。转换后 step() 行为与经典形态一致。
        """
        from ..cutemamen.face_bridge import FacePlugin
        kernel = _CubeGPTKernel(
            depth=self.depth, dim=self.dim, modalities=[],
            kv_capacity=self.kv_stack.capacity,
            memory_budget_mb=memory_budget_mb,
            training_mode=self.training_mode)
        # 零拷贝迁移: 同一 KV 堆 / 输出头 / 面对象
        kernel.working_memory.kv_stack = self.kv_stack
        kernel.output_module = self.output_module
        kernel.total_steps = self.total_steps
        kernel.cycle_phase = self.cycle_phase
        kernel.global_modulation = self.global_modulation
        kernel.learning_enabled = self.learning_enabled
        kernel._last_input = getattr(self, "_last_input", np.zeros(self.dim))
        for m, face in self.faces.items():
            kernel.mount(FacePlugin(m, depth=self.depth, dim=self.dim,
                                    face=face))
        for m, path in self._face_registry.items():
            if m not in kernel.plugins:
                kernel.registry[m] = path
        return kernel


class DistributedFormer:
    """
    DistributedFormer 完整网络

    架构: 分形递归堆叠
    - 深度0: 16单元 (浅层输出)
    - 深度1: 16 + 16×16 = 272单元
    - 深度2: 16 + 16×16 + 16×16×16 = 4,368单元 (v5.2)
    - 深度3: 65,536单元 (远期)

    包含:
    - 输入层: 接收编码后的环境脉冲
    - 思考层: num_think_layers层分形递归异步计算
    - 输出层: 生成动作脉冲
    - KV堆:  持久工作记忆
    """

    def __init__(self, depth: int = 2, dim: int = 16,
                 kv_capacity: int = 100000,
                 num_think_layers: int = 1,
                 training_mode: bool = False,
                 modalities: list[str] | None = None):
        self.depth = depth
        self.dim = dim
        self.num_think_layers = num_think_layers
        self.training_mode = training_mode  # 训练模式: 跳过KV查询以加速

        # 顶层多模态输入模块: 每种模态独立一组脉冲单元 + 编码器
        mods = list(modalities) if modalities else list(SUPPORTED_MODALITIES)
        self.input_modules: dict[str, InputModule] = {
            m: InputModule(m, dim=dim) for m in mods
        }
        # 模态融合权重 (后续可学习)
        self.modality_weights: dict[str, float] = {m: 0.5 for m in mods}

        # 思考层: num_think_layers层分形递归
        self.think_layers: list[FractalLayer] = []
        for i in range(num_think_layers):
            self.think_layers.append(FractalLayer(f"think_L{i}", depth, dim))

        # 顶层输出模块 (独立于输入与思考层)
        self.output_module = OutputModule(dim=dim)

        # KV堆 (全局共享)
        self.kv_stack = KVStack(capacity=kv_capacity, dim=dim)

        # 全局调制 (节律控制)
        self.global_modulation = 1.0
        self.cycle_phase = 0
        self.think_phase = 80
        self.inhibit_phase = 40
        self.cycle_length = 120

        # 统计
        self.total_steps = 0

        # STDP 全局学习开关
        self.learning_enabled = True
        self._units_map: dict[str, SpikingUnit] = {}
        self._all_units: list[SpikingUnit] = []
        self._build_units_map()

    @property
    def input_units(self) -> list[SpikingUnit]:
        """向后兼容: numeric 模态模块的单元 (旧单输入层)"""
        return self.input_modules["numeric"].units

    @property
    def output_units(self) -> list[SpikingUnit]:
        """输出模块的单元 (保持旧属性名可用)"""
        return self.output_module.units

    def _build_units_map(self) -> None:
        """构建所有单元的映射表和缓存列表 (用于STDP跨单元查找)"""
        self._units_map.clear()
        self._all_units = []

        for module in self.input_modules.values():
            for u in module.units:
                self._units_map[u.unit_id] = u
                self._all_units.append(u)
        for layer in self.think_layers:
            for u in layer._all_units_cache:
                self._units_map[u.unit_id] = u
                self._all_units.append(u)
        for u in self.output_module.units:
            self._units_map[u.unit_id] = u
            self._all_units.append(u)

    def enable_learning(self, enabled: bool = True) -> None:
        """启用/禁用 STDP 学习"""
        self.learning_enabled = enabled
        for u in self._all_units:
            u.stdp_enabled = enabled

    def _apply_stdp_to_all(self) -> None:
        """对所有发射过脉冲的单元应用 STDP 更新"""
        if not self.learning_enabled:
            return
        for unit in self._all_units:
            if unit.spike_times:
                unit.apply_stdp(time.time(), self._units_map)

    def get_stdp_stats(self) -> dict:
        """获取全局 STDP 统计"""
        total_ltp = sum(u.ltp_count for u in self._all_units)
        total_ltd = sum(u.ltd_count for u in self._all_units)
        total_weight_change = sum(u.total_weight_change for u in self._all_units)
        return {
            "learning_enabled": self.learning_enabled,
            "total_ltp": total_ltp,
            "total_ltd": total_ltd,
            "total_weight_change": float(total_weight_change),
            "avg_weight_change": float(total_weight_change / max(1, total_ltp + total_ltd))
        }

    def set_rhythm(self, think_phase: int = 80, inhibit_phase: int = 40) -> None:
        """设置节律参数"""
        self.think_phase = think_phase
        self.inhibit_phase = inhibit_phase
        self.cycle_length = think_phase + inhibit_phase

    def get_global_modulation(self) -> float:
        """根据当前节律相位计算全局调制强度"""
        if self.cycle_phase < self.think_phase:
            # 思考期: 调制从1.0逐渐降低到0.5
            progress = self.cycle_phase / self.think_phase
            return 1.0 - 0.5 * progress
        else:
            # 抑制期: 调制从0.5降低到0.1
            progress = (self.cycle_phase - self.think_phase) / self.inhibit_phase
            return 0.5 - 0.4 * progress

    def step(self, inputs: dict[str, Any]) -> list[SpikeMessage]:
        """
        完整网络单步执行 (多模态)

        Args:
            inputs: {模态名: 原始数据} 字典。可用的模态:
                "numeric"    → float 或 dim 维向量
                "text"       → str
                "timeseries" → 数值序列 (取最新差分)
                "image"      → 2D ndarray (灰度)
            只需给出本次存在的模态, 未提供的模态模块静默。
            传 {} 或 None 表示无外部输入 (自发活动)。

        Returns:
            输出模块发射的脉冲消息
        """
        self.total_steps += 1
        self.cycle_phase = (self.cycle_phase + 1) % self.cycle_length
        self.global_modulation = self.get_global_modulation()

        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise TypeError(
                "step() 需要 {模态: 数据} 字典, "
                f"例如 {{'numeric': 1.5, 'text': '...'}}; 收到 {type(inputs).__name__}"
            )

        # KV 注意力检索 (v0.5.0 接入主路径): 以上一拍输入为查询
        attn = self.kv_stack.retrieve(
            getattr(self, "_last_input", np.zeros(self.dim))
        )

        # 1. 各输入模块独立处理 (编码 + 单元步进)
        module_patterns = {}
        for name, raw in inputs.items():
            if name not in self.input_modules:
                raise KeyError(
                    f"未知输入模态 {name!r}, 已启用: {list(self.input_modules)}"
                )
            module = self.input_modules[name]
            module.step(raw, self.global_modulation, attn)
            pattern = module.get_pattern()
            if np.linalg.norm(pattern) > 0:
                module_patterns[name] = pattern

        # 2. 模态融合: 激活模块加权平均 → tanh → 思考层输入
        if module_patterns:
            fused = np.zeros(self.dim)
            for name, pattern in module_patterns.items():
                fused += self.modality_weights.get(name, 0.5) * pattern
            layer_input = np.tanh(fused)
        else:
            layer_input = np.zeros(self.dim)

        # 3. 思考层处理 (异步传播)
        for layer in self.think_layers:
            layer_spikes = layer.step(layer_input, self.kv_stack, self.global_modulation, self.training_mode)
            # 层间传播: 将当前层的脉冲聚合为下一层输入
            if layer_spikes:
                layer_input = np.zeros(self.dim)
                for sp in layer_spikes:
                    # 将脉冲值注入输入
                    idx = hash(sp.source_unit_id) % self.dim
                    layer_input[idx] += sp.payload.value * sp.payload.strength
                layer_input = np.tanh(layer_input)  # 归一化

        # 4. 输出模块处理
        output_spikes = self.output_module.step(layer_input, self.global_modulation, attn)
        self._last_input = layer_input

        # 5. KV堆更新 (仅在非训练模式下)
        if not self.training_mode:
            for i, unit in enumerate(self.output_module.units):
                if unit.spike_count > 0:
                    entry_id = f"output_{i}_{self.total_steps}"
                    self.kv_stack.push(entry_id, unit.state, unit.state)

        # 6. STDP 学习: 对所有发射过脉冲的单元应用权重更新
        if self.learning_enabled:
            self._apply_stdp_to_all()

        return output_spikes

    def get_network_stats(self) -> dict:
        """获取网络统计"""
        total_units = len(self._all_units)
        total_spikes = sum(u.spike_count for u in self._all_units)
        avg_fatigue = np.mean([u.fatigue for u in self._all_units]) if self._all_units else 0.0

        return {
            "depth": self.depth,
            "total_units": total_units,
            "total_spikes": total_spikes,
            "avg_fatigue": float(avg_fatigue),
            "cycle_phase": self.cycle_phase,
            "global_modulation": float(self.global_modulation),
            "input_modules": {
                name: {
                    "units": len(m.units),
                    "total_spikes": sum(u.spike_count for u in m.units),
                    "last_step_spikes": len(m.last_spikes),
                    "weight": self.modality_weights.get(name, 0.5),
                }
                for name, m in self.input_modules.items()
            },
            "kv_stats": self.kv_stack.get_stats(),
            "total_steps": self.total_steps,
            "stdp": self.get_stdp_stats()
        }

    def get_output_pattern(self) -> np.ndarray:
        """获取输出模块激活模式 (用于动作解码)"""
        return self.output_module.get_pattern()

    def get_think_layer_pattern(self) -> np.ndarray:
        """获取思考层聚合激活模式 (用于监督学习)"""
        pattern = np.zeros(self.dim)
        for layer in self.think_layers:
            if hasattr(layer, '_vec_state') and layer.N > 0:
                pattern += np.sum(np.abs(layer._vec_state) * (1 - layer._vec_fatigue[:, np.newaxis]), axis=0)
            else:
                for unit in layer._all_units_cache:
                    pattern += np.abs(unit.state) * (1 - unit.fatigue)
        # 归一化
        norm = np.linalg.norm(pattern)
        if norm > 0:
            pattern = pattern / norm
        return pattern

    def reset_state(self) -> None:
        """重置所有单元的内部状态 (用于训练时每个样本独立)"""
        for module in self.input_modules.values():
            module.reset_state()
        self.output_module.reset_state()
        for unit in self._all_units:
            unit.state = np.zeros(self.dim)
            unit.fatigue = 0.0
            unit.refractory = 0
        for layer in self.think_layers:
            if hasattr(layer, '_vec_state') and layer.N > 0:
                layer._vec_state[:] = 0.0
                layer._vec_fatigue[:] = 0.0
                layer._vec_refractory[:] = 0
        # 也重置全局状态
        self.cycle_phase = 0
        self.global_modulation = 1.0
        self.total_steps = 0
        # v0.8.6 修复: 同 CubeGPT.reset_state, 融合输入属于运行状态
        self._last_input = np.zeros(self.dim)

    def get_state_snapshot(self) -> dict:
        """获取所有单元状态的快照"""
        snapshot = {}
        for unit in self._all_units:
            snapshot[unit.unit_id] = {
                'state': unit.state.copy(),
                'fatigue': unit.fatigue,
                'refractory': unit.refractory
            }
        return snapshot

    def restore_state_snapshot(self, snapshot: dict) -> None:
        """从快照恢复单元状态"""
        for unit in self._all_units:
            if unit.unit_id in snapshot:
                s = snapshot[unit.unit_id]
                unit.state = s['state'].copy()
                unit.fatigue = s['fatigue']
                unit.refractory = s['refractory']


# ═══════════════════════════════════════════════════════════════
# 7. 工具函数
# ═══════════════════════════════════════════════════════════════

def calculate_scale(depth: int) -> dict:
    """计算给定分形深度的网络规模"""
    # 正确计算: 16 + 16^2 + ... + 16^(depth+1)
    base_units = sum(16 ** k for k in range(1, depth + 2))
    total_params = 16 * base_units
    return {
        "depth": depth,
        "base_units": base_units,
        "total_params": total_params,
        "description": f"深度{depth}: {base_units}基础单元 / {total_params//1000}K参数"
    }


# ═══════════════════════════════════════════════════════════════
# 8. 自测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("DistributedFormer v5.2 核心模块测试")
    print("=" * 60)

    # 测试规模计算
    for d in range(4):
        info = calculate_scale(d)
        print(f"  {info['description']}")

    print("\n" + "-" * 60)

    # 测试KV堆
    kv = KVStack(capacity=100, dim=16)
    for i in range(20):
        kv.push(f"test_{i}", np.random.randn(16), np.random.randn(16))
    results = kv.query(np.random.randn(16), top_k=3)
    print(f"KV堆测试: 20条目中查询top-3, 命中{len(results)}条")

    # 测试脉冲单元
    unit = SpikingUnit("test_unit")
    spike_count = 0
    for _ in range(100):
        spike = unit.step(np.random.randn(16), np.zeros(16), 1.0)
        if spike:
            spike_count += 1
    print(f"脉冲单元测试: 100步中发射{spike_count}次脉冲")

    # 测试完整网络
    print("\n" + "-" * 60)
    print("完整网络测试 (深度2, 1层)...")
    df = DistributedFormer(depth=2, dim=16, num_think_layers=1)

    # 模拟10步
    for step in range(10):
        output_spikes = df.step({"numeric": np.random.randn(16) * 0.5})
        print(f"  Step {step+1}: 输出层发射{len(output_spikes)}个脉冲, "
              f"调制={df.global_modulation:.2f}, 相位={df.cycle_phase}")

    stats = df.get_network_stats()
    print(f"\n网络统计: {stats['total_units']}单元, {stats['total_spikes']}脉冲, "
          f"疲劳={stats['avg_fatigue']:.3f}")
    print("KV堆: 利用率={:.1%}".format(stats['kv_stats']['utilization']))

    # 测试思考层模式
    think_pattern = df.get_think_layer_pattern()
    print(f"\n思考层激活模式: 范数={np.linalg.norm(think_pattern):.3f}")

    # 测试 STDP 学习
    print("\n" + "-" * 60)
    print("STDP 学习测试...")
    stdp_stats = df.get_stdp_stats()
    print(f"  LTP: {stdp_stats['total_ltp']}, LTD: {stdp_stats['total_ltd']}")
    print(f"  总权重变化: {stdp_stats['total_weight_change']:.4f}")
    print(f"  平均权重变化: {stdp_stats['avg_weight_change']:.6f}")

    # 禁用学习再运行5步对比
    print("\n  禁用 STDP 学习后运行5步...")
    df.enable_learning(False)
    for _ in range(5):
        df.step({"numeric": np.random.randn(16) * 0.5})
    stdp_stats2 = df.get_stdp_stats()
    print(f"  禁用后 LTP: {stdp_stats2['total_ltp']} (应不变)")
    print(f"  学习开关: {stdp_stats2['learning_enabled']}")

    print("\n" + "=" * 60)
    print("核心模块测试通过!")
    print("=" * 60)
