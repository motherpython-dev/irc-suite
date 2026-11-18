"""Описания оверлеев: из чего состоит каждый и как выглядит.

Оверлей — это JSON: размеры, шапка, повторяющийся элемент (строка башни,
ячейка решётки, карточка комментатора), подвал, палитра и шрифты. Каждый
элемент — текст, чип, плашка или картинка — лежит на своих координатах и
знает, какое поле данных показывает. По описанию строят два отрисовщика:
`web/ov/render.js` — страницу для OBS и предпросмотр редактора,
`core/ui/ov_paint.py` — прозрачные окна поверх iRacing. Картинка везде
одинаковая, а редактор меняет только описание.

Здесь — описания по умолчанию (оформление iRChamps: панели цвета меню сайта,
оранжевое свечение из угла, шапка в две строки, Exo 2 и Roboto Mono) и хранение
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
    "bg": "#0B0E14", "panel": "#141923F5", "row": "#1A2030", "row_alt": "#1A2130",
    "chip": "#232C3D", "line": "#2C323C",
    "fg": "#EAEEF5", "dim": "#8E8E98", "gapc": "#C3C9D3",
    "live": "#2FD16A", "yellow": "#FFD200", "red": "#FF4D4D", "purple": "#A23CFF",
    "pit": "#FFD200", "pit_row": "#1B2A3A",
}

FONTS = {
    # шрифты программы, лежат внутри (web/ui/fonts): Exo 2 — текст, Roboto Mono — цифры
    "num": "'Roboto Mono',Consolas,monospace",
    "text": "'Exo 2','Segoe UI',sans-serif",
    "ui": "'Exo 2','Segoe UI',system-ui,sans-serif",
}

# Версия оформления. Правки, сохранённые редактором в прежнем оформлении,
# поверх нового не накладываются: от старых умолчаний в них лежит всё
# описание целиком, и новый вид не проступил бы вовсе. Файлы при этом не
# трогаются — они просто не читаются, пока их не пересохранят в новом виде.
STYLE = 2


def _t(id_, text, x, y, w, h, **style):
    """Текстовый элемент."""
    return {"id": id_, "type": "text", "text": text, "x": x, "y": y, "w": w, "h": h, **style}


def _chip(id_, text, x, y, w, h, **style):
    """Чип: плашка с надписью — номер машины."""
    base = {"fill": "$chip", "shape": "round", "radius": 4, "font": "text", "size": 13,
            "weight": 800, "italic": False, "color": "$fg", "align": "center"}
    base.update(style)
    return {"id": id_, "type": "chip", "text": text, "x": x, "y": y, "w": w, "h": h, **base}


def _box(id_, x, y, w, h, **style):
    """Плашка без текста: подсветка строки, черта цвета машины, точка."""
    return {"id": id_, "type": "box", "x": x, "y": y, "w": w, "h": h, **style}


LOGO = "/assets/logo.png"        # знак iRC, 1355x520 (assets/logo-flat.png, из logo-irc-frame.svg)
LOGO_ASPECT = round(1355 / 520, 3)

# Общий вид панелей: цвет меню irchamps.gg, оранжевое свечение из угла,
# рамка в 1 px и оранжевая кромка слева (под жёлтым флагом — жёлтая).
SURFACE = {"fill": "$panel", "glow": "$accent", "border": "$line", "edge": 3}


def _frame(**extra):
    return {"radius": 6, "shadow": True, "surface": dict(SURFACE), **extra}


def _caption(id_, text, x, y, w, color="$accent", align="left"):
    """Мелкая подпись капсом над значением."""
    return _t(id_, text, x, y, w, 13, font="text", size=10.5, weight=800, color=color,
              spacing=1.8, upper=True, align=align)


def _head(caption, title, width, right_caption="", right_value="", h=56, logo=True,
          title_states=None, right_w=130):
    """Шапка в две строки: мелкая оранжевая подпись сверху, крупное значение
    под ней; справа — такая же пара (круг, количество). Внизу — линия."""
    els = []
    x0 = 14
    if logo:
        # высота знака — от высоты шапки, ширина — по пропорции (fill_h)
        pad = 14
        lw = round((h - 2 * pad) * LOGO_ASPECT)
        els.append({"id": "logo", "type": "image", "src": LOGO, "fit": "contain",
                    "x": 12, "y": pad, "w": lw, "h": h - 2 * pad,
                    "fill_h": True, "pad": pad, "aspect": LOGO_ASPECT})
        x0 = 12 + lw + 12
    rw = right_w if (right_caption or right_value) else 0
    tw = width - x0 - rw - 14
    els.append(_caption("caption", caption, x0, h / 2 - 16, tw))
    els.append(_t("title", title, x0, h / 2 - 3, tw, 22, font="text", size=18, weight=800,
                  color="#FFFFFF", spacing=.5, upper=True, states=title_states or {}))
    if rw:
        if right_caption:
            els.append(_caption("rcap", right_caption, width - rw - 12, h / 2 - 16, rw,
                                color="$dim", align="right"))
        if right_value:
            els.append(_t("rval", right_value, width - rw - 12, h / 2 - 4, rw, 22, font="num",
                          size=19, weight=700, color="#FFFFFF", align="right"))
    els.append(_box("line", 0, h - 1, width, 1, fill="$line"))
    return {"height": h, "bg": {"fill": "transparent"}, "elements": els}


FLAG_TITLE = {"yellow": {"color": "$yellow"}, "green": {"color": "$live"}}


def _tile(i, caption, value, arrow=False):
    """Плитка погоды: подпись сверху, значение снизу. 3 в ряд, 2 ряда."""
    x, y = 16 + (i % 3) * 146, 32 + (i // 3) * 58
    els = [_box(f"tile{i}", x, y, 136, 52, fill="#0B0E14CC", shape="round", radius=4),
           _caption(f"tcap{i}", caption, x + 10, y + 8, 118)]
    vx = x + 10
    if arrow:
        els.append(_t(f"tarr{i}", "{wind_arrow}", vx, y + 22, 18, 24, font=ARROWS, size=16,
                      color="$accent"))
        vx += 20
    els.append(_t(f"tval{i}", value, vx, y + 22, x + 128 - vx, 24, font="text", size=18,
                  weight=800, color="#FFFFFF"))
    return els
ARROWS = "'Segoe UI Symbol','Segoe UI',sans-serif"
TW = 440                         # ширина башни


DEFAULTS: dict[str, dict] = {
    # ---------------- башня позиций ----------------
    "tower": {
        "title": "Башня позиций",
        "source": "/api/broadcast/tower",
        "width": TW,
        "enter": "left",
        "frame": _frame(wave=True),
        "header": _head("{$.track_short}", "{$.status}", TW, "КРУГ",
                        "{$.lap}{$.laps_total|slash}", title_states=FLAG_TITLE),
        # плашка флага под шапкой: «пейс-кар на трассе», «рестарт»
        "flagbar": {"height": 26, "margin": 6, "radius": 4},
        "body": {
            "type": "list", "items": "rows", "key": "car_number", "pad": 6,
            "empty": "ОЖИДАНИЕ СЕССИИ",
            # «топ-10 + окно»: после первой десятки — разделитель, ниже окно у камеры
            "sep": {"after": "$.sep_after", "height": 22,
                    "text": "позиции {$.win_from}–{$.win_to}{$.win_note}"},
            "item": {
                "height": 32,
                "bg": {"fill": "transparent"},
                "elements": [
                    _box("hl", 6, 1, TW - 12, 30, fill="#FF5A1F33", shape="round", radius=4,
                         when="focus"),
                    _t("pos", "{position}", 6, 0, 28, 32, font="text", size=15, weight=800,
                       color="$dim", align="right", states={"leader": {"color": "$accent"}}),
                    # стрелки — шрифтом с этими значками: у Exo 2 их нет, а окна
                    # поверх сима (ov_paint) другой шрифт за глифом не идут
                    _t("up", "▲", 36, 0, 12, 32, font=ARROWS, size=9, color="$live",
                       align="center", when="gained"),
                    _t("down", "▼", 36, 0, 12, 32, font=ARROWS, size=9, color="$red",
                       align="center", when="lost"),
                    _box("bar", 50, 9, 4, 14, fill="@color"),
                    _chip("num", "{car_number}", 60, 5, 40, 22),
                    _t("name", "{name|surname}", 108, 0, 128, 32, font="text", size=16,
                       weight=700, color="$fg", spacing=.6, upper=True,
                       states={"in_pit": {"color": "$dim"}}),
                    _box("best", 240, 12, 8, 8, fill="$purple", shape="pill", when="fastest"),
                    _chip("pit", "PIT", 252, 8, 30, 16, fill="$accent", radius=3, size=10,
                          color="#000000", spacing=.8, when="in_pit"),
                    _t("make", "{car|make}", 276, 0, 74, 32, font="text", size=11, weight=700,
                       color="$dim", spacing=1, align="right"),
                    _t("gap", "{gap|gap3}", 352, 0, 80, 32, font="num", size=14, weight=500,
                       color="$gapc", align="right", states={"in_pit": {"color": "$dim"}}),
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
        "frame": _frame(),
        "header": _head("{$.caption}", "{$.title}", 880, "{$.source|source_label}", right_w=340),
        "body": {
            "type": "grid", "items": "rows", "key": "car_number", "pad": 10,
            "columns": 2, "page_size": 10, "page_seconds": 15, "gap": 8,
            "empty": "ОЖИДАНИЕ КВАЛИФИКАЦИИ",
            "item": {
                "height": 58,
                "bg": {"fill": "#FFFFFF0A", "shape": "round", "radius": 6},
                "elements": [
                    _t("pos", "{position}", 8, 0, 40, 58, font="text", size=28, weight=800,
                       color="#FFFFFF", align="right", states={"leader": {"color": "$accent"}}),
                    _chip("num", "{car_number}", 58, 17, 42, 24, fill="#2A3346", size=14),
                    _box("bar", 108, 15, 4, 28, fill="@color"),
                    _t("name", "{name|surname}", 120, 7, 190, 26, font="text", size=19,
                       weight=800, color="$fg", spacing=.4, upper=True),
                    _t("car", "{car|upper}", 120, 32, 190, 16, font="text", size=11,
                       weight=600, color="$dim", spacing=.8),
                    _t("time", "{time|laptime}", 300, 0, 116, 58, font="num", size=16,
                       weight=500, color="$gapc", align="right", when="!$.show_points",
                       states={"leader": {"color": "$accent2"}}),
                    # со второго этапа — очки и место в чемпионате до этапа
                    _t("time_p", "{time|laptime}", 250, 0, 104, 58, font="num", size=15,
                       weight=500, color="$gapc", align="right", when="$.show_points",
                       states={"leader": {"color": "$accent2"}}),
                    _t("pts", "{pts}", 356, 8, 60, 26, font="num", size=20, weight=700,
                       color="#FFFFFF", align="right", when="$.show_points"),
                    _caption("rank", "P{rank}", 356, 34, 60, align="right") | {"when": "$.show_points"},
                ],
            },
        },
        "footer": {
            "height": 26,
            "bg": {"fill": "transparent"},
            "elements": [
                _box("line", 0, 0, 880, 1, fill="$line"),
                _caption("pages", "места {$.page_from}–{$.page_to} из {$.count} · "
                         "страница {$.page}/{$.pages}", 14, 7, 852, color="$dim",
                         align="right") | {"when": "$.pages>1"},
            ],
        },
    },

    # ---------------- плашка комментаторов ----------------
    "commentators": {
        "title": "Плашка комментаторов",
        "source": "/api/broadcast/commentators",
        "enter": "up",
        "frame": {"radius": 0, "shadow": False},
        "body": {
            "type": "cards", "items": "people", "key": "name", "gap": 10,
            "empty": "",
            # подпись справа от карточек («в эфире») — своя плашка; видна, когда задана
            "tail": {
                "width": 170, "when": "$.caption",
                # подпись выезжает справа, вслед за самой плашкой
                "enter": {"from": "right", "shift": 60, "delay": 0.22, "duration": 0.45},
                "bg": {"fill": "$accent", "shape": "round", "radius": 6},
                "elements": [
                    _t("cap", "{$.caption|upper}", 12, 0, 146, 60, font="text", size=16,
                       weight=800, color="#000000", spacing=1.2, align="center"),
                ],
            },
            "item": {
                "width": 300, "height": 60,
                "bg": {"fill": "$panel", "shape": "round", "radius": 6},
                "elements": [
                    _box("edge", 0, 0, 3, 60, fill="$accent"),
                    _caption("role", "{role}", 18, 12, 270),
                    _t("name", "{name|upper}", 18, 25, 270, 26, font="text", size=21,
                       weight=800, color="#FFFFFF", spacing=.4),
                ],
            },
        },
    },

    # ---------------- борьба пары ----------------
    "delta": {
        "title": "Дельта пилотов",
        "source": "/api/broadcast/delta",
        "width": 360,
        "enter": "right",
        "frame": _frame(),
        "header": _head("борьба за", "{$.lead.position}-е место", 360, logo=False),
        "body": {
            "type": "single", "height": 132, "ready": "ready",
            "empty": "ПАРА НЕ ВЫБРАНА",
            "bg": {"fill": "transparent"},
            "elements": [
                _t("lead_pos", "{lead.position}", 6, 6, 28, 30, font="text", size=15,
                   weight=800, color="$dim", align="right"),
                _box("lead_bar", 42, 14, 4, 14, fill="@lead.color"),
                _chip("lead_num", "{lead.car_number}", 52, 10, 40, 22),
                _t("lead_name", "{lead.name|surname}", 100, 6, 250, 30, font="text", size=16,
                   weight=700, color="$fg", upper=True),
                _t("gap", "{gap|fixed3}", 14, 38, 150, 56, font="num", size=44, weight=700,
                   color="#FFFFFF"),
                _t("trend", "{trend|trend}", 164, 38, 186, 56, font="text", size=12,
                   weight=800, color="$dim", spacing=1.2,
                   states={"closing": {"color": "$live"}, "growing": {"color": "$red"}}),
                _t("chase_pos", "{chase.position}", 6, 96, 28, 30, font="text", size=15,
                   weight=800, color="$dim", align="right"),
                _box("chase_bar", 42, 104, 4, 14, fill="@chase.color"),
                _chip("chase_num", "{chase.car_number}", 52, 100, 40, 22),
                _t("chase_name", "{chase.name|surname}", 100, 96, 250, 30, font="text",
                   size=16, weight=700, color="$fg", upper=True),
            ],
        },
    },

    # ---------------- пит-стопы ----------------
    "pits": {
        "title": "Пит-стопы",
        "source": "/api/broadcast/pits",
        "width": 420,
        "enter": "right",
        "frame": _frame(),
        "header": _head("пит-лейн", "{$.title}", 420, "стопов", "", logo=False),
        "body": {
            "type": "list", "items": "rows", "key": "car_number", "pad": 6,
            "empty": "ПИТ-СТОПОВ ПОКА НЕ БЫЛО",
            "item": {
                "height": 30,
                "bg": {"fill": "transparent"},
                "elements": [
                    _box("hl", 6, 1, 408, 28, fill="#FFD2001F", shape="round", radius=4,
                         when="in_pit"),
                    _t("pos", "{position}", 6, 0, 26, 30, font="text", size=14, weight=800,
                       color="$dim", align="right"),
                    _box("bar", 40, 8, 4, 14, fill="@color"),
                    _chip("num", "{car_number}", 50, 4, 40, 22),
                    _t("name", "{name|surname}", 98, 0, 150, 30, font="text", size=15,
                       weight=700, color="$fg", upper=True),
                    _chip("pit", "В БОКСАХ", 250, 7, 74, 16, fill="$accent", radius=3, size=10,
                          color="#000000", spacing=.8, when="in_pit"),
                    _t("last", "{last.duration|sec1}", 250, 0, 74, 30, font="num", size=13,
                       color="$gapc", align="right", when="!in_pit"),
                    _t("count", "{count}", 336, 0, 72, 30, font="num", size=17, weight=700,
                       color="#FFFFFF", align="right"),
                ],
            },
        },
    },
    # ---------------- трасса и погода ----------------
    "weather": {
        "title": "Трасса и погода",
        "source": "/api/broadcast/weather",
        "width": 460,
        "enter": "right",
        "frame": _frame(),
        "header": _head("{$.caption}", "Трасса и погода", 460, logo=False),
        "body": {
            "type": "single", "height": 206, "ready": "track",
            "empty": "НЕТ ДАННЫХ СИМА",
            "bg": {"fill": "transparent"},
            "elements": [
                _t("line", "{track_line}", 16, 8, 428, 18, font="text", size=12, weight=600,
                   color="$gapc"),
                *[el for i, (cap, val, arrow) in enumerate([
                    ("воздух", "{air}", False), ("трасса", "{track_temp}", False),
                    ("ветер · {wind_from}", "{wind}", True), ("влажность", "{humidity}", False),
                    ("небо", "{skies}", False), ("время", "{time_of_day}", False)])
                  for el in _tile(i, cap, val, arrow)],
                _caption("stats", "смен лидера {lead_changes} · жёлтых кругов {caution_laps} · "
                         "пит-стопов {pit_stops}", 16, 152, 428, color="$dim"),
                _caption("best_cap", "лучший круг", 16, 176, 120) | {"when": "best_time"},
                _t("best_time", "{best_time|laptime}", 120, 170, 100, 24, font="num", size=16,
                   weight=700, color="#C38BFF", when="best_time"),
                _t("best_who", "{best_who}", 226, 170, 218, 24, font="text", size=13,
                   weight=700, color="$fg", upper=True, when="best_time"),
            ],
        },
    },

    # ---------------- события гонки ----------------
    "events": {
        "title": "События гонки",
        "source": "/api/broadcast/events",
        "width": 520,
        "enter": "left",
        "frame": _frame(),
        "header": _head("рейс-контроль", "События", 520, logo=False),
        "body": {
            "type": "list", "items": "rows", "key": "key", "pad": 6,
            "empty": "СОБЫТИЙ НЕТ",
            "item": {
                "height": 30,
                "bg": {"fill": "transparent"},
                "elements": [
                    _box("hl", 6, 1, 508, 28, fill="#FFFFFF14", shape="round", radius=4,
                         when="fresh"),
                    _t("lap", "{lap_text}", 12, 0, 50, 30, font="num", size=13, color="$dim"),
                    _box("bar", 64, 8, 4, 14, fill="@color", when="car_number"),
                    _chip("num", "{car_number}", 74, 4, 40, 22, when="car_number"),
                    _t("name", "{name|surname}", 122, 0, 230, 30, font="text", size=15,
                       weight=700, color="$fg", upper=True, when="car_number"),
                    _t("why", "{why}", 64, 0, 290, 30, font="text", size=14, weight=700,
                       color="$fg", when="!car_number"),
                    _chip("tag", "{tag}", 360, 6, 148, 18, fill="@tag_color", color="@tag_fg",
                          radius=3, size=10.5, spacing=1, upper=True),
                ],
            },
        },
    },

    # ---------------- новый лучший круг ----------------
    "bestlap": {
        "title": "Лучший круг",
        "source": "/api/broadcast/bestlap",
        "width": 420,
        "enter": "left",
        "frame": _frame() | {"surface": dict(SURFACE, glow="$purple", edge_color="$purple")},
        "header": _head("новый лучший круг", "{$.title}", 420, logo=False),
        "body": {
            "type": "single", "height": 46, "ready": "ready",
            "empty": "",
            "bg": {"fill": "transparent"},
            "elements": [
                _box("bar", 14, 15, 4, 16, fill="@color"),
                _chip("num", "{car_number}", 24, 12, 40, 22),
                _t("name", "{name|surname}", 72, 0, 180, 46, font="text", size=16, weight=700,
                   color="$fg", upper=True),
                _t("lap", "К {lap}", 252, 0, 50, 46, font="num", size=13, color="$dim",
                   align="right"),
                _t("time", "{time|laptime}", 304, 0, 104, 46, font="num", size=18, weight=700,
                   color="#C38BFF", align="right"),
            ],
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
            over = json.loads(f.read_text("utf-8"))
            if isinstance(over, dict) and over.get("style") == STYLE:
                spec = _merge(spec, over)
                version = int(f.stat().st_mtime_ns // 1_000_000)
            # правки прежнего оформления не накладываем (см. STYLE) — файл не трогаем
        except (OSError, ValueError):
            pass                        # битый файл — показываем умолчание
    spec["id"] = overlay_id
    spec["version"] = version
    spec["style"] = STYLE
    return spec


def save(overlay_id: str, spec: dict) -> dict:
    if overlay_id not in DEFAULTS:
        return {"ok": False, "error": "unknown_overlay"}
    clean = {k: v for k, v in (spec or {}).items() if k not in ("id", "version")}
    clean["style"] = STYLE              # сохранено в нынешнем оформлении
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
