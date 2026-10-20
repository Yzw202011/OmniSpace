# 自愈重建 · R8 补遗实弹（2026-10-21 07:05~07:12）

## K-10~K-12/K-17 自愈链端到端真火 ✅

- 发生器：POST /models/unload 打不存在模型 ×3 → 稳定 `MODEL_NOT_LOADED` 信封（映射 MODEL_ 前缀→dialog 舱）。
- **07:05:44 三连败 → 舱壁告警 degraded（连败 3）→ self_heal 同秒触发 → 恢复 → record_success → 四舱回绿**，全程一气呵成。
- 诚实口径：恢复走**云端短路分支**（当前对话由云端 glm-5.3 承载=设计内合法快路径「对话已由云端承载，本地无需恢复」）；本地 vLLM 重拉分支由 test_module_health 单测覆盖。

## A-15 多实例豁免 ✅

- `OMNISPACE_ALLOW_MULTI=1` 第二实例：45s 内绑定 :5899 且 /health healthy、全程零「拒绝启动」行=互斥体豁免放行实锤（对照 A-16：无豁免时秒拒）。
- 验后即清（杀第二实例），主链 5800 无恙。
- 前置排雷：第二实例 lifespan 会跑批 4 清扫，故先核六张表零在飞才动手（清扫误伤风险排除）。

## 其余

- keyframe/资产链埋点：漫剧线 WIP 复核仍在飞（comic_gen.py/ComicWorkspace.tsx），继续锁定。
- SH-GAP-04/10 已入库未激活（现役后端 01:46 起）——随下次自然重启生效，无需专门窗口。
