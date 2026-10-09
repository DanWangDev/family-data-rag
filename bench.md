# Phase 0 benchmark — 2026-10-09

**English** | [简体中文](bench.zh-CN.md)

Machine: Windows 11 Pro, RTX 5070 Ti (16 GB, driver 617.42), Ollama 0.40.2.

## Models

| model | ID | size | arch | ctx | quant |
|---|---|---|---|---|---|
| qwen3:14b | bdbd181c33f2 | 9.3 GB | qwen3 | 40960 | Q4_K_M |
| qwen3:8b | 500a1f067a9f | 5.2 GB | qwen3 | 40960 | Q4_K_M |
| qwen3-vl:8b | 901cae732162 | 6.1 GB | qwen3vl | 262144 | Q4_K_M |
| nomic-embed-text | 0a109f422b47 | 274 MB | nomic-bert | 2048 | F16 |
| bge-m3 | 790764642607 | 1.2 GB | bert | 8192 | F16 |

Embedding dims verified: nomic 768, bge-m3 1024.

## qwen3:14b (clean slate, all other models unloaded)

- cold start (model load → first token): 10.9 s
- generation: 41.3 tok/s (120 tokens)
- peak VRAM: 14,752 MiB at default 40,960 ctx — KV-dominated; chat will pin `num_ctx=16384` (~2.5 GB saved)

## Scheduling finding

First measurement was contaminated: a stale unsloth 27B from a previous manual `ollama run`
session was resident in VRAM → the 14B spilled to CPU: 0.5 tok/s, 297 s cold start,
15,960 MiB peak. Evidence for the one-big-model-at-a-time policy (`keep_alive` +
`OLLAMA_MAX_LOADED_MODELS=2`): stale manual runs squat VRAM and silently wreck latency.
