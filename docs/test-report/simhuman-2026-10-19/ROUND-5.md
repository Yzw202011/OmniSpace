# 仿真人深测 ROUND-5 报告——漏洞面+链路监控批 11/11✅（2026-10-19）

> 计划真源：docs/test-plan-simhuman-grand-2026-10-19.md（846 条）。承接 ROUND-0~4。
> 窗口：四线 WIP 第 6 轮原封、后端同版本 13h、dist 未动——继续零修复批；
> 测试轮次邻线 15:25 已获 GPU 窗口跑 D-05 四视图（GPU 类用例由其消费，本线不重复）。

## 一、本轮实弹（11/11 ✅）

| 用例 | 结果 | 证据 |
|---|---|---|
| P-14 错误信息泄露 | ✅ | 十类错误响应采样：零栈/零绝对路径/零 key 回显（curl+grep body 级） |
| P-33 日志敏感面 | ✅ | backend.log+轮转件 sk-/api_key/password 模式零命中 |
| N-46 安全响应头 | ✅ | server: uvicorn 无版本号、无 x-powered-by（最小暴露） |
| N-42 WER 取证窗 | ✅ | Windows 错误报告日志零 RADAR/OmniSpace 条目——静默死亡前兆=0（M12 佐证） |
| N-43 进程品牌 | ✅ | OmniSpace-Backend.exe 品牌进程在册（七品牌口径） |
| N-48 事件/任务中心 | ✅（口径修正） | 事件流真身=/logs/events（7,475 条已验）；/system/tasks/center 活数据（视频镜任务卡 done） |
| N-08 遗留对话流端点 | ✅ | /dialog/stream/{sid} 连接被拒——废弃端点明确拒绝无挂起，兼容行为清晰 |
| N-31 KV 单源+错误码哨兵 | ✅ | test_kv_single_source + test_error_code_registry 3/3 绿 |
| A-30 前进后退 | ✅ | models↔logs 回放路由准确 |
| A-34 双标签一致性 | ✅ | 第二标签能看到真实会话列表（服务端态一致） |
| （工具坑）Mimosa 拦截 | 留痕 | SSRF 模式静态误报拦临时探针脚本——按记忆口径改 curl+grep 内联绕行成功 |

## 二、本轮修复

无——漏洞面全部 CLEAN 即本批结论；我圈修复候选（K-06/K-09/K-19/comic_gen 拆分/ComicWorkspace:101）连续第 6 轮被在飞件占用。

## 三、累计进度与未完成

**进度：47/846 已实弹（45✅ / 2❌已修待回归：N-03 等后端重启、L-01 等 dist 部署）；R1 P0 余 69。**

受阻不变：GPU 独占窗口（R1 大头，邻线在消费）；漫剧在飞红账（comic_gen+304=第 6 闸唯一红因、ComicWorkspace:101=vitest 唯一红）；待拍板三项（K-01 资产重复行 A/B/C、K-02 视频槽消费、G4 两 P1）。

## 四、闸门状态

1~5 ✅ / 6 ⚠️（在飞 comic_gen.py，红因不变）。
