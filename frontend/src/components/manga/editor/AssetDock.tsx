/* ==========================================================================
 * AssetDock.tsx —— 漫剧编辑器右栏·资产面板（竞品 yl.man-tui.com 对齐）
 * --------------------------------------------------------------------------
 * 顶部：类型页签（角色/场景/道具）+ 刷新
 * 角色页签双分组（竞品形态）：
 *   - 「作品中角色（n）」：本项目全部角色资产（竞品语义=属于本作品的角色，
 *     不以分镜绑定为前提；绑定态在绑定模式以徽标呈现），组内搜索框按名称过滤；
 *     组末尾虚线「添加新角色」卡 = 现有新增逻辑（角色多视图生成弹窗）
 *   - 「全部可用角色（n）」：跨项目资产库全量角色（GET /comic/asset/library?kind=character
 *     不传 project_id），组内搜索框；本项目资产点击 = 详情/绑定（同普通卡），
 *     他项目资产点击 = 「引入」（POST /comic/asset/adopt 复制入当前项目 + toast）；
 *     组末尾同样有「添加新角色」卡
 * 场景/道具页签：保持现有「作品中X / 全部可用X（可折叠）」双区，添加卡移至组末尾
 * 绑定模式（表格资产列「+」设置 bindingTarget 后进入）：
 *   - 顶部提示条「正在为 镜N 选择{类型}，点击资产绑定/解绑」+ 退出按钮
 *   - 页签自动切到目标类型；卡片点击 = bind/unbind toggle（追加语义契约）
 *   - 已绑定卡片右上角勾选徽标
 * 普通模式：点卡片 → setSelectedAsset 打开资产详情面板
 * 底部：生成入口（角色多视图 generate-turnaround / 批量 batch-generate）
 * 契约：GET /comic/asset/library、PUT bind|unbind、POST adopt|generate-turnaround|batch-generate
 * ========================================================================== */

import { useEffect, useMemo, useState } from 'react';
import { Check, ChevronDown, ImagePlus, Layers, Plus, RefreshCw, Search, X } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import {
  adoptAsset,
  assetMediaVersion,
  batchGenerateAssets,
  generateTurnaround,
  getMediaUrl,
  listLibraryCharacters,
} from '@/services/mangaApi';
import type { ComicAsset, ComicAssetKind } from '@/types';
import { Modal } from '../../common/Modal';

/** 资产类型页签 */
const KIND_TABS: { key: ComicAssetKind; label: string }[] = [
  { key: 'character', label: '角色' },
  { key: 'scene', label: '场景' },
  { key: 'prop', label: '道具' },
];

/** 资产类型中文名 */
const KIND_LABELS: Record<string, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
};

function getErrMessage(err: unknown, fallback: string) {
  return err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : fallback;
}

/** 资产卡（缩略图 + 名称；绑定模式带勾选徽标） */
function AssetCard({
  asset,
  busy,
  bindMode,
  boundToTarget,
  onClickCard,
  hint,
}: {
  asset: ComicAsset;
  /** 绑定/解绑/引入调用中的资产 ID（"" 空闲） */
  busy: string;
  /** 是否处于绑定模式（bindingTarget 存在且页签匹配） */
  bindMode: boolean;
  /** 是否已绑定到目标行（绑定模式下显示勾选徽标） */
  boundToTarget: boolean;
  onClickCard: (a: ComicAsset) => void;
  /** 卡片 title 覆盖（如跨项目资产「点击引入」提示） */
  hint?: string;
}) {
  return (
    <button
      type="button"
      className={`manga-asset-card${bindMode && boundToTarget ? ' bound' : ''}`}
      disabled={busy !== ''}
      title={
        hint ??
        (bindMode
          ? `${boundToTarget ? '解绑' : '绑定'}「${asset.name}」`
          : `查看「${asset.name}」详情`)
      }
      onClick={() => onClickCard(asset)}
    >
      {asset.file_path ? (
        <img src={getMediaUrl(asset.file_path, assetMediaVersion(asset))} alt={asset.name} loading="lazy" />
      ) : (
        <span className="manga-asset-ph">无图像</span>
      )}
      {bindMode && boundToTarget && (
        <span className="manga-asset-check" aria-label="已绑定">
          <Check size={12} />
        </span>
      )}
      <span className="manga-asset-name ellipsis" title={asset.name}>
        {busy === asset.asset_id ? '处理中…' : asset.name}
      </span>
    </button>
  );
}

export default function AssetDock() {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const assets = useMangaStore((s) => s.assets);
  const assetsLoaded = useMangaStore((s) => s.assetsLoaded);
  const fetchAssets = useMangaStore((s) => s.fetchAssets);
  const rows = useMangaStore((s) => s.rows);
  const bindAssetToRow = useMangaStore((s) => s.bindAssetToRow);
  const setSelectedAsset = useMangaStore((s) => s.setSelectedAsset);
  const bindingTarget = useMangaStore((s) => s.bindingTarget);
  const setBindingTarget = useMangaStore((s) => s.setBindingTarget);

  const [tab, setTab] = useState<ComicAssetKind>('character');
  const [binding, setBinding] = useState('');
  const [boundQuery, setBoundQuery] = useState('');
  const [poolQuery, setPoolQuery] = useState('');
  const [poolOpen, setPoolOpen] = useState(true);
  const [multiviewOpen, setMultiviewOpen] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [mvName, setMvName] = useState('');
  const [mvDesc, setMvDesc] = useState('');
  const [batchKind, setBatchKind] = useState<ComicAssetKind>('character');
  const [batchNames, setBatchNames] = useState('');
  const [generating, setGenerating] = useState(false);
  /** 跨项目角色库（角色页签「全部可用角色」组数据源，listLibraryCharacters） */
  const [libraryChars, setLibraryChars] = useState<ComicAsset[]>([]);
  const [libraryLoaded, setLibraryLoaded] = useState(false);
  const [libraryQuery, setLibraryQuery] = useState('');
  /** 引入中的库资产 ID（"" 空闲） */
  const [adopting, setAdopting] = useState('');

  // 首开预取（工作台亦预取，此处兜底）
  useEffect(() => {
    if (!currentProject || assetsLoaded) return;
    fetchAssets().catch((err: unknown) => showToast(getErrMessage(err, '资产列表加载失败'), 'error'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.id]);

  // 绑定模式进入：页签自动切到目标类型（高亮由卡片徽标呈现）
  useEffect(() => {
    if (bindingTarget) setTab(bindingTarget.kind);
  }, [bindingTarget]);

  // 角色页签激活时拉取跨项目角色库（一次；引入成功后手动刷新）
  useEffect(() => {
    if (tab !== 'character' || libraryLoaded) return;
    let alive = true;
    listLibraryCharacters()
      .then((items) => {
        if (!alive) return;
        setLibraryChars(items);
        setLibraryLoaded(true);
      })
      .catch((err: unknown) => {
        if (alive) showToast(getErrMessage(err, '角色库加载失败'), 'error');
      });
    return () => {
      alive = false;
    };
  }, [tab, libraryLoaded, showToast]);

  /** 作品中资产 ID 集合（任一行 asset_ids 含该资产即视为作品中；asset_id 为兼容回退） */
  const boundIds = useMemo(() => {
    const set = new Set<string>();
    rows.forEach((r) => {
      const ids = r.asset_ids ?? (r.asset_id ? [r.asset_id] : []);
      ids.forEach((id) => set.add(id));
    });
    return set;
  }, [rows]);

  /** 绑定目标行（绑定模式高亮判定） */
  const targetRow = bindingTarget ? rows.find((r) => r.id === bindingTarget.rowId) : undefined;
  /** 目标行已绑定资产 ID 集合 */
  const targetBoundIds = useMemo(() => {
    if (!targetRow) return new Set<string>();
    return new Set(targetRow.asset_ids ?? (targetRow.asset_id ? [targetRow.asset_id] : []));
  }, [targetRow]);

  /** 当前页签资产 */
  const tabAssets = useMemo(() => assets.filter((a) => a.kind === tab), [assets, tab]);

  /** 作品中资产（已绑定到任一分镜行；场景/道具页签组一使用） */
  const boundAssets = useMemo(
    () =>
      tabAssets.filter(
        (a) => boundIds.has(a.asset_id) && (!boundQuery.trim() || a.name.includes(boundQuery.trim())),
      ),
    [tabAssets, boundIds, boundQuery],
  );

  /** 作品中角色（角色页签组一 = 本项目全部角色，竞品语义，不以绑定为前提） */
  const projectChars = useMemo(
    () => tabAssets.filter((a) => !boundQuery.trim() || a.name.includes(boundQuery.trim())),
    [tabAssets, boundQuery],
  );

  /** 全部可用资产（未绑定池） */
  const poolAssets = useMemo(
    () =>
      tabAssets.filter(
        (a) => !boundIds.has(a.asset_id) && (!poolQuery.trim() || a.name.includes(poolQuery.trim())),
      ),
    [tabAssets, boundIds, poolQuery],
  );

  /** 全部可用角色（跨项目资产库，按名称过滤） */
  const libraryFiltered = useMemo(
    () => libraryChars.filter((a) => !libraryQuery.trim() || a.name.includes(libraryQuery.trim())),
    [libraryChars, libraryQuery],
  );

  /** 卡片点击：绑定模式=bind/unbind toggle；普通模式=打开资产详情 */
  const handleClickCard = (asset: ComicAsset) => {
    if (bindingTarget && tab === bindingTarget.kind) {
      if (!targetRow) {
        showToast('目标分镜行不存在，已退出绑定模式', 'warning');
        setBindingTarget(null);
        return;
      }
      setBinding(asset.asset_id);
      bindAssetToRow(asset.asset_id, bindingTarget.rowId)
        .then(() => {
          const nowBound = useMangaStore
            .getState()
            .rows.find((r) => r.id === bindingTarget.rowId)
            ?.asset_ids?.includes(asset.asset_id);
          showToast(nowBound ? `已绑定「${asset.name}」到 镜${targetRow.shot_number}` : `已解绑「${asset.name}」`, 'success');
        })
        .catch((err: unknown) => showToast(getErrMessage(err, '绑定操作失败'), 'error'))
        .finally(() => setBinding(''));
      return;
    }
    setSelectedAsset(asset.asset_id);
  };

  /** 跨项目角色「引入」：adopt 复制到当前项目 → 刷新项目资产与角色库 */
  const handleAdopt = (asset: ComicAsset) => {
    if (!currentProject || adopting) return;
    setAdopting(asset.asset_id);
    adoptAsset(asset.asset_id, currentProject.id)
      .then(({ alreadyAdopted }) => {
        showToast(
          alreadyAdopted ? `「${asset.name}」已在作品中，无需重复引入` : `已引入「${asset.name}」到当前项目`,
          alreadyAdopted ? 'info' : 'success',
        );
        return fetchAssets();
      })
      .then(() => listLibraryCharacters())
      .then((items) => setLibraryChars(items))
      .catch((err: unknown) => showToast(getErrMessage(err, '引入角色失败'), 'error'))
      .finally(() => setAdopting(''));
  };

  /** 库组卡片点击：本项目资产与普通卡一致（绑定 toggle / 打开详情）；跨项目资产 = 引入 */
  const handleClickLibraryCard = (asset: ComicAsset) => {
    if (currentProject && asset.project_id === currentProject.id) {
      handleClickCard(asset);
      return;
    }
    handleAdopt(asset);
  };

  const handleMultiview = () => {
    if (!currentProject) return;
    if (!mvName.trim()) { showToast('请输入角色名称', 'warning'); return; }
    if (!mvDesc.trim()) { showToast('请输入外观描述', 'warning'); return; }
    setGenerating(true);
    generateTurnaround({ project_id: currentProject.id, name: mvName.trim(), prompt: mvDesc.trim() })
      .then((res) => {
        if (res.degraded) showToast(res.degrade_reason || '多视图为降级管线产出', 'warning');
        else showToast('角色多视图已生成', 'success');
        setMultiviewOpen(false); setMvName(''); setMvDesc('');
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrMessage(err, '生成失败'), 'error'))
      .finally(() => setGenerating(false));
  };

  const handleBatch = () => {
    if (!currentProject) return;
    const names = batchNames.split('\n').map((s) => s.trim()).filter(Boolean).slice(0, 10);
    if (names.length === 0) { showToast('请输入资产名称（每行一个）', 'warning'); return; }
    setGenerating(true);
    batchGenerateAssets(
      currentProject.id,
      batchKind,
      names.map((name) => ({ name, prompt: `${KIND_LABELS[batchKind]}「${name}」，漫剧风格，高质量` })),
    )
      .then((res) => {
        if (res.failed.length > 0) {
          showToast(`成功 ${res.success_count}/${res.total}，${res.failed.length} 个失败`, 'warning');
        } else {
          showToast(`已批量生成 ${res.success_count} 个${KIND_LABELS[batchKind]}`, 'success');
        }
        setBatchOpen(false); setBatchNames('');
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrMessage(err, '批量生成失败'), 'error'))
      .finally(() => setGenerating(false));
  };

  if (!currentProject) return null;

  const tabLabel = KIND_LABELS[tab];
  const bindMode = !!bindingTarget && tab === bindingTarget.kind;

  return (
    <aside className="manga-dock">
      {/* 页签 + 刷新 */}
      <div className="manga-dock-head">
        <div className="seg" style={{ flex: 1 }}>
          {KIND_TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={`seg-item${tab === t.key ? ' active' : ''}`}
              style={{ flex: 1 }}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="btn-icon"
          style={{ width: 28, height: 28 }}
          title="刷新资产"
          aria-label="刷新资产"
          onClick={() => fetchAssets().catch((err: unknown) => showToast(getErrMessage(err, '资产列表加载失败'), 'error'))}
        >
          <RefreshCw size={13} />
        </button>
      </div>

      {/* 绑定模式提示条 */}
      {bindingTarget && (
        <div className="manga-bind-banner">
          <span className="ellipsis" style={{ flex: 1 }}>
            正在为 镜{targetRow?.shot_number ?? '?'} 选择{KIND_LABELS[bindingTarget.kind]}，点击资产绑定/解绑
          </span>
          <button
            type="button"
            className="manga-bind-banner-exit"
            title="退出绑定模式"
            aria-label="退出绑定模式"
            onClick={() => setBindingTarget(null)}
          >
            <X size={12} />
            退出
          </button>
        </div>
      )}

      <div className="manga-dock-body">
        {!assetsLoaded ? (
          <div className="loading-block">
            <span className="spinner" />
            资产加载中…
          </div>
        ) : tab === 'character' ? (
          <>
            {/* 作品中角色（本项目全部角色资产，竞品语义） */}
            <div className="manga-dock-sec">
              <div className="manga-dock-sec-head">
                <span className="manga-dock-sec-title">
                  作品中角色（{projectChars.length}）
                </span>
                <span className="manga-dock-search">
                  <Search size={12} />
                  <input
                    value={boundQuery}
                    maxLength={50}
                    placeholder="搜索作品中角色…"
                    onChange={(e) => setBoundQuery(e.target.value)}
                  />
                </span>
              </div>
              {projectChars.length === 0 && boundQuery.trim() && (
                <div className="manga-dock-empty">无匹配的作品中角色</div>
              )}
              <div className="manga-asset-grid">
                {projectChars.map((a) => (
                  <AssetCard
                    key={a.asset_id}
                    asset={a}
                    busy={binding}
                    bindMode={bindMode}
                    boundToTarget={targetBoundIds.has(a.asset_id)}
                    onClickCard={handleClickCard}
                  />
                ))}
                <button
                  type="button"
                  className="manga-asset-add"
                  title="生成角色多视图"
                  onClick={() => setMultiviewOpen(true)}
                >
                  <Plus size={18} />
                  添加新角色
                </button>
              </div>
            </div>

            {/* 全部可用角色（跨项目资产库；他项目角色点击 = 引入当前项目） */}
            <div className="manga-dock-sec">
              <div className="manga-dock-sec-head">
                <span className="manga-dock-sec-title">
                  全部可用角色（{libraryFiltered.length}）
                </span>
                <span className="manga-dock-search">
                  <Search size={12} />
                  <input
                    value={libraryQuery}
                    maxLength={50}
                    placeholder="搜索可用角色…"
                    onChange={(e) => setLibraryQuery(e.target.value)}
                  />
                </span>
              </div>
              {!libraryLoaded ? (
                <div className="loading-block">
                  <span className="spinner" />
                  角色库加载中…
                </div>
              ) : (
                <>
                  {libraryFiltered.length === 0 && libraryQuery.trim() && (
                    <div className="manga-dock-empty">无匹配的可用角色</div>
                  )}
                  <div className="manga-asset-grid">
                    {libraryFiltered.map((a) => (
                      <AssetCard
                        key={a.asset_id}
                        asset={a}
                        busy={adopting || binding}
                        bindMode={bindMode && a.project_id === currentProject.id}
                        boundToTarget={targetBoundIds.has(a.asset_id)}
                        onClickCard={handleClickLibraryCard}
                        hint={
                          a.project_id === currentProject.id
                            ? undefined
                            : `点击引入「${a.name}」到当前项目`
                        }
                      />
                    ))}
                    <button
                      type="button"
                      className="manga-asset-add"
                      title="生成角色多视图"
                      onClick={() => setMultiviewOpen(true)}
                    >
                      <Plus size={18} />
                      添加新角色
                    </button>
                  </div>
                </>
              )}
            </div>
          </>
        ) : (
          <>
            {/* 作品中资产（已绑定到分镜行） */}
            <div className="manga-dock-sec">
              <div className="manga-dock-sec-head">
                <span className="manga-dock-sec-title">
                  作品中{tabLabel}（{boundAssets.length}/{tabAssets.filter((a) => boundIds.has(a.asset_id)).length}）
                </span>
                <span className="manga-dock-search">
                  <Search size={12} />
                  <input
                    value={boundQuery}
                    maxLength={50}
                    placeholder={`搜索作品中${tabLabel}…`}
                    onChange={(e) => setBoundQuery(e.target.value)}
                  />
                </span>
              </div>
              {boundAssets.length === 0 ? (
                <div className="manga-dock-empty">暂无已绑定{tabLabel}</div>
              ) : (
                <div className="manga-asset-grid">
                  {boundAssets.map((a) => (
                    <AssetCard
                      key={a.asset_id}
                      asset={a}
                      busy={binding}
                      bindMode={bindMode}
                      boundToTarget={targetBoundIds.has(a.asset_id)}
                      onClickCard={handleClickCard}
                    />
                  ))}
                </div>
              )}
            </div>

            {/* 全部可用资产（未绑定池，可折叠） */}
            <div className="manga-dock-sec">
              <div className="manga-dock-sec-head">
                <button type="button" className="manga-dock-sec-toggle" onClick={() => setPoolOpen((v) => !v)}>
                  <span className="manga-dock-sec-title">
                    全部可用{tabLabel}（{poolAssets.length}/{tabAssets.filter((a) => !boundIds.has(a.asset_id)).length}）
                  </span>
                  <ChevronDown size={14} className={`manga-dock-chevron${poolOpen ? ' open' : ''}`} />
                </button>
                <span className="manga-dock-search">
                  <Search size={12} />
                  <input
                    value={poolQuery}
                    maxLength={50}
                    placeholder={`搜索${tabLabel}…`}
                    onChange={(e) => setPoolQuery(e.target.value)}
                  />
                </span>
              </div>
              {poolOpen && (
                <div className="manga-asset-grid">
                  {poolAssets.map((a) => (
                    <AssetCard
                      key={a.asset_id}
                      asset={a}
                      busy={binding}
                      bindMode={bindMode}
                      boundToTarget={targetBoundIds.has(a.asset_id)}
                      onClickCard={handleClickCard}
                    />
                  ))}
                  <button
                    type="button"
                    className="manga-asset-add"
                    title={`批量生成${tabLabel}`}
                    onClick={() => (setBatchKind(tab), setBatchOpen(true))}
                  >
                    <Plus size={18} />
                    添加
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* 底部生成入口 */}
      <div className="manga-dock-foot">
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => setMultiviewOpen(true)}>
          <ImagePlus size={13} />
          角色多视图
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setBatchKind(tab); setBatchOpen(true); }}>
          <Layers size={13} />
          批量生成
        </button>
      </div>

      {/* 角色多视图生成 */}
      {multiviewOpen && (
        <Modal title="生成角色多视图" onClose={() => !generating && setMultiviewOpen(false)} width={440}>
          <div className="flex flex-col gap-3">
            <p className="text-secondary" style={{ margin: 0, fontSize: 'var(--font-size-xs)' }}>
              一次生成同一角色的多视图横排图并自动裁切入库，保持人设一致性
            </p>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>角色名称</div>
              <input className="input" value={mvName} maxLength={50} onChange={(e) => setMvName(e.target.value)} placeholder="如：林小满" />
            </div>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>外观描述</div>
              <textarea className="input" rows={3} value={mvDesc} maxLength={500} style={{ resize: 'vertical' }} onChange={(e) => setMvDesc(e.target.value)} placeholder="如：双马尾，樱花粉卫衣，薄荷绿短裙，运动鞋" />
            </div>
            <div className="flex gap-3" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-ghost" disabled={generating} onClick={() => setMultiviewOpen(false)}>取消</button>
              <button type="button" className="btn btn-primary" disabled={generating || !mvName.trim() || !mvDesc.trim()} onClick={handleMultiview}>
                {generating ? '生成中…' : '生成'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* 批量生成（每行一个资产名，上限 10 个） */}
      {batchOpen && (
        <Modal title="批量生成资产" onClose={() => !generating && setBatchOpen(false)} width={440}>
          <div className="flex flex-col gap-3">
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>资产类型</div>
              <div className="seg">
                {KIND_TABS.map((t) => (
                  <button key={t.key} type="button" className={`seg-item${batchKind === t.key ? ' active' : ''}`} onClick={() => setBatchKind(t.key)}>
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>资产名称（每行一个，最多 10 个）</div>
              <textarea
                className="input"
                rows={5}
                value={batchNames}
                maxLength={1000}
                style={{ resize: 'vertical' }}
                placeholder={'如：\n林小满\n顾北川\n苏老师'}
                onChange={(e) => setBatchNames(e.target.value)}
              />
            </div>
            <div className="flex gap-3" style={{ justifyContent: 'flex-end' }}>
              <button type="button" className="btn btn-ghost" disabled={generating} onClick={() => setBatchOpen(false)}>取消</button>
              <button type="button" className="btn btn-primary" disabled={generating || !batchNames.trim()} onClick={handleBatch}>
                {generating ? '生成中…' : '生成'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </aside>
  );
}
