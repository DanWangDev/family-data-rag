"""models.py — Ollama client with app-level serial dispatch for big models.

Policy (plan v3): embeddings stay resident; ONE big model at a time. The lock here is
the real serialization — OLLAMA_MAX_LOADED_MODELS is only a backstop in run.bat.
"""

import threading

import ollama

_BIG_LOCK = threading.Lock()


def embed_batch(model: str, texts: list[str]) -> list[list[float]]:
    resp = ollama.embed(model=model, input=texts, keep_alive="-1")  # embeddings resident
    return [list(e) for e in resp["embeddings"]]


def embed_dims(model: str) -> int:
    return len(embed_batch(model, ["probe"])[0])


def complete(
    model: str,
    messages: list[dict],
    *,
    num_ctx: int = 16384,
    temperature: float = 0.2,
    keep_alive: str = "20m",
) -> str:
    with _BIG_LOCK:
        resp = ollama.chat(
            model=model,
            messages=messages,
            options={"num_ctx": num_ctx, "temperature": temperature},
            keep_alive=keep_alive,
        )
        return resp["message"]["content"]


def model_digest(model: str) -> str:
    """Digest of the locally installed model, for index_meta provenance."""
    for m in ollama.list()["models"]:
        if m["model"] == model or m["name"] == model:
            return m["digest"].split(":")[0]
    raise ValueError(f"model not installed: {model}")
