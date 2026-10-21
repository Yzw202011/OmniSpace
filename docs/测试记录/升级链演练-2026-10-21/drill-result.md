# 升级链重建 · 演练实弹结果（2026-10-21 19:58~20:27）

> 用户令「跑演练」。全流程：优雅退出开发版 → drill_upgrade.py 六点 → 清场 → 拉回开发版（20:27 健康 2.5.1+dev）。

## 六点结果（5✅ + 1 项负向在正确层闭环 + 1 项待查）

| 点 | 结果 | 证据 |
|---|---|---|
| ①出签名 .upg | ✅ | OmniSpace-upgrade-2.5.0-to-2.5.1.upg，33.3MB，替换 272/删除 24，**contains_migration=True**（Cython 重编译哈希漂移触发保守启发） |
| ②导入+验签 | ✅ | 基座 /upgrade/import success；**篡改包在 import 层放行=设计**（import 只验清单签名，payload 完整性归启动层深验） |
| ②负向（启动层） | ✅ **补测闭环** | 篡改包 upgrade_start → `UPGRADE_PACKAGE_CORRUPT｜包体损坏（CRC 校验失败: payload/src/api/manga/keyframe....pyd）`——精确指出损坏文件并拒收 |
| ③升级启动受理 | ✅（含一项待查） | 脚本首次调用被拒（原因未捕获，2 分钟后手动同参成功）——瞬时因待查，已记录 |
| ③状态机全程 | ✅ | handover_wait→backing_up→applying→verifying_start→**success**（20:11 启动→20:15:15 success，全程 **4 分半**，含 DB 快照） |
| ④build_id 三方对账 | ✅ | 验证启动后 ：5800 build=**2.5.1+gb83a5601+20261021+cython** == dist_manifest == to_build_id |
| ⑤data/ 原地保留 | ✅ | 演练标记文件升级后仍在 |

## 额外实锤（计划外收获）

1. **contains_migration=True 全链首次实弹**：DB 快照进 backup/20261021_201352 镜像位（R17「落错位」根修后的路径首次真跑）。
2. **U4 回归**：升级成功后好包自动删除（残留仅我注入的 tampered.upg，已清）。
3. **版本隔离守卫**（前两次尝试的「失败」实为正面证据）：发行包基座拒绝接管开发版后端——防混装机制在岗。
4. **升级期间业务无损**：5800 down 窗口仅 2 分钟（handover→verify_start），verify_start 拉起新构建即恢复 healthy。

## 待查/待办

1. **脚本首次 start 被拒的瞬时因**（detail 未捕获=脚本判据缺陷已修口）；2 分钟后同参成功——若复现优先查深验超时/任务忙预检。
2. 篡改负向在「回滚层」的实弹（rollback 触发需真实 verify 失败注入）——单测已覆盖 restore_backup 全链，live 层挂低优先。
3. 演练副本已删（23GB 回收）；演练产物保留：.upg+notes（E:/ccd_archive/selfheal_drill_upg/）、2.5.1 双构建（D:/ccd）、本记录。

## 清场确认

- 开发链拉回：20:27:09 healthy（2.5.1+dev）✅
- 演练实例/副本：全退全删 ✅
- E 盘余 155G ✅
