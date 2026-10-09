"""Load config.toml (stdlib tomllib)."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Models:
    chat: str
    extract: str
    embed: str
    vision: str


@dataclass(frozen=True)
class Config:
    source_dirs: tuple[Path, ...]
    db_path: Path
    blob_dir: Path
    max_file_mb: int
    photo_captions: bool
    models: Models

    @classmethod
    def load(cls, path: str = "config.toml") -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        models = data["models"]
        return cls(
            source_dirs=tuple(Path(p) for p in data["source_dirs"]),
            db_path=Path(data["db_path"]),
            blob_dir=Path(data["blob_dir"]),
            max_file_mb=int(data["max_file_mb"]),
            photo_captions=bool(data["photo_captions"]),
            models=Models(**models),
        )
