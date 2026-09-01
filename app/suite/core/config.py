"""Конфиг и пути Suite.

Данные пользователя — в %LOCALAPPDATA%\\ichamps\\suite: Program Files недоступен
на запись без прав администратора.

Конфиг разбит по модулям: каждый читает только свой раздел `modules.<id>`,
общие настройки (порт, частота опроса) лежат в корне.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_NAME = "ichamps-suite"
APP_TITLE = "ichamps Suite"

DEFAULTS = {
    "http_port": 8777,
    "poll_hz": 5.0,
    "open_panel_on_start": False,
    # автообновление
    "update_url": "",
    "update_check_on_start": True,
    "update_check_interval_min": 60,

    "modules": {
        "racecontrol": {
            "enabled": True,
            "detector_period": 1.0,
            # Детект выездов по геометрии: работает всегда, включая
            # квалификацию и режим спектатора, где счётчиков инцидентов нет
            "offtrack_enabled": True,
            "min_offtrack_seconds": 1.0,
            "offtrack_cooldown": 8.0,
            "merge_window": 12.0,
            "live_frame_threshold": 60,
            "pre_roll_live": 10.0,
            "pre_roll_estimated": 20.0,
            "incident_camera": "",
            "overlay_enabled": True,
            "overlay_alpha": 0.96,
            "overlay_hotkeys": ["ctrl+alt+r", "ctrl+shift+r", "ctrl+alt+o", "alt+f8"],
            "overlay_x": 24,
            "overlay_y": 24,
            # Синтаксис админ-команд НЕ подтверждён на живой сессии —
            # правится здесь без пересборки клиента
            "chat_commands": {
                "yellow": "!y",
                "pace_laps": "!pacelaps {n}",
                "clear": "!clear {car}",
                "drive_through": "!black {car}",
                "time_penalty": "!black {car} {sec}",
                "dq": "!dq {car}",
                "wave_by": "!waveby {car}",
                "eol": "!eol {car}",
            },
            "pace_laps_default": 2,
            "penalty_seconds": 5,
        },
        "broadcast": {
            "enabled": False,
            "tower_rows": 12,
            "gap_mode": "leader",        # leader | ahead
            "commentators": [],
        },
        "rutp": {
            "enabled": False,
            # путь к внешнему агенту; пусто = искать в стандартных местах
            "agent_path": "",
        },
    },
}


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "ichamps" / "suite"
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ichamps" / "racecontrol"


def db_path() -> Path:
    return data_dir() / "suite.sqlite3"


def config_path() -> Path:
    return data_dir() / "config.json"


def log_path() -> Path:
    return data_dir() / "suite.log"


def _deep_merge(base: dict, override: dict) -> dict:
    """Слить конфиг пользователя с дефолтами, сохранив новые ключи дефолтов."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate_from_racecontrol() -> str:
    """Перенести данные отдельного рейс-контрола при первом запуске Suite.

    Старую папку НЕ удаляем: там история инцидентов, и пользователь может
    ещё вернуться на прежний клиент.
    """
    src, dst = legacy_dir(), data_dir()
    if not src.is_dir() or (dst / ".migrated").exists():
        return ""
    moved = []
    try:
        old_db = src / "racecontrol.sqlite3"
        if old_db.exists() and not db_path().exists():
            shutil.copy2(old_db, db_path())
            moved.append("база инцидентов")

        old_cfg = src / "config.json"
        if old_cfg.exists() and not config_path().exists():
            old = json.loads(old_cfg.read_text("utf-8"))
            # плоский конфиг рейс-контрола -> раздел modules.racecontrol
            cfg = dict(DEFAULTS)
            rc = dict(DEFAULTS["modules"]["racecontrol"])
            for k, v in old.items():
                if k in ("http_port", "poll_hz", "open_panel_on_start",
                         "update_url", "update_check_on_start",
                         "update_check_interval_min"):
                    cfg[k] = v
                elif k in rc:
                    rc[k] = v
            cfg["modules"] = dict(DEFAULTS["modules"])
            cfg["modules"]["racecontrol"] = rc
            save(cfg)
            moved.append("настройки")
        (dst / ".migrated").write_text("\n".join(moved) or "nothing", "utf-8")
    except Exception as exc:                    # noqa: BLE001
        return f"миграция не удалась: {exc}"
    return ", ".join(moved)


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))      # глубокая копия
    p = config_path()
    if p.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(p.read_text("utf-8")))
        except Exception:
            pass
    else:
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")


def module_config(cfg: dict, module_id: str) -> dict:
    return cfg.setdefault("modules", {}).setdefault(module_id, {})
