# family-data-rag

Local, privacy-first RAG system for family data — documents, photos, bills, and legal
docs. Everything runs on this machine: local LLMs via Ollama, embedded SQLite storage,
no cloud calls.

## Status

Phase 0: environment setup and baseline.

## Setup

1. Install [Ollama](https://ollama.com/download) (>= 0.16.2) and pull the models:
   ```
   ollama pull qwen3:14b
   ollama pull qwen3:8b
   ollama pull qwen3-vl:8b
   ollama pull nomic-embed-text
   ```
2. Python 3.14 venv:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. Copy `config.toml.example` to `config.toml` and set your source folders.

## Security

Sensitive family data stays local. Ollama binds localhost only, cloud features are
disabled (`OLLAMA_NO_CLOUD=1`), and `data/` (index + blobs) is excluded from git and
cloud sync. Encrypt the volume holding `data/` (BitLocker).
