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

APP_NAME = "irc"
APP_TITLE = "iRC"

DEFAULTS = {
    "http_port": 8777,
    "poll_hz": 5.0,
    # при старте показываем только главное окно; оверлеи и пульт
    # включаются уже из него
    "open_panel_on_start": True,
    # Доступ к панели с телефона в своей сети. Пароля нет: включать
    # осмысленно в домашней сети, не в открытом Wi-Fi
    "allow_lan": False,
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
            "refresh_hz": 3,             # чаще смысла нет: башня не про кадры
            "tower_rows": 12,
            "tower_title": "",
            "gap_mode": "leader",        # leader | ahead
            "show_pits": True,
            # Появление и уход графики. Мгновенное включение в эфире читается
            # как сбой картинки, поэтому по умолчанию анимируем.
            "animate": True,
            "focus_car": "",             # держать эту машину в кадре
            "commentators": [],
            # Что сейчас в эфире. Управляет iRC, а не сцены OBS: в OBS все
            # источники добавляются один раз и висят постоянно.
            "visible": {},
            # Пульт поверх сима — единственный способ увидеть и переключить
            # эфир, не сворачивая полноэкранный iRacing на одном мониторе
            # Макросы пульта: именованные кнопки, шаги идут по порядку.
            # Пример — камера в салон машины в кадре и через секунду разрыв.
            "macros": [
                {"id": "cockpit-delta", "title": "Кокпит + разрыв",
                 "steps": [
                     {"type": "camera", "group": "Cockpit", "car": "focus"},
                     {"type": "wait", "sec": 1.0},
                     {"type": "overlay", "id": "delta", "action": "show"},
                 ]},
            ],
            "deck_enabled": True,
            "deck_alpha": 0.96,
            "deck_x": 470,
            "deck_y": 24,
        },
        "rutp": {
            "enabled": False,
            # путь к внешнему агенту; пусто = искать в стандартных местах
            "agent_path": "",
        },
    },
}


def data_dir() -> Path:
    """Папка данных пользователя.

    IRC_DATA_DIR переопределяет её — это нужно тестам и запускам из исходников:
    без такой отдельной папки пробный прогон пишет свой порт и свои настройки
    в боевой конфиг, и установленное приложение потом стартует не там, где ждут.
    """
    override = os.environ.get("IRC_DATA_DIR")
    if override:
        p = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        p = Path(base) / "iRC"
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_dirs() -> list[Path]:
    """Папки прежних версий, откуда переносим данные при первом запуске iRC."""
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
    return [base / "ichamps" / "suite",          # промежуточная сборка Suite
            base / "ichamps" / "racecontrol"]    # отдельный клиент рейс-контроля


def db_path() -> Path:
    return data_dir() / "irc.sqlite3"


def config_path() -> Path:
    return data_dir() / "config.json"


def log_path() -> Path:
    return data_dir() / "irc.log"


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
    dst = data_dir()
    if (dst / ".migrated").exists():
        return ""
    src = next((d for d in legacy_dirs() if d.is_dir()), None)
    if src is None:
        return ""
    moved = []
    try:
        old_db = next((src / n for n in ("suite.sqlite3", "racecontrol.sqlite3")
                       if (src / n).exists()), None)
        if old_db and not db_path().exists():
            shutil.copy2(old_db, db_path())
            moved.append("база инцидентов")

        old_cfg = src / "config.json"
        if old_cfg.exists() and not config_path().exists():
            old = json.loads(old_cfg.read_text("utf-8"))
            if "modules" in old:
                # конфиг Suite уже в нужной форме — переносим как есть
                save(_deep_merge(json.loads(json.dumps(DEFAULTS)), old))
                moved.append("настройки")
                (dst / ".migrated").write_text("\n".join(moved), "utf-8")
                return ", ".join(moved)
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
