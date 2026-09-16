/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 底部状态栏（规格 §6.1.4：28px 通栏，字段实时）
 * --------------------------------------------------------------------------
 * 十个字段（规格 §6.1.4，左 → 右）：
 *   CPU 使用率 / GPU 使用率 / 显存占用 / 内存占用   —— 硬件实时 WS 推送（2s）
 *   协同状态                                        —— /hardware/synergy 2s 轮询（store 内置）
 *   AV1 编码状态                                    —— /style/status 5s 轮询（编码服务遥测）
 *   知识库条目数 / LoRA 版本 / 当前学习主题及进度    —— learn 端点 5s 轮询
 *   联网状态                                        —— navigator.onLine + online/offline 事件
 * 诚实门控：任一数据源未就绪时对应字段显示 --，绝不伪造。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { useHardwareStore } from '@/stores/useHardwareStore';
import * as learningApi from '@/services/learningApi';
import { listLearnModels } from '@/services/learnApi';
import { getStyleStatus } from '@/services/styleApi';
import { LEARN_SESSION_STATUS_LABELS } from '@/constants/statusLabels';

/** learn 端点轮询周期（ms） */
const LEARN_POLL_MS = 5000;

/** 单个字段（.sb-item 契约：标签 + 值；label 为空时仅渲染值） */
function SbItem({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  /** ok=绿 / warn=黄 / 默认主文字色 */
  tone?: 'ok' | 'warn';
  title?: string;
}) {
  return (
    <span className="sb-item" title={title ?? (label ? `${label}:${value}` : value)}>
      {label !== '' && <span className="sb-label">{label}</span>}
      <span className={`sb-value${tone ? ` ${tone}` : ''}`}>{value}</span>
    </span>
  );
}

/** 字段分隔竖线 */
function Sep() {
  return <span className="sb-sep" aria-hidden="true" />;
}

/** MB → GB 一位小数（无数据 --） */
function gb(mb: number | undefined | null): string {
  return mb === null || mb === undefined ? '--' : (mb / 1024).toFixed(1);
}

/** 百分比文本（无数据 --） */
function pct(v: number | undefined | null): string {
  return v === null || v === undefined ? '--' : `${Math.round(v)}%`;
}

/** 学习进度摘要（规格示例：进行中(短剧编剧技巧 12/20页)） */
function learningSummary(s: learningApi.LearnSessionStatus | null): string {
  if (!s || s.status === 'idle' || !s.topic) {
    return LEARN_SESSION_STATUS_LABELS.idle;
  }
  const pages = s.max_pages ? ` ${s.pages_visited ?? 0}/${s.max_pages}页` : '';
  const statusText = LEARN_SESSION_STATUS_LABELS[s.status] ?? s.status;
  return `${statusText}(${s.topic}${pages})`;
}

/** 订阅联网状态（online/offline 事件驱动 + 初始快照） */
function useOnline(): boolean {
  const [online, setOnline] = useState<boolean>(() =>
    typeof navigator === 'undefined' ? true : navigator.onLine,
  );
  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener('online', on);
    window.addEventListener('offline', off);
    return () => {
      window.removeEventListener('online', on);
      window.removeEventListener('offline', off);
    };
  }, []);
  return online;
}

/** 底部状态栏（挂载于 AppShell 底部，通栏） */
export function BottomStatusBar() {
  // 硬件实时（WS 2s 推送，store 全局单例）
  const realtime = useHardwareStore((s) => s.realtime);
  const wsStatus = useHardwareStore((s) => s.wsStatus);
  const synergyModeText = useHardwareStore((s) => s.synergyModeText);

  const online = useOnline();

  // learn 端点快照（5s 轮询）
  const [knowledgeCount, setKnowledgeCount] = useState<number | null>(null);
  const [loraVersion, setLoraVersion] = useState<string>('--');
  const [session, setSession] = useState<learningApi.LearnSessionStatus | null>(null);
  // AV1 编码器遥测（/style/status 5s 轮询；null=未获取到）
  const [av1Encoder, setAv1Encoder] = useState<'nvenc' | 'svt' | 'none' | 'off' | null>(null);
  const [encoderHw, setEncoderHw] = useState<string>('');

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      // 知识库条目数
      try {
        const stats = await learningApi.getKnowledgeStats();
        if (!cancelled) {
          setKnowledgeCount(stats.total ?? null);
        }
      } catch {
        /* 后端未就绪保持上次快照 */
      }
      // LoRA 版本：取学习模型清单中最新一个可训练基座/LoRA 名
      try {
        const models = await listLearnModels();
        if (!cancelled) {
          const ready = models.filter((m) => m.ready);
          setLoraVersion(ready.length > 0 ? ready[ready.length - 1].name : '--');
        }
      } catch {
        /* 同上 */
      }
      // 当前学习主题及进度
      try {
        const s = await learningApi.getSessionStatus();
        if (!cancelled) {
          setSession(s);
        }
      } catch {
        /* 同上 */
      }
      // AV1 编码器状态（编码服务遥测）
      try {
        const st = await getStyleStatus();
        if (!cancelled) {
          setAv1Encoder(st.av1_encoder ?? null);
          setEncoderHw(st.encoder_hw ?? '');
        }
      } catch {
        /* 同上 */
      }
    }

    poll();
    const timer = setInterval(poll, LEARN_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const wsConnected = wsStatus === 'open';
  const learnText = learningSummary(session);

  return (
    <footer className="statusbar" aria-label="系统状态栏">
      {/* WS 连接灯（COM-011：断线重连可见） */}
      <span className="sb-item" title={wsConnected ? '实时通道已连接' : '实时通道重连中'}>
        <span className={`ws-dot ${wsConnected ? 'on' : 'off'}`} />
      </span>

      <SbItem label="CPU" value={pct(realtime?.cpu_percent)} />
      <Sep />
      <SbItem label="GPU" value={pct(realtime?.gpu_util_pct)} />
      <Sep />
      <SbItem
        label="显存"
        value={
          realtime?.vram_total_mb
            ? `${gb(realtime.vram_used_mb)}/${gb(realtime.vram_total_mb)}GB`
            : '--'
        }
      />
      <Sep />
      <SbItem
        label="内存"
        value={
          realtime?.ram_total_mb
            ? `${gb(realtime.ram_used_mb)}/${gb(realtime.ram_total_mb)}GB`
            : '--'
        }
      />
      <Sep />
      <SbItem label="协同" value={synergyModeText} />
      <Sep />
      {/* AV1 编码状态：/style/status 编码服务遥测（5s 轮询）。
          nvenc=硬编（绿）/ svt=软编（绿）/ none=无 AV1 编码器将降级 H.264（黄）/
          off=ffmpeg 缺失（黄）/ null=遥测未获取（--） */}
      <SbItem
        label="AV1"
        value={
          av1Encoder === null
            ? '--'
            : av1Encoder === 'nvenc'
              ? 'NVENC'
              : av1Encoder === 'svt'
                ? 'SVT-AV1'
                : av1Encoder === 'none'
                  ? 'H.264'
                  : '不可用'
        }
        tone={av1Encoder === 'nvenc' || av1Encoder === 'svt' ? 'ok' : av1Encoder === null ? undefined : 'warn'}
        title={
          av1Encoder === 'nvenc'
            ? `AV1 硬件编码可用（NVENC${encoderHw ? `，${encoderHw}` : ''}）`
            : av1Encoder === 'svt'
              ? `AV1 软件编码可用（SVT-AV1${encoderHw ? `，${encoderHw}` : ''}）`
              : av1Encoder === 'none'
                ? 'FFmpeg 无 AV1 编码器，视频导出将降级 H.264'
                : av1Encoder === 'off'
                  ? 'FFmpeg 缺失，视频编码不可用'
                  : 'AV1 编码状态遥测未获取'
        }
      />
      <Sep />
      <SbItem
        label="知识"
        value={knowledgeCount === null ? '--' : `${knowledgeCount.toLocaleString()}条`}
      />
      <Sep />
      <SbItem label="LoRA" value={loraVersion} title={`LoRA 版本：${loraVersion}`} />

      {/* 右侧：联网状态 + 学习进度 */}
      <span className="sb-right">
        <SbItem
          label=""
          value={online ? '已联网' : '离线'}
          tone={online ? 'ok' : 'warn'}
          title={online ? '网络连接正常' : '网络已断开'}
        />
        <Sep />
        <SbItem label="学习" value={learnText} title={`学习：${learnText}`} />
      </span>
    </footer>
  );
}

export default BottomStatusBar;
// 本项目仅供学习使用，商业授权请+Q 3559331368
