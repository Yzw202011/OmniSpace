# 远程推理服务器部署模板（A100 / H100 专业卡）

供 OmniSpace「设置 → 远程推理服务器」连接的 Linux 侧 vLLM 服务部署模板。
桌面端只需在设置页填服务器地址即可，本目录内容都跑在**服务器上**。

> 适用：Ubuntu 20.04+/22.04，NVIDIA A100/H200/H100/4090 级专业或消费卡，
> 已装 NVIDIA 驱动（`nvidia-smi` 正常）。桌面端与服务器需在同一网络可达。

## 方式一：pip 直装（最简）

```bash
# 建议独立虚拟环境
python -m venv ~/vllm-env && source ~/vllm-env/bin/activate
pip install -U vllm

# A100（Ampere，无原生 FP8 → 用 BF16 或 INT8 量化权重）
vllm serve /path/to/Qwen3-32B-AWQ \
  --served-model-name qwen3-32b-awq \
  --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 2 --dtype bfloat16 \
  --max-model-len 16384 --gpu-memory-utilization 0.9

# H100/H200（Hopper，原生 FP8 → 最佳档）
vllm serve /path/to/Qwen3-32B-FP8 \
  --served-model-name qwen3-32b \
  --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 2 \
  --max-model-len 32768 --gpu-memory-utilization 0.9

# 可选：api-key 鉴权（桌面端设置页对应填写）
vllm serve ... --api-key sk-your-secret
```

## 方式二：Docker Compose

```bash
cd docs/deploy && docker compose up -d
docker compose logs -f vllm   # 等待 "Application startup complete"
```

`docker-compose.yml` 要点：`--gpus all`、`--ipc=host`（NCCL 共享内存）、
模型目录挂载进容器、`--port 8000`。改模型路径/名称编辑该文件顶部变量。

## 验证

```bash
curl http://<服务器IP>:8000/health                 # 200 = 就绪
curl http://<服务器IP>:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-32b-awq","messages":[{"role":"user","content":"你好"}]}'
```

## OmniSpace 侧接线

1. 设置 → 远程推理服务器：地址填 `http://<服务器IP>:8000`，
   密钥按需，远端模型名填 `--served-model-name` 的值；
2. 点「测试连接」→ 通过后「保存」；
3. 即刻生效：AI 对话 / 写作台生成全部走远端大模型，本机显卡照常画画。

## 常见坑

| 症状 | 原因/解法 |
|---|---|
| 桌面端「持续不可达」 | 服务器防火墙未放行 8000：`sudo ufw allow 8000` |
| HTTP 404 model not found | `--served-model-name` 与设置页「远端模型名」不一致 |
| 容器里 NCCL 报错 | 缺 `--ipc=host` 或 `--shm-size`（见 compose 模板） |
| A100 加载 FP8 权重报错 | A100 无原生 FP8，换 BF16 权重或 INT8 量化版 |
| 多卡张量并行启动慢 | 首次建 CUDA 图需几分钟，属正常（日志等 startup complete） |
