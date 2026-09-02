/* ==========================================================================
 * ReadinessBanner.tsx —— 模型就绪总检横幅（体验流 #3：绿灯/缺件指路）
 * --------------------------------------------------------------------------
 * 数据源 GET /models/readiness（按 models_manifest 契约盘上核对）：
 *   - 模块绿＝该功能可用；灰＝缺大模型包；
 *   - 缺件指路：把 models 文件夹拖到启动图标上（或放入安装目录 models\），
 *     重启后引擎自动接线——与 boot 拖入识别/挂接器同一条链路。
 * 这是「拖入→启动→首启激活→即用」最后一步"完美使用"的验收门面。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { CheckCircle2, CircleDashed, PackageOpen } from 'lucide-react';
import { fetchModelReadiness, type ModelReadiness } from '@/services/modelApi';

export function ReadinessBanner() {
  const [data, setData] = useState<ModelReadiness | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchModelReadiness()
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, []);

  if (failed || !data || data.modules.length === 0) return null;

  const grayModules = data.modules.filter((m) => !m.ready);
  const optionalMissing = data.modules.filter(
    (m) => m.ready && m.missing.length > 0);

  return (
    <section className="mm-reco" aria-label="模型就绪总检">
      <div className="mm-reco-head">
        <span className="mm-reco-title">
          <PackageOpen size={15} aria-hidden="true" /> 模型就绪总检
        </span>
        {data.all_ready ? (
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-success)' }}>
            全部功能就绪{optionalMissing.length > 0 && `（${data.missing_count} 件可选模型未装入）`}
          </span>
        ) : (
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-warning)' }}>
            {grayModules.length} 项功能待解锁
          </span>
        )}
      </div>
      <div className="mm-reco-chips" style={{ flexWrap: 'wrap', gap: 'var(--space-2)' }}>
        {data.modules.map((m) => (
          <span
            key={m.key}
            title={
              m.ready
                ? (m.missing.length > 0
                    ? `就绪；未装入：${m.missing.join('、')}`
                    : '就绪')
                : `缺模型：${m.missing.join('、') || '清单无此类模型'}`
            }
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '4px 10px', borderRadius: 999,
              fontSize: 'var(--font-size-sm)',
              border: `1px solid ${m.ready ? 'var(--color-success)' : 'var(--color-border)'}`,
              color: m.ready ? 'var(--color-text-primary)' : 'var(--color-text-secondary)',
            }}
          >
            {m.ready
              ? <CheckCircle2 size={14} style={{ color: 'var(--color-success)' }} aria-hidden="true" />
              : <CircleDashed size={14} aria-hidden="true" />}
            {m.label}
            {m.ready && m.missing.length > 0 && (
              <span style={{ opacity: 0.65 }}>·缺{m.missing.length}</span>
            )}
          </span>
        ))}
      </div>
      {!data.all_ready && (
        <p style={{ marginTop: 'var(--space-2)', marginBottom: 0,
                    fontSize: 'var(--font-size-sm)', color: 'var(--color-text-secondary)' }}>
          把 models 大模型文件夹拖到启动图标上（或放入安装目录 models\ 后重启），
          上述功能自动解锁；知识库/语音等内置能力不受影响。
        </p>
      )}
    </section>
  );
}
