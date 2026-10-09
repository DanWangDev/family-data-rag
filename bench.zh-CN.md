# Phase 0 基准测试 — 2026-10-09

[English](bench.md) | **简体中文**

测试机器：Windows 11 Pro，RTX 5070 Ti（16 GB，驱动 617.42），Ollama 0.40.2。

## 模型

| 模型 | ID | 大小 | 架构 | 上下文 | 量化 |
|---|---|---|---|---|---|
| qwen3:14b | bdbd181c33f2 | 9.3 GB | qwen3 | 40960 | Q4_K_M |
| qwen3:8b | 500a1f067a9f | 5.2 GB | qwen3 | 40960 | Q4_K_M |
| qwen3-vl:8b | 901cae732162 | 6.1 GB | qwen3vl | 262144 | Q4_K_M |
| nomic-embed-text | 0a109f422b47 | 274 MB | nomic-bert | 2048 | F16 |
| bge-m3 | 790764642607 | 1.2 GB | bert | 8192 | F16 |

已验证 embedding 维度：nomic 为 768，bge-m3 为 1024。

## qwen3:14b（干净环境，其他模型全部卸载）

- 冷启动（加载模型 → 首个 token）：10.9 s
- 生成速度：41.3 tok/s（120 tokens）
- 显存峰值：默认 40,960 上下文时为 14,752 MiB，主要由 KV 缓存占用；chat 将固定 `num_ctx=16384`（约节省 2.5 GB）

## 模型调度发现

首次测量受到干扰：之前手动 `ollama run` 会话遗留的 unsloth 27B 模型仍驻留显存，
导致 14B 部分转移到 CPU：生成速度 0.5 tok/s，冷启动 297 s，显存峰值 15,960 MiB。
这支持一次只运行一个大模型的策略（`keep_alive` + `OLLAMA_MAX_LOADED_MODELS=2`）：
手动运行遗留的模型会占用显存，并在不明显报错的情况下严重增加延迟。
