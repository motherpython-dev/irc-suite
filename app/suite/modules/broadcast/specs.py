"""Описания оверлеев: из чего состоит каждый и как выглядит.

Оверлей — это JSON: размеры, шапка, повторяющийся элемент (строка башни,
ячейка решётки, карточка комментатора), подвал, палитра и шрифты. Каждый
элемент — текст, чип, плашка или картинка — лежит на своих координатах и
знает, какое поле данных показывает. Один HTML-отрисовщик (`web/ov/render.js`)
строит по описанию и родное окно поверх iRacing, и страницу для OBS, поэтому
картинка везде одинаковая, а редактор меняет только описание.

Здесь — описания по умолчанию (эфирный стиль, палитра iRChamps) и хранение
пользовательских правок: `<данные>/overlays/<id>.json` поверх умолчаний.
Удалить файл — вернуть оверлей к исходному виду.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
from pathlib import Path

PALETTE = {
    "accent": "#FF5A1F", "accent2": "#FF8A4C",
    "bg": "#0B0E14", "row": "#141923", "row_alt": "#1A2130", "chip": "#232C3D",
    "fg": "#E6EDF3", "dim": "#8B98A9", "live": "#3FB950", "pit": "#F0A020",
    "pit_row": "#1B2A3A",
}

FONTS = {
    # узкий DIN есть в Windows 10 и 11; дальше — запасные
    "num": "'Bahnschrift SemiBold Condensed','Bahnschrift','Arial Narrow',sans-serif",
    "text": "'Bahnschrift Condensed','Bahnschrift','Arial Narrow',sans-serif",
    "ui": "'Segoe UI',system-ui,sans-serif",
}


def _t(id_, text, x, y, w, h, **style):
    """Текстовый элемент."""
    return {"id": id_, "type": "text", "text": text, "x": x, "y": y, "w": w, "h": h, **style}


def _chip(id_, text, x, y, w, h, **style):
    """Чип: фигура с надписью — номер машины под цвет класса."""
    base = {"fill": "class", "shape": "slant", "font": "num", "size": 13,
            "weight": 600, "italic": True, "color": "#0B0E14", "align": "center"}
    base.update(style)
    return {"id": id_, "type": "chip", "text": text, "x": x, "y": y, "w": w, "h": h, **base}


def _box(id_, x, y, w, h, **style):
    """Плашка без текста: подложки, акцентные скосы."""
    return {"id": id_, "type": "box", "x": x, "y": y, "w": w, "h": h, **style}


def _head(title_text, width, right_text="{$.track|upper}", h=30):
    return {
        "height": h,
        "bg": {"fill": "$accent", "shape": "slant-right"},
        "elements": [
            _t("title", title_text, 12, 0, width // 2, h, font="num", size=15,
               weight=600, color="#FFFFFF", upper=True),
            # правый край шапки срезан скосом — отступаем, чтобы текст не заезжал
            _t("right", right_text, width // 2, 0, width // 2 - 22, h, font="text",
               size=12, color="#FFE8DC", align="right"),
        ],
    }


DEFAULTS: dict[str, dict] = {
    # ---------------- пилон позиций ----------------
    "tower": {
        "title": "Башня позиций",
        "source": "/api/broadcast/tower",
        "width": 340,
        "enter": "left",
        "frame": {"radius": 6, "shadow": True, "flag_stripe": 5},
        "header": _head("КРУГ {$.lap}{$.laps_total|slash}", 340),
        "body": {
            "type": "list", "items": "rows", "key": "car_number",
            "empty": "ОЖИДАНИЕ СЕССИИ",
            "item": {
                "height": 27,
                "bg": {"fill": "$row", "alt": "$row_alt"},
                "states": {"leader": {"edge": "$accent"}, "in_pit": {"fill": "$pit_row"}},
                "elements": [
                    _t("pos", "{position}", 6, 0, 24, 27, font="num", size=18,
                       weight=600, italic=True, color="$fg", align="right",
                       states={"leader": {"color": "#FFFFFF"}}),
                    _chip("num", "{car_number}", 38, 5, 36, 17),
                    _t("name", "{name|surname}", 84, 0, 170, 27, font="num", size=15,
                       weight=600, color="$fg", states={"leader": {"color": "#FFFFFF"}}),
                    _t("gap", "{gap|gap}", 250, 0, 80, 27, font="text", size=14,
                       color="$dim", align="right",
                       states={"leader": {"color": "$accent2"}, "in_pit": {"color": "$pit"}}),
                ],
            },
        },
    },

    # ---------------- стартовая решётка ----------------
    "grid": {
        "title": "Стартовая решётка",
        "source": "/api/broadcast/grid",
        "width": 880,
        "enter": "up",
        "frame": {"radius": 6, "shadow": True},
        "header": _head("{$.title|upper}", 880, h=28),
        "body": {
            "type": "grid", "items": "rows", "key": "car_number",
            "columns": 2, "page_size": 10, "page_seconds": 15, "gap": 3,
            "empty": "ОЖИДАНИЕ КВАЛИФИКАЦИИ",
            "item": {
                "height": 58,
                "bg": {"fill": "$row"},
                "states": {"leader": {"fill": "#22160E"}},
                "elements": [
                    _t("pos", "{position}", 8, 0, 36, 58, font="num", size=28,
                       weight=600, color="#FFFFFF", align="right",
                       states={"leader": {"color": "$accent2"}}),
                    _chip("num", "{car_number}", 56, 16, 44, 26, size=16),
                    _t("name", "{name|surname}", 114, 8, 210, 26, font="num", size=20,
                       weight=600, color="$fg"),
                    _t("team", "{team|upper}", 114, 33, 210, 16, font="text", size=11,
                       color="$dim"),
                    _t("pole", "{time|laptime}", 300, 0, 124, 58, font="num", size=17,
                       color="$live", align="right", when="leader"),
                    _t("delta", "{delta|plus3}", 300, 0, 124, 58, font="num", size=17,
                       color="$dim", align="right", when="!leader"),
                ],
            },
        },
        "footer": {
            "height": 26,
            "bg": {"fill": "$row"},
            "elements": [
                _t("src", "{$.source|source_label}", 12, 0, 420, 26, font="text",
                   size=11, color="$dim"),
                _t("pages", "МЕСТА {$.page_from}–{$.page_to} ИЗ {$.count} · "
                   "СТРАНИЦА {$.page}/{$.pages}", 440, 0, 428, 26, font="text",
                   size=11, color="$dim", align="right", when="$.pages>1"),
            ],
        },
    },

    # ---------------- плашка комментаторов ----------------
    "commentators": {
        "title": "Плашка комментаторов",
        "source": "/api/broadcast/commentators",
        "enter": "up",
        "frame": {"radius": 0, "shadow": True},
        "body": {
            "type": "cards", "items": "people", "key": "name", "gap": 8,
            "empty": "",
            "item": {
                # ширина под имя с фамилией: при 230 «КОММЕНТАТОР ОДИН» обрезался
                "width": 280, "height": 52,
                "bg": {"fill": "$row"},
                "elements": [
                    _box("edge", 0, 0, 12, 52, fill="$accent", shape="slant-right"),
                    _t("name", "{name|upper}", 20, 6, 254, 26, font="num", size=21,
                       weight=600, color="#FFFFFF"),
                    _t("role", "{role|upper}", 21, 32, 254, 14, font="text", size=12,
                       color="$accent2"),
                ],
            },
        },
    },

    # ---------------- разрыв пары ----------------
    "delta": {
        "title": "Дельта пилотов",
        "source": "/api/broadcast/delta",
        "width": 300,
        "enter": "right",
        "frame": {"radius": 6, "shadow": True},
        "header": _head("РАЗРЫВ", 300, right_text="", h=28),
        "body": {
            "type": "single", "height": 118, "ready": "ready",
            "empty": "ПАРА НЕ ВЫБРАНА",
            "bg": {"fill": "$row"},
            "elements": [
                _box("lead_bg", 0, 0, 300, 32, fill="$row"),
                _t("lead_pos", "{lead.position}", 6, 0, 22, 32, font="num", size=15,
                   color="$dim", align="right"),
                _chip("lead_num", "{lead.car_number}", 36, 7, 34, 19,
                      fill_from="lead.class_name"),
                _t("lead_name", "{lead.name|surname}", 80, 0, 210, 32, font="num",
                   size=17, weight=600, color="$fg"),
                _t("gap", "{gap|fixed1}", 14, 32, 100, 54, font="num", size=38,
                   weight=600, color="#FFFFFF"),
                _t("trend", "{trend|trend}", 118, 32, 170, 54, font="num", size=13,
                   color="$dim",
                   states={"closing": {"color": "$live"}, "growing": {"color": "#FF6B6B"}}),
                _box("chase_bg", 0, 86, 300, 32, fill="$row_alt"),
                _t("chase_pos", "{chase.position}", 6, 86, 22, 32, font="num", size=15,
                   color="$dim", align="right"),
                _chip("chase_num", "{chase.car_number}", 36, 93, 34, 19,
                      fill_from="chase.class_name"),
                _t("chase_name", "{chase.name|surname}", 80, 86, 210, 32, font="num",
                   size=17, weight=600, color="$fg"),
            ],
        },
    },

    # ---------------- пит-стопы ----------------
    "pits": {
        "title": "Пит-стопы",
        "source": "/api/broadcast/pits",
        "width": 360,
        "enter": "right",
        "frame": {"radius": 6, "shadow": True},
        "header": _head("{$.title|upper}", 360, right_text="", h=28),
        "body": {
            "type": "list", "items": "rows", "key": "car_number",
            "empty": "ПИТ-СТОПОВ ПОКА НЕ БЫЛО",
            "item": {
                "height": 28,
                "bg": {"fill": "$row", "alt": "$row_alt"},
                "states": {"in_pit": {"fill": "$pit_row"}},
                "elements": [
                    _t("pos", "{position}", 6, 0, 22, 28, font="num", size=14,
                       color="$dim", align="right"),
                    _chip("num", "{car_number}", 36, 5, 32, 18),
                    _t("name", "{name|surname}", 78, 0, 170, 28, font="num", size=15,
                       weight=600, color="$fg"),
                    _t("last", "{last.duration|sec1}", 244, 0, 64, 28, font="text",
                       size=13, color="$dim", align="right"),
                    _t("count", "{count}", 314, 0, 36, 28, font="num", size=17,
                       weight=600, color="#FFFFFF", align="center"),
                ],
            },
        },
    },
}


def _data_dir() -> Path:
    base = os.environ.get("IRC_DATA_DIR")
    if base:
        return Path(base)
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "iRC"


def user_dir() -> Path:
    p = _data_dir() / "overlays"
    p.mkdir(parents=True, exist_ok=True)
    return p


def assets_dir() -> Path:
    """Шрифты и логотипы, загруженные в редакторе."""
    p = _data_dir() / "overlay-assets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _merge(base, over):
    """Правки пользователя поверх умолчаний. Списки заменяются целиком:
    элементы оверлея — это список, и слияние по индексу перепутало бы их."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def load(overlay_id: str) -> dict | None:
    """Описание оверлея: умолчание + правки. version меняется при сохранении —
    по нему открытые оверлеи узнают, что вид пора перестроить."""
    base = DEFAULTS.get(overlay_id)
    if base is None:
        return None
    spec = copy.deepcopy(base)
    spec.setdefault("palette", dict(PALETTE))
    spec.setdefault("fonts", dict(FONTS))
    version = 0
    f = user_dir() / f"{overlay_id}.json"
    if f.exists():
        try:
            spec = _merge(spec, json.loads(f.read_text("utf-8")))
            version = int(f.stat().st_mtime_ns // 1_000_000)
        except (OSError, ValueError):
            pass                        # битый файл — показываем умолчание
    spec["id"] = overlay_id
    spec["version"] = version
    return spec


def save(overlay_id: str, spec: dict) -> dict:
    if overlay_id not in DEFAULTS:
        return {"ok": False, "error": "unknown_overlay"}
    clean = {k: v for k, v in (spec or {}).items() if k not in ("id", "version")}
    (user_dir() / f"{overlay_id}.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), "utf-8")
    return {"ok": True, "spec": load(overlay_id)}


def reset(overlay_id: str) -> dict:
    """Вернуть оверлей к исходному виду: удалить файл правок.

    Вызывается только кнопкой «Сбросить» в редакторе, после подтверждения.
    """
    if overlay_id not in DEFAULTS:
        return {"ok": False, "error": "unknown_overlay"}
    f = user_dir() / f"{overlay_id}.json"
    if f.exists():
        f.unlink()
    return {"ok": True, "spec": load(overlay_id)}


# ---------------- шрифты и логотипы ----------------

ASSET_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
}
MAX_ASSET = 10 * 1024 * 1024


def _safe_name(name: str) -> str | None:
    """Имя файла без путей и странных символов; расширение — из разрешённых."""
    base = os.path.basename(str(name or "")).strip()
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in ASSET_TYPES:
        return None
    stem = re.sub(r"[^\w\-. ]+", "_", stem).strip(" .") or "file"
    return stem[:60] + ext


def save_asset(name: str, data_b64: str) -> dict:
    fname = _safe_name(name)
    if not fname:
        return {"ok": False, "error": "bad_type",
                "message": "Картинки: png, jpg, webp, gif, svg. Шрифты: ttf, otf, woff, woff2."}
    try:
        raw = base64.b64decode(str(data_b64).split(",")[-1], validate=False)
    except Exception:
        return {"ok": False, "error": "bad_data"}
    if len(raw) > MAX_ASSET:
        return {"ok": False, "error": "too_big", "message": "Файл больше 10 МБ."}
    (assets_dir() / fname).write_bytes(raw)
    return {"ok": True, "name": fname, "url": f"/ov/asset?name={fname}",
            "kind": "font" if ASSET_TYPES[os.path.splitext(fname)[1]].startswith("font") else "image"}


def list_assets() -> list[dict]:
    out = []
    for f in sorted(assets_dir().iterdir()):
        ext = f.suffix.lower()
        if f.is_file() and ext in ASSET_TYPES:
            out.append({"name": f.name, "url": f"/ov/asset?name={f.name}",
                        "kind": "font" if ASSET_TYPES[ext].startswith("font") else "image"})
    return out


def read_asset(name: str) -> tuple[bytes, str] | None:
    fname = _safe_name(name)
    if not fname:
        return None
    f = assets_dir() / fname
    if not f.is_file():
        return None
    return f.read_bytes(), ASSET_TYPES[f.suffix.lower()]
