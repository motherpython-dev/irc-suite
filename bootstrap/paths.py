r"""Расположение версий приложения.

Схема автообновления: exe (ядро с интерпретатором и зависимостями) меняется редко,
а логика приложения живёт отдельным бандлом и обновляется без перекачки клиента.

%LOCALAPPDATA%\ichamps\suite\
    versions\
        1.0.0\suite...        <- распакованный бандл
        1.0.1\suite...
    current.json                     <- какая версия активна + last_good
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "ichamps" / "suite"
    p.mkdir(parents=True, exist_ok=True)
    return p


def versions_dir() -> Path:
    p = data_dir() / "versions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def version_dir(version: str) -> Path:
    return versions_dir() / version


def pointer_path() -> Path:
    return data_dir() / "current.json"


def read_pointer() -> dict:
    p = pointer_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def write_pointer(data: dict) -> None:
    tmp = pointer_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(pointer_path())      # атомарная замена


def seed_dir() -> Path:
    """Бандл, вшитый в exe (или лежащий рядом при запуске из исходников)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "seed"
    return Path(__file__).resolve().parent.parent / "app"
