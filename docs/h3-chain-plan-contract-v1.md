# H3 链式引擎 · 计划单契约 v1（冻结稿 2026-08-30）

> 目标读者：要给漫剧模块递视频生成任务的外部软件/skill。
> 你只需要这一份文档 + HTTP,不需要知道 ComfyUI 的存在。

## 1. 唯一入口

```
POST /api/v1/manga/video/generate_h3_chain
Content-Type: application/json
```

## 2. 请求字段（计划单）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `row_ids` | string[] | 是 | 分镜行 id 列表,**按顺序每行渲染一镜**;参考图取各行绑定资产(角色→场景→道具)的并集,≤7 张 |
| `seconds_per_shot` | number | 否 | 每镜秒数,3~15,默认 10;帧数自动对齐 17k+5 网格(24fps) |
| `quality` | string | 否 | `480p`(864×480,每镜≤15s,16G 卡实测安全)或 `720p`(1344×768,**每镜≤8s**,超长会被拒绝) |
| `start_clip` | int | 否 | 断点续跑:从第几镜开始(1=整片);沿用同 `run_name` 的已完成镜检查点 |
| `run_name` | string | 否 | 运行名;续跑时**必须传原运行名**,否则从零开始。默认自动生成 |

## 3. 响应

- 受理成功:`{"success": true, "data": {"task_id": "...", "status": "generating", "engine": "h3_chain"}}`
- 参数错误:`success=false` + `error.code=60001`(消息里写明原因)

## 4. 轮询与结果

- `GET /api/v1/manga/video/{task_id}/status` → `data.status` = `generating|done|failed`,`data.progress` 0~1
- `GET /api/v1/manga/video/{task_id}/result` → 完成后 `data.result`:
  `file_path`(绝对路径)、`duration_seconds`、`resolution`、`model_used="h3_chain_turbo_v10"`、`download_url`、`file_exists`
- `GET /api/v1/manga/video/{task_id}/download` → 直接下载 MP4(H264+AAC,24fps)

## 5. 错误码

| code | 含义 |
| --- | --- |
| 60001 | 参数问题(时长越界/画质档矛盾/缺绑定资产/描述词为空) |
| 60003 | 引擎问题(ComfyUI 未就绪/计划被拒/执行失败/成片缺失) |
| 60004 | 超时或任务被中断(可用 start_clip 续跑) |
| 40005 | 分镜行不存在 |

## 6. 画质档位（RTX 5070 Ti 16G 实测,2026-08-30）

| 档位 | 分辨率 | 每镜时长 | 显存峰值 |
| --- | --- | --- | --- |
| 480p | 864×480 | 10.1s(243帧)~15s(362帧) | ~11.6GB |
| 720p | 1344×768 | ≤8s(192帧) | ~15.6GB(擦线) |

> 更长的片子 = 加 `row_ids` 镜数,不要加单镜时长。

## 7. 断点续跑

任意原因中断后:用**原 run_name** + `start_clip=已完成镜数+1` 重新提交,
已完成的镜不重渲染;`start_clip` 之前的镜若改动过提示词/种子/时长,续跑会被拒绝(指纹校验)。

## 8. 约束(提交前自查)

- 每镜绑定的资产必须有图(参考图来自 comic_assets,绑定名与资产名模糊匹配 ≥2 字)
- 提示词若含台词,画面会出现烧录字幕;需要干净画面请写「无字幕」
- 同一时刻仅一个视频任务占用 GPU;并发提交会被功能锁拒绝(409 语义),请串行排队

—— 契约版本 v1,冻结于 2026-08-30。字段只增不改;破坏性变更会升 v2 并行提供一段时间。
