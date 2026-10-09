# family-data-rag

Local, privacy-first RAG system for family data — documents, photos, bills, and legal
docs. Everything runs on this machine: local LLMs via Ollama, embedded SQLite storage,
no cloud calls.

## Status

Phase 1 complete: versioned ingest (NAS sources + local blob store), hybrid retrieval
(vector + keyword + graph hooks), chat with citations and honest refusal, backup/restore,
Streamlit UI. Next: Phase 2 knowledge-graph extraction.

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

## Usage

```
.venv\Scripts\python -m ingest scan        # scan sources, index new/changed files
.venv\Scripts\python -m ingest status      # index overview
.venv\Scripts\python -m ingest backup      # WAL-safe snapshot + blob manifest
.venv\Scripts\python -m ingest restore-test
.venv\Scripts\python -m selftest           # all checks (model-free)
run.bat                                    # launch the web UI (localhost only)
```

Indexed data lives in `data/` (SQLite + content-addressed blobs) — excluded from git and
cloud sync. Encrypt the volume (BitLocker).

## Security

Sensitive family data stays local. Ollama binds localhost only, cloud features are
disabled (`OLLAMA_NO_CLOUD=1`, verified at startup by `run.ps1`), and document text is
treated as data, never instructions. No auth (single-user machine) — LAN access, if ever
needed, goes through a VPN only.
