"""Отрисовка оверлея по JSON-описанию в картинку с прозрачностью (PIL).

Двойник `web/ov/render.js` для окон поверх iRacing. Страницу для OBS и
предпросмотр редактора по-прежнему строит HTML, а окна поверх сима рисует
этот модуль: WebView2 не умеет окно, прозрачное до рабочего стола (проверено
пиксельным замером — при любых флагах под оверлеем оставалась подложка).
Здесь получается картинка RGBA, её выводит слоистое окно Windows: вокруг
плашек — настоящая пустота, в ней виден сим.

Смысл описания тот же, что в render.js: шапка, тело (список, решётка
страницами, карточки с хвостом, одиночный блок), подвал; элементы — текст,
чип, плашка, картинка; шаблоны {поле|формат}, условия when, состояния строк.
Меняешь одно — сверяй с другим.

Строки списков рисуются плитками и кэшируются: при обгоне плитка переезжает
на новое место плавно, как в HTML, а перерисовываются только изменившиеся.
"""
from __future__ import annotations

import base64
import io
import math
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

CLASS = {"GT3": "#FF5A1F", "PRO": "#FF5A1F", "GT4": "#3B82F6", "AM": "#3B82F6",
         "SILVER": "#94A3B8", "GOLD": "#E3B341"}
FLAG = {"green": "#2EA043", "yellow": "#E3B341", "red": "#DA3633", "white": "#E6EDF3",
        "checkered": "#E6EDF3", "none": None}
SLANT = 0.22
PAD = 14                     # поле вокруг оверлея — под тень, как на странице
ITALIC_SKEW = 0.2            # наклон курсива, которого нет у шрифта (как у браузера)
EMPTY_FONT = "'Bahnschrift Condensed','Arial Narrow',sans-serif"
ROW_MOVE_S = 0.45            # переезд строки при обгоне — как transition у .item
WAVE_S = 1.1                 # волна флага сверху вниз — как WAVE_MS в render.js
LIT_S = 0.55                 # вспышка строки, когда волна через неё проходит
TONE = {"yellow": "$yellow", "green": "$live"}


def bezier(x1: float, y1: float, x2: float, y2: float):
    """Кривая cubic-bezier из CSS: те же движения, что у страницы."""
    def f(t: float) -> float:
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(22):
            u = (lo + hi) / 2
            x = 3 * (1 - u) ** 2 * u * x1 + 3 * (1 - u) * u * u * x2 + u ** 3
            lo, hi = (u, hi) if x < t else (lo, u)
        u = (lo + hi) / 2
        return 3 * (1 - u) ** 2 * u * y1 + 3 * (1 - u) * u * u * y2 + u ** 3
    return f


ROW_EASE = bezier(.3, .7, .2, 1)
TAIL_EASE = bezier(.2, .8, .2, 1)
SHOWN_GAP_S = 1.0            # кадров не было дольше — окно только что показали (в эфире они идут чаще)


# ---------- данные и шаблоны (как в render.js) ----------

def get(obj, path):
    for k in str(path).split("."):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and k.isdigit():
            i = int(k)
            obj = obj[i] if i < len(obj) else None
        else:
            return None
    return obj


def num(v):
    """Число из значения или None — как JS-сравнение с undefined."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _n(v) -> float:
    x = num(v)
    return 0.0 if x is None or not math.isfinite(x) else x


def js_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isfinite(v) and v == int(v):
            return str(int(v))
        return repr(v)
    if isinstance(v, (list, dict)):
        return ""
    return str(v)


def truthy(v) -> bool:
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    return True                  # в JS пустой список и объект — истина


def fixed(v, n: int) -> str:
    return f"{float(v):.{n}f}"


def surname(v) -> str:
    p = str(v or "").split()
    if not p:
        return ""
    t = p[-1]
    if len(t) <= 2 and len(p) > 1:
        t = p[0]
    return t.upper()


def laptime(t) -> str:
    t = num(t)
    if not t or t <= 0:
        return "—"
    m = int(t // 60)
    return f"{m}:{t - m * 60:06.3f}" if m else f"{t:.3f}"


def _gap3(v, it, _r):
    """Отрыв: «ЛИДЕР», «+1 КРУГ», «+1:02.345», «+0.412» — как gap3 в render.js."""
    if isinstance(it, dict) and it.get("qual"):
        b = num(it.get("best_lap"))
        if not b or b <= 0:
            return "—"
        return laptime(b) if it.get("position") == 1 else f"+{num(v) or 0:.3f}"
    if isinstance(it, dict) and it.get("position") == 1:
        return "ЛИДЕР"
    ld = num(it.get("laps_down")) if isinstance(it, dict) else None
    if ld and ld > 0:
        n = int(ld)
        return f"+{n} " + ("КРУГ" if n == 1 else "КРУГА" if n < 5 else "КРУГОВ")
    x = num(v)
    if x is None or x <= 0:
        return "—"
    m = int(x // 60)
    return "+" + (f"{m}:{x - m * 60:06.3f}" if m else f"{x:.3f}")


def _gap(v, it, _r):
    if isinstance(it, dict) and it.get("position") == 1:
        return "ЛИДЕР"
    x = num(v)
    return "+" + fixed(x, 1) if x is not None and x > 0 else "—"


def _trend(v, *_):
    x = num(v)
    if x is not None and x < 0:
        return "▼ СОКРАЩАЕТСЯ"
    if x is not None and x > 0:
        return "▲ РАСТЁТ"
    return "— ДЕРЖИТСЯ"


FMT = {
    "surname": lambda v, *_: surname(v),
    "upper": lambda v, *_: js_str(v).upper(),
    "gap": _gap,
    "gap3": _gap3,
    "make": lambda v, *_: (js_str(v).split() or [""])[0].split("-")[0].upper(),   # «Mercedes-AMG GT3» -> MERCEDES
    "fixed1": lambda v, *_: "—" if num(v) is None else fixed(v, 1),
    "fixed3": lambda v, *_: "—" if num(v) is None else fixed(v, 3),
    "plus3": lambda v, *_: "+" + fixed(v, 3) if num(v) else "",
    "laptime": lambda v, *_: laptime(v),
    "sec1": lambda v, *_: "—" if num(v) is None else fixed(v, 1) + "с",
    "slash": lambda v, *_: "/" + js_str(v) if truthy(v) else "",
    "trend": _trend,
    "source_label": lambda v, *_: ("ПО РЕЗУЛЬТАТАМ КВАЛИФИКАЦИИ" if v == "qualifying"
                                   else "КВАЛИФИКАЦИИ НЕТ — ТЕКУЩИЙ ПОРЯДОК"),
}
TPL = re.compile(r"\{([^}|]+)(?:\|([^}]+))?\}")


def tpl(text, item, rootd) -> str:
    def sub(m):
        path, f = m.group(1), m.group(2)
        from_root = path.startswith("$.")
        v = get(rootd if from_root else item, path[2:] if from_root else path)
        if f and f in FMT:
            try:
                return FMT[f](v, item, rootd)
            except (TypeError, ValueError):
                return ""
        return js_str(v)
    return TPL.sub(sub, "" if text is None else str(text))


COND = re.compile(r"^(\$\.)?([\w.]+)\s*(>|<|==)\s*(.+)$")


def cond(c, item, rootd, states=None) -> bool:
    """Условия: «leader», «!in_pit», «$.pages>1», «count>0»."""
    if not c:
        return True
    c = str(c)
    neg = c.startswith("!")
    if neg:
        c = c[1:]
    m = COND.match(c)
    if m:
        v = get(rootd if m.group(1) else item, m.group(2))
        raw = m.group(4)
        r = num(raw)
        op = m.group(3)
        if op == "==":
            ok = (num(v) == r) if (r is not None and num(v) is not None) else js_str(v) == raw
        else:
            a = num(v)
            if a is None or r is None:
                ok = False
            else:
                ok = a > r if op == ">" else a < r
    elif states and c in states:
        ok = bool(states[c])
    else:
        ok = truthy(get(rootd if c.startswith("$.") else item,
                        c[2:] if c.startswith("$.") else c))
    return ok != neg


def item_states(item, rootd) -> dict:
    item = item if isinstance(item, dict) else {}
    trend = num(item.get("trend"))
    focus = rootd.get("focus_car")
    return {
        "leader": item.get("position") == 1,
        "in_pit": bool(item.get("on_pit") or item.get("in_pit_stall") or item.get("in_pit")),
        "focus": bool(item.get("focus") or (focus and item.get("car_number") == focus)),
        "closing": trend is not None and trend < 0,
        "growing": trend is not None and trend > 0,
        "gained": item.get("moved") == "up",
        "lost": item.get("moved") == "down",
        "fastest": bool(item.get("fastest")),
    }


def root_states(d) -> dict:
    """Состояния шапки и подвала — по флагу: заголовок желтеет или зеленеет."""
    tone = (d or {}).get("flag_tone")
    return {"yellow": tone == "yellow", "green": tone == "green"}


# ---------- цвета ----------

_COLORS: dict = {}


def parse_color(v):
    """CSS-цвет в (r, g, b, a) или None для прозрачного."""
    if not isinstance(v, str):
        return None
    if v in _COLORS:
        return _COLORS[v]
    s = v.strip().lower()
    out = None
    if s and s not in ("transparent", "none"):
        m = re.match(r"rgba?\(([^)]*)\)", s)
        try:
            if s.startswith("#"):
                h = s[1:]
                if len(h) in (3, 4):
                    h = "".join(ch * 2 for ch in h)
                if len(h) == 6:
                    h += "ff"
                if len(h) == 8:
                    out = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
            elif m:
                parts = [p for p in re.split(r"[\s,/]+", m.group(1)) if p]
                rgb = [int(float(p[:-1]) * 2.55) if p.endswith("%") else int(float(p))
                       for p in parts[:3]]
                a = parts[3] if len(parts) > 3 else "1"
                a = float(a[:-1]) / 100 if a.endswith("%") else float(a)
                out = (*[max(0, min(255, c)) for c in rgb], int(round(max(0, min(1, a)) * 255)))
            else:
                from PIL import ImageColor
                out = (*ImageColor.getrgb(s)[:3], 255)
        except (ValueError, IndexError):
            out = None
    _COLORS[v] = out
    return out


# ---------- шрифты ----------

GENERIC = {"sans-serif": "segoe ui", "system-ui": "segoe ui", "serif": "times new roman",
           "monospace": "consolas", "-apple-system": "segoe ui"}
WEIGHT_NAMES = {"thin": 100, "extralight": 200, "light": 300, "semilight": 350,
                "regular": 400, "normal": 400, "medium": 500, "semibold": 600,
                "demibold": 600, "bold": 700, "extrabold": 800, "black": 900}
WIDTH_NAMES = {"condensed": 75, "semicondensed": 87.5, "narrow": 75}


def _bundled_font_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    return [here.parents[2] / "assets" / "fonts",
            here.parents[2] / "web" / "ui" / "fonts",       # Exo 2 и Roboto Mono программы
            Path(getattr(sys, "_MEIPASS", ".")) / "assets" / "fonts",
            Path(sys.executable).parent / "_internal" / "assets" / "fonts"]


def _system_fonts() -> dict[str, str]:
    """Имя начертания (в нижнем регистре) -> файл: реестр Windows и бандл."""
    idx: dict[str, str] = {}
    windir = os.environ.get("WINDIR", r"C:\Windows")
    try:
        import winreg
        sub = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(val, str) or not val:
                    continue
                path = val if os.path.isabs(val) else os.path.join(windir, "Fonts", val)
                base = re.sub(r"\s*\((truetype|opentype|all res)\)\s*$", "", name, flags=re.I)
                for part in base.split(" & "):
                    idx.setdefault(part.strip().lower(), path)
    except Exception:
        pass
    for d in _bundled_font_dirs():
        if not d.is_dir():
            continue
        for f in d.glob("*.ttf"):
            try:
                fam, style = ImageFont.truetype(str(f), 10).getname()
            except Exception:
                continue
            key = fam if (style or "").lower() in ("", "regular") else f"{fam} {style}"
            idx.setdefault(key.lower(), str(f))
    return idx


class Fonts:
    """CSS-стек шрифтов -> шрифт PIL того же начертания.

    Узкий DIN из Windows (Bahnschrift) — вариативный: «SemiBold Condensed» —
    это не отдельный файл, а точка на осях толщины и ширины. Её и ставим.
    Шрифты, загруженные в редакторе (font_faces), берутся с сервера.
    """

    _index: dict[str, str] | None = None

    def __init__(self, fetch) -> None:
        self.fetch = fetch
        self.faces: dict[str, bytes] = {}
        self._faces_src: dict[str, bytes | None] = {}
        self._cache: dict = {}

    @classmethod
    def index(cls) -> dict[str, str]:
        if cls._index is None:
            cls._index = _system_fonts()
        return cls._index

    def set_faces(self, faces) -> None:
        self.faces = {}
        for f in faces or []:
            fam, src = str(f.get("family") or "").strip().lower(), f.get("src")
            if not fam or not src:
                continue
            if src not in self._faces_src:
                self._faces_src[src] = self.fetch(src)
            if self._faces_src[src]:
                self.faces[fam] = self._faces_src[src]
        self._cache.clear()

    def get(self, stack: str, size: float, weight: int, italic: bool):
        """(шрифт, нужен ли искусственный курсив, толщина обводки-«жирности»)."""
        key = (stack, round(size, 2), weight, italic)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._resolve(stack, max(1, int(round(size))), weight, italic)
            self._cache[key] = hit
        return hit

    def _resolve(self, stack, size, weight, italic):
        fams = [p.strip().strip("'\"").strip() for p in str(stack or "").split(",")]
        for fam in [f for f in fams if f] + ["segoe ui"]:
            low = GENERIC.get(fam.lower(), fam.lower())
            got = self._try(low, size, weight, italic)
            if got:
                return got
        return ImageFont.load_default(size), italic, 0

    def _try(self, fam, size, weight, italic):
        if fam in self.faces:
            f = ImageFont.truetype(io.BytesIO(self.faces[fam]), size)
            return f, italic, self._stroke(size) if weight >= 600 else 0
        idx = self.index()
        # отдельные файлы начертаний: «Arial Narrow Bold Italic», «Segoe UI Semibold»
        if fam in idx:
            wn = ([] if weight < 500 else
                  ["medium"] if weight < 600 else
                  ["semibold", "demibold", "bold"] if weight < 700 else ["bold", "semibold"])
            for w in wn + [""]:
                for it in (["italic", "oblique"] if italic else []) + [""]:
                    name = " ".join(p for p in (fam, w, it) if p)
                    if name in idx:
                        f = self._load(idx[name], size, weight, None)
                        if f:
                            bold_ok = bool(w) or weight < 600 or self._variable(f)
                            return f, italic and not it, 0 if bold_ok else self._stroke(size)
        # вариативный шрифт: «Bahnschrift SemiBold Condensed» = bahnschrift.ttf + оси
        for name in sorted(idx, key=len, reverse=True):
            if fam.startswith(name + " "):
                f = self._load(idx[name], size, weight, fam[len(name):].strip())
                if f and self._variable(f):
                    return f, italic, 0
        return None

    @staticmethod
    def _stroke(size) -> int:
        return max(1, int(size / 24 + 0.5))

    @staticmethod
    def _variable(f) -> bool:
        try:
            return bool(f.get_variation_axes())
        except Exception:
            return False

    def _load(self, path, size, weight, style):
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            return None
        try:
            axes = f.get_variation_axes()
        except Exception:
            axes = None
        if axes:
            toks = (style or "").lower().replace("-", " ").split()
            wght = next((WEIGHT_NAMES[t] for t in toks if t in WEIGHT_NAMES), weight)
            wdth = next((WIDTH_NAMES[t] for t in toks if t in WIDTH_NAMES), None)
            vals = []
            for a in axes:
                name = (a.get("name") or b"").lower()
                lo, hi, dflt = a.get("minimum"), a.get("maximum"), a.get("default")
                if name in (b"weight", b"wght"):
                    v = wght
                elif name in (b"width", b"wdth"):
                    v = wdth if wdth is not None else dflt
                else:
                    v = dflt
                vals.append(max(lo, min(hi, v)))
            try:
                f.set_variation_by_axes(vals)
            except Exception:
                pass
        elif style:
            return None                   # у обычного шрифта нет «SemiBold Condensed»
        return f


# ---------- рисование ----------

def _paste(cv: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """alpha_composite с обрезкой по краю холста."""
    W, H = cv.size
    lw, lh = layer.size
    sx, sy = max(0, -x), max(0, -y)
    ex, ey = min(lw, W - x), min(lh, H - y)
    if ex <= sx or ey <= sy:
        return
    if sx or sy or ex < lw or ey < lh:
        layer = layer.crop((sx, sy, ex, ey))
    cv.alpha_composite(layer, (x + sx, y + sy))


def _fill(cv, x, y, w, h, rgba, mask=None, opacity=1.0) -> None:
    if rgba is None or w <= 0 or h <= 0:
        return
    a = rgba[3] / 255 * opacity
    if a <= 0:
        return
    layer = Image.new("RGBA", (w, h), (*rgba[:3], 255))
    if mask is not None:
        layer.putalpha(mask if a >= 1 else mask.point(lambda p: int(p * a + .5)))
    elif a < 1:
        layer.putalpha(int(255 * a + .5))
    _paste(cv, layer, x, y)


def to_bgra(img: Image.Image) -> bytes:
    """RGBA -> BGRA с предумноженной альфой: так её ждёт UpdateLayeredWindow."""
    return img.tobytes("raw", "BGRa")


class Painter:
    """Отрисовщик одного оверлея."""

    def __init__(self, fetch) -> None:
        self.fetch = fetch              # fetch(путь) -> bytes | None
        self.fonts = Fonts(fetch)
        self.spec: dict | None = None
        self.scale = 1.0
        self._images: dict = {}
        self._shapes: dict = {}
        self._shadows: dict = {}
        self._tiles: dict = {}          # ключ строки -> (подпись, картинка)
        self._pos: dict = {}            # ключ строки -> движение по экрану
        self._page, self._page_at = 0, None
        self._width = None
        self._wave_id = None            # последний показанный выброс флага
        self._last_frame = None         # когда рисовали прошлый кадр
        self._base = None               # (данные, ключ, картинка) — неподвижная часть кадра
        self._padded: dict = {}         # холст с полем и тенью под размер кадра
        self._elems: dict = {}          # готовые картинки элементов строк
        self._tail_on = False
        self._tail_t0 = None            # выезд подписи после карточек
        self._wave = None               # {"t0", "tone"} — идущая волна

    # ---------- настройка ----------

    def set_spec(self, spec: dict) -> None:
        self.spec = spec
        self.fonts.set_faces(spec.get("font_faces"))
        self._reset()

    def set_scale(self, scale: float) -> None:
        if abs(scale - self.scale) > 1e-3:
            self.scale = scale
            self._reset()

    def _reset(self) -> None:
        self._base = None
        self._elems.clear()
        self._padded.clear()
        self._tiles.clear()
        self._pos.clear()
        self._shapes.clear()
        self._shadows.clear()
        self._images = {k: v for k, v in self._images.items() if len(k) == 1}

    def px(self, v: float) -> int:
        return int(math.floor(v * self.scale + 0.5))

    def color(self, v):
        if isinstance(v, str) and v.startswith("$"):
            v = (self.spec.get("palette") or {}).get(v[1:]) or v
        return parse_color(v)

    def font_stack(self, key) -> str:
        fonts = self.spec.get("fonts") or {}
        if not key:
            return fonts.get("text") or EMPTY_FONT
        return fonts.get(key) or key

    # ---------- элементы ----------

    def resolve(self, e, item, rootd, states, ch):
        """Элемент с подставленными данными — или None, если он скрыт условием.
        Результат хешируется: по нему плитка строки понимает, что менять нечего."""
        if not isinstance(e, dict) or not cond(e.get("when"), item, rootd, states):
            return None
        over = {}
        for k, v in (e.get("states") or {}).items():
            if states and states.get(k) and isinstance(v, dict):
                over.update(v)
        x, y, w, h = (_n(e.get(k)) for k in "xywh")
        if e.get("fill_h") and ch:
            pad = _n(e.get("pad"))
            hh = max(1.0, ch - 2 * pad)
            y, h = pad, hh
            if e.get("aspect"):
                w = round(hh * _n(e.get("aspect")))
        t = e.get("type")
        op = e.get("opacity")
        r = {"type": t, "box": (x, y, w, h), "opacity": 1.0 if op is None else _n(op)}
        if t in ("chip", "box"):
            fill = over.get("fill") or e.get("fill")
            if fill == "class":
                cls = get(item, e["fill_from"]) if e.get("fill_from") else get(item, "class_name")
                fill = CLASS.get(str(cls or "").upper(), "#5A6B85")
            elif isinstance(fill, str) and fill.startswith("@"):      # из поля строки: «@color»
                fill = get(item, fill[1:]) or e.get("fill_default") or "#5A6B85"
            r.update(fill=self.color(fill), shape=e.get("shape"), radius=e.get("radius"),
                     sh=_n(e.get("h")),
                     border=self.color(e.get("border")) if e.get("border") else None)
        if t in ("text", "chip"):
            txt = tpl(e.get("text"), item, rootd)
            col = over.get("color") or e.get("color") or "$fg"
            if isinstance(col, str) and col.startswith("@"):            # из данных строки
                col = get(item, col[1:]) or "#FFFFFF"
            r.update(text=txt.upper() if e.get("upper") else txt,
                     color=self.color(col),
                     font=self.font_stack(e.get("font")), size=_n(e.get("size")) or 14,
                     weight=int(_n(e.get("weight")) or 400), italic=bool(e.get("italic")),
                     spacing=_n(e.get("spacing")), align=e.get("align") or "left")
        if t == "image":
            r.update(src=str(e.get("src") or ""), fit=e.get("fit") or "contain")
        return tuple(sorted(r.items()))

    def draw(self, cv, res, ox=0.0, oy=0.0) -> None:
        r = dict(res)
        x, y, w, h = r["box"]
        X, Y = self.px(ox + x), self.px(oy + y)
        W, H = self.px(ox + x + w) - X, self.px(oy + y + h) - Y
        if W <= 0 or H <= 0:
            return
        op, t, mask = r["opacity"], r["type"], None
        if t in ("chip", "box"):
            mask = self.shape_mask(W, H, r["shape"], r["radius"], r["sh"])
            _fill(cv, X, Y, W, H, r["fill"], mask, op)
            if r["border"]:
                _fill(cv, X, Y, W, H, r["border"],
                      self.ring_mask(W, H, r["shape"], r["radius"], r["sh"]), op)
        if t in ("text", "chip"):
            self.text(cv, X, Y, W, H, r, op, mask if t == "chip" else None)
        if t == "image":
            self.image(cv, X, Y, W, H, r["src"], r["fit"], op)

    def shape_mask(self, W, H, shape, radius, sh):
        """Маска формы: rect, round, pill, slant, slant-right, slant-left.
        Со сглаживанием — рисуем вчетверо крупнее и уменьшаем."""
        if shape not in ("round", "pill", "slant", "slant-right", "slant-left"):
            return None
        key = ("shape", W, H, shape, radius, sh)
        m = self._shapes.get(key)
        if m is not None:
            return m
        k, s = 4, self.scale
        big = Image.new("L", (W * k, H * k), 0)
        d = ImageDraw.Draw(big)
        if shape in ("round", "pill"):
            r = min(W, H) / 2 if shape == "pill" else min((6 if radius is None else _n(radius)) * s,
                                                           W / 2, H / 2)
            d.rounded_rectangle([0, 0, W * k - 1, H * k - 1], radius=r * k, fill=255)
        else:
            dx = round((sh or 20) * SLANT) * s * k
            Wk, Hk = W * k, H * k
            pts = {"slant": [(dx, 0), (Wk, 0), (Wk - dx, Hk), (0, Hk)],
                   "slant-right": [(0, 0), (Wk, 0), (Wk - dx, Hk), (0, Hk)],
                   "slant-left": [(dx, 0), (Wk, 0), (Wk, Hk), (0, Hk)]}[shape]
            d.polygon(pts, fill=255)
        m = big.resize((W, H), Image.BOX)
        self._shapes[key] = m
        return m

    def ring_mask(self, W, H, shape, radius, sh):
        """Рамка в 1 px внутри фигуры (inset box-shadow из render.js)."""
        key = ("ring", W, H, shape, radius, sh)
        m = self._shapes.get(key)
        if m is not None:
            return m
        b = max(1, round(self.scale))
        ring = Image.new("L", (W, H), 255)
        ImageDraw.Draw(ring).rectangle([b, b, W - 1 - b, H - 1 - b], fill=0)
        shp = self.shape_mask(W, H, shape, radius, sh)
        if shp is not None:
            ring = ImageChops.multiply(ring, shp)
        self._shapes[key] = ring
        return ring

    def text(self, cv, X, Y, W, H, r, op, clip=None) -> None:
        txt = r["text"]
        col = r["color"]
        if not txt or col is None:
            return
        s = self.scale
        font, fake_italic, stroke = self.fonts.get(r["font"], r["size"] * s, r["weight"], r["italic"])
        sp = r["spacing"] * s
        pad = s                                    # padding 0 1px у .t
        avail = W - 2 * pad

        def width(t):
            return font.getlength(t) + sp * len(t) + 2 * stroke

        if width(txt) > avail:                     # многоточие, как text-overflow
            lo, hi = 0, len(txt)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if width(txt[:mid] + "…") <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            txt = txt[:lo] + "…"
        tw = width(txt)
        asc, desc = font.getmetrics()
        base = (H - (asc + desc)) / 2 + asc
        align = r["align"]
        x0 = (W - pad - tw if align == "right" else (W - tw) / 2 if align == "center" else pad) + stroke
        mask = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(mask)
        if sp:
            cx = x0
            for chh in txt:
                d.text((cx, base), chh, font=font, fill=255, anchor="ls",
                       stroke_width=stroke, stroke_fill=255)
                cx += font.getlength(chh) + sp
        else:
            d.text((x0, base), txt, font=font, fill=255, anchor="ls",
                   stroke_width=stroke, stroke_fill=255)
        if fake_italic:
            k = ITALIC_SKEW
            mask = mask.transform((W, H), Image.AFFINE, (1, k, -k * base, 0, 1, 0),
                                  resample=Image.BILINEAR)
        if clip is not None:
            mask = ImageChops.multiply(mask, clip)
        _fill(cv, X, Y, W, H, col, mask, op)

    def _load_image(self, src):
        key = (src,)
        if key not in self._images:
            img = None
            try:
                if src.startswith("data:"):
                    raw = base64.b64decode(src.split(",", 1)[1])
                elif src.lower().split("?")[0].endswith(".svg"):
                    raw = None                     # svg PIL не рисует
                else:
                    raw = self.fetch(src)
                if raw:
                    img = Image.open(io.BytesIO(raw)).convert("RGBA")
            except Exception:
                img = None
            self._images[key] = img
        return self._images[key]

    def image(self, cv, X, Y, W, H, src, fit, op) -> None:
        img = self._load_image(src) if src else None
        if img is None:
            return
        key = (src, W, H, fit)
        out = self._images.get(key)
        if out is None:
            iw, ih = img.size
            if fit == "fill":
                out, ox, oy = img.resize((W, H), Image.LANCZOS), 0, 0
            else:
                k = (max(W / iw, H / ih) if fit == "cover" else
                     1.0 if fit == "none" else
                     min(1.0, W / iw, H / ih) if fit == "scale-down" else min(W / iw, H / ih))
                nw, nh = max(1, round(iw * k)), max(1, round(ih * k))
                sc = img.resize((nw, nh), Image.LANCZOS)
                out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                _paste(out, sc, (W - nw) // 2, (H - nh) // 2)
            self._images[key] = out
        if op < 1:
            out = out.copy()
            out.putalpha(out.getchannel("A").point(lambda p: int(p * op + .5)))
        _paste(cv, out, X, Y)

    def elements(self, cv, elements, item, rootd, states, ox, oy, ch) -> None:
        for e in elements or []:
            res = self.resolve(e, item, rootd, states, ch)
            if res:
                self.draw(cv, res, ox, oy)

    def empty(self, cv, y, width, text) -> None:
        X, Y = 0, self.px(y)
        W, H = self.px(width), self.px(y + 40) - Y
        _fill(cv, X, Y, W, H, parse_color("#141923"))
        r = {"text": text, "color": parse_color("#8B98A9"), "font": EMPTY_FONT, "size": 13,
             "weight": 400, "italic": False, "spacing": 0.5, "align": "center"}
        self.text(cv, X, Y, W, H, r, 1.0)

    def layer(self, cv, d, width, y0, rootd) -> None:
        """Шапка или подвал: фон с формой и элементы по данным корня."""
        h = _n(d.get("height"))
        bg = d.get("bg") or {}
        Y = self.px(y0)
        W, H = self.px(width), self.px(y0 + h) - Y
        _fill(cv, 0, Y, W, H, self.color(bg.get("fill")),
              self.shape_mask(W, H, bg.get("shape"), bg.get("radius"), h) if H > 0 else None)
        self.elements(cv, d.get("elements"), rootd, rootd, root_states(rootd), 0, y0, h)

    # ---------- строки списков ----------

    def tile(self, key, it, w, item, rootd, st, alt):
        """Плитка строки: фон, элементы, кромка. Перерисовывается, только когда
        изменилось то, что в ней видно."""
        over = {}
        for k, v in (it.get("states") or {}).items():
            if st.get(k) and isinstance(v, dict):
                over.update(v)
        bg = it.get("bg") or {}
        h = _n(it.get("height"))
        fill = self.color(over.get("fill") or (bg.get("alt") if alt and bg.get("alt") else bg.get("fill")))
        edge = self.color(over.get("edge")) if over.get("edge") else None
        res = tuple(r for r in (self.resolve(e, item, rootd, st, h)
                                for e in it.get("elements") or []) if r)
        W, H = int(math.ceil(w * self.scale)), int(math.ceil(h * self.scale))
        sig = (W, H, fill, edge, res)
        old = self._tiles.get(key)
        if old and old[0] == sig:
            return old[1]
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _fill(img, 0, 0, W, H, fill, self.shape_mask(W, H, bg.get("shape"), bg.get("radius"), h))
        for r in res:
            self.draw_cached(img, r)
        if edge:
            _fill(img, 0, 0, self.px(3), H, edge)
        self._tiles[key] = (sig, img)
        return img

    def draw_cached(self, cv, res) -> None:
        """Элемент строки из кэша: при новых данных в строке обычно меняется
        один отрыв, а не номер с фамилией — перерисовываем только его. Без
        этого раз в 0,4 с все строки башни рисовались заново (13–30 мс), и
        переезд строк спотыкался."""
        x, y, w, h = dict(res)["box"]
        X, Y = self.px(x), self.px(y)
        W, H = self.px(x + w) - X, self.px(y + h) - Y
        if W <= 0 or H <= 0:
            return
        k = (res, W, H)
        patch = self._elems.get(k)
        if patch is None:
            patch = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            self.draw(patch, res, -x, -y)
            if len(self._elems) > 4000:
                self._elems.clear()
            self._elems[k] = patch
        _paste(cv, patch, X, Y)

    def tail_tile(self, t, h, rootd):
        bg = t.get("bg") or {}
        w = _n(t.get("width")) or 160
        res = tuple(r for r in (self.resolve(e, rootd, rootd, {}, h)
                                for e in t.get("elements") or []) if r)
        W, H = int(math.ceil(w * self.scale)), int(math.ceil(h * self.scale))
        fill = self.color(bg.get("fill"))
        sig = (W, H, fill, bg.get("shape"), res)
        old = self._tiles.get("\0tail")
        if old and old[0] == sig:
            return old[1]
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _fill(img, 0, 0, W, H, fill, self.shape_mask(W, H, bg.get("shape"), bg.get("radius"), h))
        for r in res:
            self.draw(img, r)
        self._tiles["\0tail"] = (sig, img)
        return img

    def move(self, key, target, now, fresh):
        """Где стоит строка сейчас: при смене места она переезжает плавно."""
        p = self._pos.get(key)
        if p is None or fresh:
            self._pos[key] = {"from": target, "to": target, "t0": now}
            return target, False
        if p["to"] != target:
            cur, _ = self._at(p, now)
            p.update({"from": cur, "to": target, "t0": now})
        return self._at(p, now)

    @staticmethod
    def _at(p, now):
        t = (now - p["t0"]) / ROW_MOVE_S
        if t >= 1 or p["from"] == p["to"]:
            return p["to"], False
        k = ROW_EASE(t)
        (x0, y0), (x1, y1) = p["from"], p["to"]
        return (x0 + (x1 - x0) * k, y0 + (y1 - y0) * k), True

    # ---------- кадр ----------

    def tail_enter(self, t, img, pos, now, start):
        """Подпись после карточек выезжает отдельно, вслед за плашкой — как
        tailEnter() в render.js. (картинка, место, идёт ли движение)."""
        en = t.get("enter")
        if not isinstance(en, dict):
            return img, pos, False
        if start:
            self._tail_t0 = now
        if self._tail_t0 is None:
            return img, pos, False
        delay, dur = _n(en.get("delay", .22)), _n(en.get("duration", .45)) or .45
        k = (now - self._tail_t0 - delay) / dur
        if k >= 1:
            self._tail_t0 = None
            return img, pos, False
        e = TAIL_EASE(max(0.0, k))
        dx = (-1 if en.get("from") == "left" else 1) * _n(en.get("shift", 60)) * (1 - e)
        faded = img.copy()
        faded.putalpha(faded.getchannel("A").point(lambda p: int(p * e + .5)))
        return faded, (pos[0] + dx, pos[1]), True

    def tail_on(self, data) -> bool:
        t = self.spec["body"].get("tail")
        return bool(t and data and cond(t.get("when"), data, data))

    def body_width(self, data, items) -> float:
        b = self.spec["body"]
        if b.get("type") == "cards":
            n = len(items)
            iw, gap = _n(b["item"].get("width")) or 200, _n(b.get("gap"))
            w = n * iw + max(0, n - 1) * gap
            if self.tail_on(data):
                w += (gap if n else 0) + (_n(b["tail"].get("width")) or 160)
            return max(w, 1)
        return _n(self.spec.get("width")) or 300

    def page_info(self, items, now):
        b = self.spec["body"]
        if b.get("type") != "grid" or not b.get("page_size"):
            return items, 1, 1
        size = int(_n(b["page_size"]))
        pages = max(1, math.ceil(len(items) / size))
        if self._page_at is None:
            self._page_at = now
        if pages > 1 and now - self._page_at >= (_n(b.get("page_seconds")) or 15):
            self._page, self._page_at = (self._page + 1) % pages, now
        self._page = min(self._page, pages - 1)
        return items[self._page * size:(self._page + 1) * size], self._page + 1, pages

    def frame(self, data: dict, now: float):
        """Кадр оверлея: (картинка RGBA с полем PAD вокруг, идёт ли движение)."""
        spec, s = self.spec, self.scale
        b = spec.get("body") or {}
        typ = b.get("type")
        items = get(data, b["items"]) if b.get("items") else []
        items = items if isinstance(items, list) else []
        width = self.body_width(data, items)
        if typ == "cards" and width != self._width:
            self._pos.clear()                 # карточки перестраиваются без переезда
        self._width = width
        plist, page, pages = self.page_info(items, now)
        psize = _n(b.get("page_size"))
        rootd = dict(data, count=len(items), page=page, pages=pages,
                     page_from=(page - 1) * psize + 1, page_to=(page - 1) * psize + len(plist))
        head, foot = spec.get("header"), spec.get("footer")
        hh = _n(head.get("height")) if head else 0
        fh = _n(foot.get("height")) if foot else 0
        fbd, bar = spec.get("flagbar"), data.get("flag_bar") or {}
        fbm = _n(fbd.get("margin", 6)) if fbd else 0
        fb_h = (_n(fbd.get("height")) or 26) + fbm if (fbd and bar.get("text")) else 0
        top = hh + fb_h                       # где начинается тело
        pad = _n(b.get("pad"))
        ready = truthy(get(data, b["ready"])) if b.get("ready") else True
        tail = self.tail_on(data) if typ == "cards" else False
        is_empty = (not ready) if typ == "single" else (not plist and not tail)
        empty_txt = b.get("empty") or ""

        moving = False
        placed = []
        if typ == "single":
            body_h = 40 if is_empty else (_n(b.get("height")) or 100)
        else:
            it = b.get("item") or {}
            ih = _n(it.get("height"))
            cols = int(_n(b.get("columns")) or 1) if typ == "grid" else 1
            gap = _n(b.get("gap"))
            w = (_n(it.get("width")) or 200) if typ == "cards" else math.floor((width - gap * (cols - 1)) / cols)
            sep = b.get("sep") or {}
            sep_after = int(_n(get(data, str(sep.get("after") or "").replace("$.", "", 1)))) if sep else 0
            sep_h = (_n(sep.get("height")) or 22) if 0 < sep_after < len(plist) else 0
            seen = set()
            for i, item in enumerate(plist):
                if not isinstance(item, dict):
                    continue
                kv = get(item, b.get("key") or "id")
                key = js_str(kv) if kv is not None else str(i)
                seen.add(key)
                fresh = key not in self._tiles
                img = self.tile(key, it, w, item, rootd, item_states(item, rootd), i % 2 == 1)
                if typ == "cards":
                    tgt = (i * (w + gap), 0)
                else:
                    tgt = ((i % cols) * (w + gap),
                           pad + (i // cols) * (ih + gap) + (sep_h if sep_h and i >= sep_after else 0))
                pos, mv = self.move(key, tgt, now, fresh)
                moving |= mv
                placed.append((img, pos))
            appeared = self._last_frame is None or now - self._last_frame > SHOWN_GAP_S
            if tail:
                n = len(plist)
                fresh = "\0tail" not in self._tiles
                img = self.tail_tile(b["tail"], ih or 40, rootd)
                pos, mv = self.move("\0tail", (n * (w + gap) if n else 0, 0), now, fresh)
                moving |= mv
                img, pos, mv = self.tail_enter(b["tail"], img, pos, now,
                                               appeared or not self._tail_on)
                moving |= mv
                placed.append((img, pos))
                seen.add("\0tail")
            self._tail_on = bool(tail)
            for k in [k for k in self._tiles if k not in seen]:
                self._tiles.pop(k, None)
                self._pos.pop(k, None)
            rows = (1 if (plist or tail) else 0) if typ == "cards" else math.ceil(len(plist) / cols)
            body_h = rows * ih + (rows - 1) * gap + 2 * pad + sep_h if rows else 0
            if is_empty and empty_txt:
                body_h = 40

        self._last_frame = now
        H = top + body_h + fh
        FW, FH = self.px(width), self.px(H)
        f = spec.get("frame") or {}
        surf = f.get("surface")
        sep_y = top + pad + sep_after * (ih + gap) - gap if typ == "list" and sep_h else None

        # Неподвижная часть кадра — фон, свечение, кромка, шапка, плашка флага,
        # подвал, разделитель — меняется только с новыми данными (раз в 0,4 с).
        # Во время анимации её не перерисовываем: кадр = копия + строки + волна.
        # Иначе кадр башни стоил 10–20 мс и движение шло рывками.
        key = (FW, FH, top, body_h, data.get("flag_tone"), sep_y, is_empty)
        base = self._base
        if base and base[0] is data and base[1] == key:
            fr = base[2].copy()
        else:
            fr = Image.new("RGBA", (max(1, FW), max(1, FH)), (0, 0, 0, 0))
            if surf:
                self.surface(fr, surf)
                self.edges(fr, surf, data)          # кромка под строками — как inset-тень в CSS
            if head:
                self.layer(fr, head, width, 0, rootd)
            if fb_h:
                self.flag_bar(fr, fbd, bar, width, hh)
            if typ == "single":
                bg = b.get("bg") or {}
                Y = self.px(top)
                _fill(fr, 0, Y, FW, self.px(top + body_h) - Y, self.color(bg.get("fill")))
                if not is_empty:
                    self.elements(fr, b.get("elements"), data, rootd, item_states(data, rootd),
                                  0, top, body_h)
            elif sep_y is not None:
                self.separator(fr, sep, tpl(sep.get("text"), rootd, rootd), width, sep_y, sep_h)
            if is_empty and empty_txt:
                self.empty(fr, top, width, empty_txt)
            stripe = _n(f.get("flag_stripe"))
            if stripe and body_h:
                Y = self.px(top)
                _fill(fr, 0, Y, self.px(stripe), self.px(top + body_h) - Y,
                      parse_color(FLAG.get(data.get("flag"), None)))
            if foot:
                self.layer(fr, foot, width, top + body_h, rootd)
            self._base = (data, key, fr.copy())
        if typ != "single":
            for img, (x, y) in placed:
                _paste(fr, img, self.px(x), self.px(top + y))
        if f.get("wave"):
            moving |= self.wave(fr, data, now, hh, top,
                                [(y, ih) for _img, (_x, y) in placed] if typ != "single" else [])

        radius = _n(f.get("radius")) * s
        if radius > 0:
            fr.putalpha(ImageChops.multiply(fr.getchannel("A"), self._round(fr.size, radius)))
        pad = self.px(PAD)
        # холст с полем и тенью — один на размер кадра
        pk = (fr.size, radius, bool(f.get("shadow") and H > 0))
        canvas = self._padded.get(pk)
        if canvas is None:
            canvas = Image.new("RGBA", (fr.width + 2 * pad, fr.height + 2 * pad), (0, 0, 0, 0))
            if pk[2]:
                canvas.alpha_composite(self._shadow(fr.size, radius))
            if len(self._padded) > 8:
                self._padded.clear()
            self._padded[pk] = canvas
        out = canvas.copy()
        out.alpha_composite(fr, (pad, pad))
        return out, moving

    # ---------- поверхность, флаг, волна ----------

    def surface(self, fr, s) -> None:
        """Цвет панели и оранжевое свечение из левого верхнего угла."""
        W, H = fr.size
        fill, glow = self.color(s.get("fill")), self.color(s.get("glow")) if s.get("glow") else None
        key = ("surface", W, H, fill, glow)
        img = self._shapes.get(key)
        if img is None:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            _fill(img, 0, 0, W, H, fill)
            if glow:
                img.alpha_composite(self._glow(W, H, glow))
            self._shapes[key] = img
        fr.paste(img, (0, 0))

    def _glow(self, W, H, col):
        """radial-gradient(120% 70% at 0 0, цвет .16, transparent 60%) из render.js."""
        key = ("glow", W, H, col)
        g = self._shapes.get(key)
        if g is None:
            n = 96
            m = Image.new("L", (n, n), 0)
            px = m.load()
            for yy in range(n):
                for xx in range(n):
                    r = math.hypot(xx / n / 1.2, yy / n / 0.7)
                    if r < 0.6:
                        px[xx, yy] = int(255 * 0.16 * (1 - r / 0.6) + 0.5)
            g = Image.new("RGBA", (W, H), (*col[:3], 255))
            g.putalpha(m.resize((W, H), Image.BILINEAR))
            self._shapes[key] = g
        return g

    def edges(self, fr, s, data) -> None:
        """Рамка в 1 px и кромка слева: оранжевая, под жёлтым — жёлтая, на рестарте — зелёная."""
        W, H = fr.size
        if s.get("border"):
            _fill(fr, 0, 0, W, H, self.color(s.get("border")), self.ring_mask(W, H, None, None, 0))
        if s.get("edge"):
            tone = TONE.get(data.get("flag_tone"))
            _fill(fr, 0, 0, self.px(_n(s.get("edge"))), H, self.color(tone or s.get("edge_color") or "$accent"))

    def flag_bar(self, fr, d, bar, width, y0) -> None:
        """Плашка флага под шапкой: «пейс-кар на трассе», «рестарт»."""
        m = _n(d.get("margin", 6))
        h = _n(d.get("height")) or 26
        X, Y = self.px(m), self.px(y0 + m)
        W, H = self.px(width - m) - X, self.px(y0 + m + h) - Y
        if W <= 0 or H <= 0:
            return
        _fill(fr, X, Y, W, H, self.color(TONE.get(bar.get("tone")) or "$yellow"),
              self.shape_mask(W, H, "round", _n(d.get("radius", 4)), h))
        r = {"text": str(bar.get("text") or "").upper(), "color": parse_color("#111111"),
             "font": self.font_stack("text"), "size": 12, "weight": 800, "italic": False,
             "spacing": 1.6, "align": "left"}
        self.text(fr, X + self.px(10), Y, W - self.px(20), H, r, 1.0)

    def separator(self, fr, s, txt, width, y, h) -> None:
        """Разделитель «топ + окно»: подпись по центру, линии по бокам."""
        x0 = _n(s.get("x", 12))
        X, Y = self.px(x0), self.px(y)
        W, H = self.px(width - x0) - X, self.px(y + h) - Y
        col = self.color(s.get("color") or "$dim")
        if W <= 0 or H <= 0 or col is None:
            return
        font, _fi, _st = self.fonts.get(self.font_stack("text"), 10.5 * self.scale, 800, False)
        sp = 1.6 * self.scale
        txt = txt.upper()
        tw = font.getlength(txt) + sp * len(txt)
        gap = self.px(10)
        line = (*col[:3], int(col[3] * .35))
        side = max(0, (W - tw) / 2 - gap)
        _fill(fr, X, Y + H // 2, int(side), max(1, round(self.scale)), line)
        _fill(fr, X + W - int(side), Y + H // 2, int(side), max(1, round(self.scale)), line)
        r = {"text": txt, "color": col, "font": self.font_stack("text"), "size": 10.5, "weight": 800,
             "italic": False, "spacing": 1.6, "align": "center"}
        self.text(fr, X, Y, W, H, r, 1.0)

    def wave(self, fr, data, now, hh, top, rows) -> bool:
        """Выброс флага: одна волна сверху вниз — полоса света и вспышка шапки и
        строк в момент её прохода. Как wave() в render.js. True — волна идёт."""
        ev = data.get("flag_event") or {}
        if ev.get("id") is not None and ev.get("id") != self._wave_id:
            self._wave_id = ev.get("id")
            age = num(ev.get("age"))
            if age is not None and 0 <= age < 1.5:      # открыли окно позже — не догоняем
                self._wave = {"t0": now - age, "tone": ev.get("tone")}
        w = self._wave
        if not w:
            return False
        t = now - w["t0"]
        if t >= WAVE_S + LIT_S:
            self._wave = None
            return False
        col = self.color(TONE.get(w["tone"]) or "$yellow")
        if col is None:
            return False
        W, H = fr.size
        Hd = H / self.scale                          # высота в пикселях описания

        def lit(y, h):
            k = (t - max(0.0, (y + h / 2) / Hd * WAVE_S - 0.12)) / LIT_S
            if 0 < k < 1:
                a = k / .3 if k < .3 else (1 - k) / .7
                Y = self.px(y)
                _fill(fr, 0, Y, W, self.px(y + h) - Y, col, None, .88 * a)

        if hh:
            lit(0, hh)
        ih = rows[0][1] if rows else 0
        for y, _h in rows:
            lit(top + y, ih)
        if t < WAVE_S:                               # полоса света
            bh = self.px(90)
            key = ("band", W, bh, col)
            band = self._shapes.get(key)
            if band is None:
                m = Image.new("L", (1, bh), 0)
                for yy in range(bh):
                    m.putpixel((0, yy), int(255 * .9 * (1 - abs(yy / bh * 2 - 1))))
                band = Image.new("RGBA", (W, bh), (*col[:3], 255))
                band.putalpha(m.resize((W, bh)))
                self._shapes[key] = band
            _paste(fr, band, 0, int(-bh / 2 + t / WAVE_S * H))
        return True

    def _round(self, size, radius):
        key = ("round", size, radius)
        m = self._shapes.get(key)
        if m is None:
            k = 4
            big = Image.new("L", (size[0] * k, size[1] * k), 0)
            ImageDraw.Draw(big).rounded_rectangle([0, 0, size[0] * k - 1, size[1] * k - 1],
                                                  radius=radius * k, fill=255)
            m = big.resize(size, Image.BOX)
            self._shapes[key] = m
        return m

    def _shadow(self, size, radius):
        """Тень 0 8px 22px -8px rgba(0,0,0,.7) — только снаружи рамки, как у CSS."""
        key = (size, radius)
        sh = self._shadows.get(key)
        if sh is not None:
            return sh
        s, pad = self.scale, self.px(PAD)
        W, H = size
        m = Image.new("L", (W + 2 * pad, H + 2 * pad), 0)
        spread, off = 8 * s, 8 * s
        x0, y0 = pad + spread, pad + spread + off
        x1, y1 = pad + W - spread, pad + H - spread + off
        if x1 > x0 and y1 > y0:
            ImageDraw.Draw(m).rounded_rectangle([x0, y0, x1 - 1, y1 - 1],
                                                radius=max(0.0, radius - spread), fill=int(255 * .7))
            m = m.filter(ImageFilter.GaussianBlur(11 * s))
        m.paste(0, (pad, pad, pad + W, pad + H))
        sh = Image.new("RGBA", m.size, (0, 0, 0, 0))
        sh.putalpha(m)
        self._shadows[key] = sh
        return sh
