"""models.py — Ollama client with app-level serial dispatch for big models.

Security contract (docs/security-review.md item 1):
- the client is pinned to http://127.0.0.1:11434 (env OLLAMA_HOST is never read),
- env proxies are disabled (trust_env=False),
- the SDK attaches OLLAMA_API_KEY as a Bearer header even when host+headers are passed
  explicitly (verified 2026-10-09); we drop the inherited key before construction and
  REFUSE to use a client that still carries an Authorization header,
- every network call requires the server to report cloud features disabled.
"""

import os
import threading
from functools import cache

import ollama

LOCAL_ENDPOINT = "http://127.0.0.1:11434"

_BIG_LOCK = threading.Lock()


def _client() -> ollama.Client:
    os.environ.pop("OLLAMA_API_KEY", None)
    client = ollama.Client(
        host=LOCAL_ENDPOINT,
        trust_env=False,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    if any("authorization" in k.lower() for k in client._client.headers):
        raise RuntimeError("refusing to use an ollama client with an inherited Authorization header")
    return client


@cache
def cloud_disabled() -> bool:
    """One-time check against the pinned local endpoint: cloud features must be off."""
    resp = _client()._client.get("/api/status")  # same pinned, proxy-free client
    resp.raise_for_status()
    return bool(resp.json().get("cloud", {}).get("disabled"))


def _require_local_only() -> None:
    if not cloud_disabled():
        raise RuntimeError("ollama reports cloud features enabled — refusing (OLLAMA_NO_CLOUD=1 required)")


def embed_batch(model: str, texts: list[str]) -> list[list[float]]:
    _require_local_only()
    resp = _client().embed(model=model, input=texts, keep_alive="100000h")  # resident; bare "-1" no longer parses
    return [list(e) for e in resp["embeddings"]]


@cache
def embed_dims(model: str) -> int:
    return len(embed_batch(model, ["probe"])[0])


def complete(
    model: str,
    messages: list[dict],
    *,
    num_ctx: int = 16384,
    temperature: float = 0.2,
    keep_alive: str = "20m",
    format: dict | None = None,
) -> str:
    _require_local_only()
    options: dict = {"num_ctx": num_ctx, "temperature": temperature}
    kwargs: dict = {"options": options, "keep_alive": keep_alive}
    if format is not None:
        kwargs["format"] = format  # constrained decoding (structured outputs)
    with _BIG_LOCK:
        resp = _client().chat(model=model, messages=messages, **kwargs)
        return resp["message"]["content"]


@cache
def model_digest(model: str) -> str:
    """Digest of the locally installed model, for index_meta provenance."""
    _require_local_only()
    for m in _client().list()["models"]:
        try:
            name = m["model"]
        except KeyError:
            name = m["name"]
        if name == model or name.rsplit(":", 1)[0] == model:  # list names carry :latest tags
            return m["digest"].split(":")[0]
    raise ValueError(f"model not installed: {model}")
