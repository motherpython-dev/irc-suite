"""Отрисовка оверлеев пилота (PIL): Standings, Relative, радар, ветер, двигатель.

Данные приходят готовыми из модуля pilot (`/api/pilot/data?id=...`): строки
таблиц, сторона и положение соседа для радара, зоны двигателя. Здесь только
картинка — RGBA с прозрачностью вокруг панели, её выводит слоистое окно хоста
оверлеев. Вид — как у мокапа и эфирных оверлеев: тёмная панель, оранжевое
свечение из угла, оранжевая кромка слева, шрифты Exo 2 и Roboto Mono.

Фигуры (скругления, дуги радара, стрелка ветра) рисуются вдвое крупнее и
уменьшаются — так края сглажены; текст рисуется сразу в размер.
"""
from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .ov_paint import Fonts, parse_color
from ..radar_script import radar_script

TEXT, MONO, ARROW = "'Exo 2'", "'Roboto Mono'", "'Segoe UI Symbol'"
FG, DIM, MID, ACC, LINE = "#EAEEF5", "#8E8E98", "#C3C9D3", "#FF5A1F", "#2C323C"
PURPLE, GREEN, RED, YELLOW = "#C38BFF", "#2FD16A", "#FF4D4D", "#FFD200"
TONE = {"ahead": "#FF8A6A", "behind": "#7FB3FF", "pace": "#FFD200"}
PAD = 14                       # поле под тень
SS = 2                         # сглаживание фигур: рисуем вдвое крупнее

# колонки таблиц: ширина, px при 100 %, и заголовок
COLW = {"ld": (44, "Δ Lap"), "ir": (36, "iR"), "dir": (40, "±iR"), "lic": (34, "Lic"), "pits": (22, "Pit"), "pitlast": (84, "Last pit"),
        "sec": (132, "S1 · S2 · S3"), "last": (66, "Last"), "best": (66, "Best"), "gap": (62, "")}
ROW_H, HEAD_H, HDR_H = 26, 40, 18
INFO_H = 24                    # Relative: строка сведений сверху (температуры, инциденты) и снизу (круг, время)
NAME_W = 150
# шрифты таблиц на выбор: роль -> вариант -> (CSS-стек, множитель размера, толщина, разрядка).
# Bahnschrift узкий — крупнее, чтобы высота цифр совпала с Exo 2; Segoe UI Variable
# есть только в Windows 11 — на Windows 10 берётся обычный Segoe UI
TYPO = {
    "pos": {"exo": ("'Exo 2'", 1.0, 800, 0), "bahn": ("'Bahnschrift Bold Condensed'", 1.11, 700, 0),
            "segv": ("'Segoe UI Variable', 'Segoe UI'", 1.0, 700, 0), "segoe": ("'Segoe UI Black', 'Segoe UI'", .96, 900, 0)},
    "name": {"exo": ("'Exo 2'", 1.0, 700, .03), "bahn": ("'Bahnschrift SemiBold SemiCondensed'", 1.07, 600, 0),
             "segv": ("'Segoe UI Variable', 'Segoe UI'", .96, 650, 0),
             "franklin": ("'Franklin Gothic Medium', 'Segoe UI'", 1.04, 400, .02),
             "segoe": ("'Segoe UI Semibold', 'Segoe UI'", .96, 600, .02)},
    "num": {"mono": ("'Roboto Mono'", 1.0, 0, 0), "bahn": ("'Bahnschrift SemiCondensed'", 1.1, 0, 0),
            "segv": ("'Segoe UI Variable', 'Segoe UI'", 1.03, 0, 0)},
}
TYPO_DEFAULT = {"pos": "exo", "name": "exo", "num": "mono"}
CHG_W = 30                     # колонка «▲4»: стрелка и число отыгранных мест
NUM_SKEW = 0.18                # наклон номера на плашке (курсив)
FLASH_S, FLASH_N = 0.45, 3     # мигание окна двигателя: длительность вспышки и сколько раз
# рация: цифры говорящего гаснут за VOICE_IN, волна стихает за VOICE_OUT после
# конца реплики; отсвет по строке — SWEEP_S; строка «Говорит» — STRIP_H px
VOICE_IN, VOICE_OUT, SWEEP_S, STRIP_H = 0.28, 0.5, 0.9, 29
WIND_TAU = 0.07                # сглаживание поворота ветра, с
# вид «кольцо»: цвет кольца по скорости ветра (до, км/ч), непрозрачность
WIND_BANDS = ((2, "#8FC3E6", .38), (10, "#4FBF7A", .80), (20, "#E2C04A", .85), (1e9, "#D9574A", .85))
WIND_COLOR_TAU = 0.15
# двигатель, вид «полукруги»
ENG_D = 54                     # размер одного полукруга, px при 100 %
ENG_COLOR_TAU = 0.15           # цвет дуги переходит плавно, с
ENG_FLASH_S = 0.6              # вспышка окна при включении лампы, с
ENG_G, ENG_Y, ENG_O, ENG_R, ENG_N = (79, 191, 122), (226, 192, 74), (255, 138, 61), (255, 77, 77), (172, 180, 192)          # цвет кольца переходит к новой полосе плавно, с
WAVE_S, LIT_S = 1.1, 0.55     # волна выброса флага и вспышка строки — как в башне (ov_paint)
RADAR_FADE = 0.2              # полоса радара появляется и гаснет, с
# сетапы: колонка подписей, столбец сетапа (все одной ширины), строки
SET_LAB, SET_COL, SET_TH, SET_SMALL = 86, 118, 38, 22
SET_COL_TY = 300               # столбец с блоком «Резина»: колесо — три части
TY_WHEEL = 70                  # высота колеса в блоке «Резина»
RADAR_M = 10                   # поле вокруг полос радара: кромка и ореол не упираются в край окна
def _ease(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x * x * (3 - 2 * x)


def rgba(c, a=1.0):
    p = parse_color(c) or (0, 0, 0, 0)
    return (p[0], p[1], p[2], int(round(p[3] * a)))


class PilotPainter:
    def __init__(self, fetch) -> None:
        self.fonts = Fonts(fetch)
        self.dpi = 1.0
        self.bg = 0.92                       # непрозрачность фона окна (cfg.bg, %): 0 — прозрачный, 1 — сплошной
        self._panel: dict = {}
        self._ind: dict = {}                 # плитки индикатора радара
        self._rad_fade = {"l": 0.0, "r": 0.0, "t": -9.0}   # видимость полос радара, 0..1
        self._wc: dict = {}
        self._ec: dict = {}                   # цвета дуг двигателя (вид «полукруги»)                   # цвет кольца ветра (вид «кольцо»)
        self._wa = {"a": None, "t": -9.0}    # текущий угол треугольника ветра
        self._tbl = None                     # таблица без анимации рации: (ключ, слои)
        self._tiles: dict = {}               # плитки таблиц: шапка, строки, ячейки
        self._key_of = (None, None)          # данные -> ключ (json на кадр — лишняя работа)
        self.fps = 60                        # сколько кадров в секунду просит анимация
        self._vt = {"id": None, "start": 0.0, "end": None}   # реплика: местное время начала и конца
        self._flash = {"seen": None, "start": 0.0, "tone": ""}
        self._wave_id, self._wave = None, None   # выброс флага: последний показанный и идущая волна

    def set_scale(self, dpi: float) -> None:
        self.dpi = dpi or 1.0

    # ---------- общее ----------

    def font(self, stack, size, weight=700):
        f, _it, _st = self.fonts.get(stack, size, weight, False)
        return f

    def text(self, d, x, y, s, txt, size, color, weight=700, stack=TEXT, anchor="lm",
             spacing=0.0, alpha=1.0, maxw=None):
        """Текст по вертикальной середине y. spacing — разрядка в em."""
        txt = str(txt)
        f = self.font(stack, size * s, weight)
        fill = rgba(color, alpha)
        if maxw is not None:
            while txt and d.textlength(txt, font=f) > maxw:
                txt = txt[:-1]
        if not spacing:
            d.text((x, y), txt, font=f, fill=fill, anchor=anchor)
            return d.textlength(txt, font=f)
        sp = spacing * size * s
        w = sum(d.textlength(ch, font=f) for ch in txt) + sp * max(0, len(txt) - 1)
        cx = x - (w if anchor[0] == "r" else w / 2 if anchor[0] == "m" else 0)
        for ch in txt:
            d.text((cx, y), ch, font=f, fill=fill, anchor="l" + anchor[1])
            cx += d.textlength(ch, font=f) + sp
        return w

    def panel(self, w: int, h: int, s: float, glow=True, tint=None, edge=None) -> Image.Image:
        """Панель с тенью, свечением из угла, кромкой и рамкой; поле PAD вокруг.
        edge — цвет кромки слева (под полным жёлтым у таблиц — жёлтая)."""
        bgk = round(self.bg, 2)
        key = (w, h, round(s, 3), glow, tint, bgk, edge)
        hit = self._panel.get(key)
        if hit is not None:
            return hit.copy()
        P = int(PAD * s)
        r = 6 * s
        img = Image.new("RGBA", (w + 2 * P, h + 2 * P), (0, 0, 0, 0))
        # тень
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle((P, P + 8 * s, P + w, P + h + 2 * s), r, fill=150)
        sh = sh.filter(ImageFilter.GaussianBlur(9 * s))
        img.paste((0, 0, 0, 255), (0, 0), sh)
        # тело — вдвое крупнее, потом уменьшаем
        W, H, R = w * SS, h * SS, r * SS
        body = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), R, fill=255)
        base = Image.new("RGBA", (W, H), rgba(tint) if tint else (20, 25, 35, 255))
        if glow:
            # radial-gradient(120% 70% at 0 0, rgba(255,90,31,.14), transparent 60%)
            g = Image.radial_gradient("L").resize((max(2, int(2.4 * W)), max(2, int(1.4 * H))))
            q = g.crop((g.width // 2, g.height // 2, g.width, g.height))
            g = Image.new("L", (W, H), 255)
            g.paste(q.crop((0, 0, min(W, q.width), min(H, q.height))), (0, 0))
            g = g.point(lambda v: int(max(0.0, 1 - (v / 255) / 0.6) * 36))
            base.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (W, H), (255, 90, 31)).split(), g)))
        # оранжевая кромка слева
        ImageDraw.Draw(base).rectangle((0, 0, 3 * s * SS, H), fill=rgba(edge or ACC))
        body.paste(base, (0, 0), mask)
        ImageDraw.Draw(body).rounded_rectangle((0, 0, W - 1, H - 1), R, outline=rgba(LINE), width=max(1, int(SS * s)))
        body = _down(body, (w, h))
        img.alpha_composite(body, (P, P))
        if bgk < 1:
            # фон окна — вся панель с тенью и кромкой; текст рисуется поверх и не тускнеет
            img.putalpha(img.getchannel("A").point(lambda a: int(a * bgk)))
        self._panel[key] = img
        if len(self._panel) > 80:
            self._panel.pop(next(iter(self._panel)))
        return img.copy()

    def header(self, db, dt, P, s, w, small, big, chip=None, right=(), y0=None):
        """Шапка: мелкая строка (сессия своим цветом, дальше трасса — оранжевым),
        крупный заголовок, справа значения."""
        top = P if y0 is None else y0           # y0 — шапка внизу: линия над ней, а не под
        x, cy = P + 14 * s, top + HEAD_H / 2 * s
        if small or chip:
            sx = x
            if chip and chip[0]:
                sx += self.text(dt, sx, cy - 7 * s, s, chip[0].upper(), 9, chip[1] or ACC, 800, spacing=.2)
                if small:
                    sx += self.text(dt, sx, cy - 7 * s, s, "  ·  ", 9, DIM, 800)
            if small:
                self.text(dt, sx, cy - 7 * s, s, small.upper(), 9, ACC, 800, spacing=.2)
            self.text(dt, x, cy + 6 * s, s, big.upper(), 13.5, FG, 800, spacing=.08)
        else:
            self.text(dt, x, cy, s, big.upper(), 13.5, FG, 800, spacing=.08)
        rx = P + w - 12 * s
        for label, val, *tone in reversed(list(right)):
            f = self.font(MONO, 13 * s, 700)
            vw = max(dt.textlength(val, font=f), self.font(TEXT, 9 * s, 800).getlength(label.upper()) + 9 * .2 * s * len(label))
            self.text(dt, rx, cy - 7 * s, s, label.upper(), 9, DIM, 800, anchor="rm", spacing=.2)
            self.text(dt, rx, cy + 7 * s, s, val, 13, tone[0] if tone else FG, 700, stack=MONO, anchor="rm")
            rx -= vw + 14 * s
        ly = top + HEAD_H * s if y0 is None else top - 2 * s
        db.line((P, ly, P + w, ly), fill=rgba(LINE), width=max(1, int(s)))

    # ---------- таблицы ----------

    @staticmethod
    def _cols(cfg, gap_w, rel=False):
        on = cfg.get("cols") or {}
        out = []
        for k in ("ld", "ir", "dir", "lic", "pits", "pitlast", "sec", "last", "best", "gap"):
            if rel and k == "best":
                continue
            if on.get(k):
                out.append((k, gap_w if k == "gap" else COLW[k][0]))
        return out

    def _table_w(self, cols, chg):
        n = 3 + (1 if chg else 0) + len(cols)
        return 22 + (CHG_W if chg else 0) + 32 + NAME_W + sum(w for _, w in cols) + 8 * (n - 1) + 18

    def _layout(self, cols, chg):
        """x каждой колонки (px при 100 %) от левого края строки."""
        x, out = 8, {}
        for k, w in [("p", 22)] + ([("chg", CHG_W)] if chg else []) + [("num", 32), ("name", NAME_W)] + cols:
            out[k] = (x, w)
            x += w + 8
        return out

    def _hdr(self, dt, P, s, y, lay, cols, gap_head):
        cy = y + HDR_H / 2 * s
        for k, (x, w) in lay.items():
            label = {"p": "P", "num": "№", "name": "Driver"}.get(k) or (gap_head if k == "gap" else COLW.get(k, (0, ""))[1])
            if not label:
                continue
            anchor = "lm" if k == "name" else "mm" if k in ("num", "lic", "sec") else "rm"
            xx = P + (x + (0 if anchor == "lm" else w / 2 if anchor == "mm" else w)) * s
            self.text(dt, xx, cy, s, label.upper(), 8.5, DIM, 800, anchor=anchor, spacing=.12)

    def _row(self, db, dt, P, s, y, r, lay, cols, chg, w, head=True, tail=True):
        cy = y + ROW_H / 2 * s
        a = 0.5 if r.get("pit") else 1.0
        tone = TONE.get(r.get("tone", ""))
        X = lambda k: P + lay[k][0] * s                     # noqa: E731
        R = lambda k: P + (lay[k][0] + lay[k][1]) * s       # noqa: E731
        if head:
            self._row_head(db, dt, P, s, y, cy, r, lay, chg, w, a, tone, X, R)
        if tail:
            self._row_tail(db, dt, s, cy, r, cols, a, tone, X, R)

    def _row_head(self, db, dt, P, s, y, cy, r, lay, chg, w, a, tone, X, R):
        """Место, номер, фамилия, флаг — и подложка своей строки."""
        if r.get("me"):
            db.rounded_rectangle((P + 4 * s, y + 1 * s, P + w - 4 * s, y + (ROW_H - 1) * s), 4 * s,
                                fill=(255, 90, 31, 56))
        ty = getattr(self, "_ty", None) or {k: TYPO[k][TYPO_DEFAULT[k]] for k in TYPO}
        st, m, w, _sp = ty["pos"]
        self.text(dt, R("p"), cy, s, r["pos"], 13.5 * m, DIM, w, stack=st, anchor="rm", alpha=a)
        if chg and r.get("chg"):
            # сколько мест отыграно (▲) или потеряно (▼) со старта — стрелка и число цветом
            up = r["chg"] > 0
            col = GREEN if up else RED
            self.text(dt, X("chg"), cy, s, "▲" if up else "▼", 7.5, col, 400, stack=ARROW, anchor="lm", alpha=a)
            self.text(dt, X("chg") + 9 * s, cy, s, str(abs(r["chg"])), 10 * ty["num"][1], col, 700, ty["num"][0], "lm", alpha=a)
        # цвет машины — фоном плашки номера (раньше — полоской слева), цифры с обводкой
        box = (X("num"), cy - 9 * s, X("num") + 32 * s, cy + 9 * s)
        db.rounded_rectangle(box, 3 * s, fill=rgba(r.get("color") or "#232C3D", a))
        if r.get("color2"):
            # два цвета ливреи — градиент слева направо: концы-скругления чистыми
            # цветами, между ними полосы с переходом
            c1, c2 = rgba(r["color"], a), rgba(r["color2"], a)
            rr = 3 * s
            db.rounded_rectangle((box[2] - 2 * rr, box[1], box[2], box[3]), rr, fill=c2)
            x0, x1, n = box[0] + rr, box[2] - rr, 24
            for i in range(n):
                t = i / (n - 1)
                db.rectangle((x0 + (x1 - x0) * i / n, box[1], x0 + (x1 - x0) * (i + 1) / n + 0.5, box[3]),
                             fill=tuple(int(c1[j] + (c2[j] - c1[j]) * t) for j in range(4)))
        # центр цифр — в центр плашки; плашка стоит в дробных координатах, поэтому
        # округляем до ближайшего пикселя, а не вниз (иначе номер уезжал влево-вверх)
        tile, tcx, tcy = self._num_tile(str(r["num"]), s, a)
        dt._image.alpha_composite(tile, (int(round(X("num") + 16 * s - tcx)), int(round(cy - tcy))))
        flag = r.get("flag") if r.get("_flags") else ""
        nw = (NAME_W - (18 if flag else 0)) * s
        name = str(r["name"]).upper()
        st, m, w, sp = ty["name"]
        tw = self.text(dt, X("name"), cy, s, name, 13.5 * m, tone or (FG if not r.get("me") else "#FFFFFF"), w,
                       stack=st, spacing=sp, alpha=a, maxw=nw)
        if flag:
            fx = X("name") + min(tw, nw) + 5 * s
            box = (fx, cy - 6 * s, fx + 12 * s, cy + 6 * s)
            if flag == "warn":
                db.polygon([(box[0], box[1]), (box[2], box[1]), (box[0], box[3])], fill=(0, 0, 0, 255))
                db.polygon([(box[2], box[1]), (box[2], box[3]), (box[0], box[3])], fill=(255, 255, 255, 255))
                db.rectangle(box, outline=rgba(DIM))
            else:
                db.rectangle(box, fill=(0, 0, 0, 255), outline=(255, 255, 255, 255))
                if flag == "meat":
                    db.ellipse((box[0] + 3 * s, box[1] + 3 * s, box[2] - 3 * s, box[3] - 3 * s), fill=rgba("#FF8A00"))

    def _num_tile(self, num, s, a):
        """Номер на плашке: Exo 2 900 курсивом (наклон NUM_SKEW), мягкая тень вместо
        обводки. Плитка размером с плашку, кэшируется."""
        key = ("num", round(s, 3), num, round(a, 2))
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        W, H = int(32 * s), int(18 * s)
        f = self.font(TEXT, 12.5 * s, 900)
        CW, CH = W * 2, H * 2                          # холст с запасом: курсив и тень не обрежутся

        def lay(col, dy):
            img = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((CW / 2, CH / 2 + dy), num, font=f, fill=col, anchor="mm")
            # курсив — сдвиг строк: верх вправо, низ влево
            return img.transform(img.size, Image.AFFINE, (1, NUM_SKEW, -NUM_SKEW * CH / 2, 0, 1, 0), Image.BICUBIC)
        txt = lay((255, 255, 255, int(255 * a)), 0)
        # центр — по самим цифрам после наклона: «mm» шрифта считает и нижние выносные
        # элементы, которых у цифр нет, и цифры садились на 1–1,5 px ниже середины;
        # по горизонтали курсив и ширина цифр сдвигали до 2 px
        bx = txt.getchannel("A").point(lambda v: 255 if v > 96 else 0).getbbox() or (0, 0, CW, CH)
        cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
        sh = lay((0, 0, 0, 255), 1 * s).filter(ImageFilter.GaussianBlur(1.1 * s))
        sh.putalpha(sh.getchannel("A").point(lambda v: int(v * .85 * a)))
        sh.alpha_composite(txt)
        # плитка с запасом вокруг цифр; вместе с ней — где в ней центр цифр (дробно)
        m = int(3 * s)
        x0, y0 = int(cx - W / 2) - m, int(cy - H / 2) - m
        tile = sh.crop((x0, y0, x0 + W + 2 * m, y0 + H + 2 * m))
        out = (tile, cx - x0, cy - y0)
        if len(self._tiles) > 4000:
            self._tiles.clear()
        self._tiles[key] = out
        return out

    def _row_tail(self, db, dt, s, cy, r, cols, a, tone, X, R):
        """Колонки после фамилии: рейтинг, лицензия, времена, отрыв."""
        N, nm, _w, _sp = (getattr(self, "_ty", None) or {}).get("num") or TYPO["num"][TYPO_DEFAULT["num"]]
        for k, _w in cols:
            if r.get("pace") and k != "gap":
                continue                         # у пейс-кара — только расстояние
            x0, x1 = X(k), R(k)
            if k == "ld":
                v = r.get("ld")
                if v is not None:
                    col = GREEN if v >= 0.005 else "#FF6A6A" if v <= -0.005 else DIM
                    self.text(dt, x1, cy, s, f"{abs(v):.2f}", 11.5 * nm, col, 600, N, "rm", alpha=a)
            elif k == "ir":
                self.text(dt, x1, cy, s, f"{(r.get('ir') or 0) / 1000:.1f}k", 11.5 * nm, DIM, 500, N, "rm", alpha=a)
            elif k == "dir":
                v = r.get("dir") or 0
                self.text(dt, x1, cy, s, f"{'+' if v > 0 else ''}{v}", 11.5 * nm, GREEN if v > 0 else "#FF6A6A" if v < 0 else DIM,
                          600, N, "rm", alpha=a)
            elif k == "lic":
                lic = str(r.get("lic") or "")
                if lic:
                    parts = lic.split()
                    t = f"{parts[0]} {float(parts[1]):.1f}" if len(parts) > 1 and _isnum(parts[1]) else lic
                    mx = (x0 + x1) / 2
                    f = self.font(TEXT, 10 * s, 800)
                    tw2 = dt.textlength(t, font=f) + 8 * s
                    db.rounded_rectangle((mx - tw2 / 2, cy - 7 * s, mx + tw2 / 2, cy + 7 * s), 3 * s,
                                        fill=rgba(r.get("lic_color") or DIM, a))
                    col = "#FFFFFF" if _dark(r.get("lic_color")) else "#000000"
                    self.text(dt, mx, cy, s, t, 10, col, 800, anchor="mm", alpha=a)
            elif k == "pits":
                self.text(dt, x1, cy, s, r.get("pits", 0), 12.5 * nm, MID, 500, N, "rm", alpha=a)
            elif k == "pitlast":
                self.text(dt, x1, cy, s, r.get("pitlast", "—"), 12 * nm, MID, 500, N, "rm", alpha=a)
            elif k == "sec":
                cells = r.get("sec") or []
                if cells:
                    step = (x1 - x0) / len(cells)
                    for j, c in enumerate(cells):
                        col = PURPLE if c["c"] == "purple" else GREEN if c["c"] == "green" else MID
                        self.text(dt, x0 + step * (j + .5), cy, s, c["t"], 11.5 * nm, col, 500, N, "mm", alpha=a)
            elif k in ("last", "best"):
                c = r.get(k + "_c")
                col = PURPLE if c == "purple" else GREEN if c == "green" else (tone or MID)
                self.text(dt, x1, cy, s, r.get(k, "—"), 12.5 * nm, col, 500, N, "rm", alpha=a)
            elif k == "gap":
                self.text(dt, x1, cy, s, r.get("gap", ""), 12.5 * nm, tone or MID, 500, N, "rm", alpha=a)

    def _tile(self, key, size, draw):
        """Готовая плитка: рисуется один раз, дальше берётся из кэша. Таблица
        собирается из плиток — при смене отрыва рисуется одна ячейка, а не всё
        окно (полная перерисовка — ~12 мс, сборка из плиток — доли мс)."""
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        img = Image.new("RGBA", (max(1, int(size[0])), max(1, int(size[1]))), (0, 0, 0, 0))
        bg, tx, db, dt = _layers(img)
        draw(db, dt)
        _flat(img, bg, tx)
        if len(self._tiles) > 4000:
            self._tiles.clear()
        self._tiles[key] = img
        return img

    @staticmethod
    def _cell_key(k, r):
        """Что видно в ячейке колонки k — ключ её плитки."""
        if k == "ir":
            return r.get("ir")
        if k == "dir":
            return r.get("dir")
        if k == "lic":
            return (r.get("lic"), r.get("lic_color"))
        if k == "pits":
            return r.get("pits")
        if k == "sec":
            return tuple((c.get("t"), c.get("c")) for c in r.get("sec") or [])
        if k in ("last", "best"):
            return (r.get(k), r.get(k + "_c"))
        return r.get(k)

    @staticmethod
    def _info_items(hd: dict) -> tuple[list, list]:
        """Сведения сессии для Relative: строка под шапкой — температуры трассы и
        воздуха (значками) и свои инциденты; строка под пилотами — круг и время до
        конца сессии. Элемент: (подпись или «@значок», значение, цвет)."""
        top = []
        if hd.get("track_temp") is not None:
            top.append(("@track", f"{hd['track_temp']:.0f}°C", FG))
        if hd.get("air_temp") is not None:
            top.append(("@air", f"{hd['air_temp']:.0f}°C", FG))
        inc = hd.get("inc")
        if inc:
            n, lim = inc["n"], inc.get("limit")
            tone = FG if not lim or n < lim - 4 else (ACC if n < lim else RED)
            top.append(("Inc", f"{n}x/{lim}" if lim else f"{n}x", tone))
        bottom = []
        if hd.get("lap"):
            bottom.append(("Lap", f"{hd['lap']}/{hd['laps_total']}" if hd.get("laps_total") else str(hd["lap"]), FG))
        est = hd.get("est") or {}
        tt = hd.get("remain")
        if tt is None and est.get("time") is not None:
            tt = est["time"]                         # гонка на круги без часов: по темпу лидера
        if hd.get("finished"):
            bottom.append(("Race", "FINISH", GREEN))
        elif tt is not None:
            tt = int(tt)
            v = f"{tt // 3600}:{tt // 60 % 60:02d}:{tt % 60:02d}" if tt >= 3600 else f"{tt // 60}:{tt % 60:02d}"
            bottom.append(("Time left", ("~" + v) if hd.get("remain") is None else v, FG))
        return top, bottom

    def _icon(self, db, kind, x, cy, s, col):
        """Значок 16 px: термометр; у трассы под ним асфальт, у воздуха — ветер."""
        c = rgba(col)
        tx = x + 4 * s
        db.rounded_rectangle((tx - 1.6 * s, cy - 7 * s, tx + 1.6 * s, cy + 2 * s), 1.6 * s, outline=c, width=max(1, int(round(1.2 * s))))
        db.ellipse((tx - 3.2 * s, cy + 1 * s, tx + 3.2 * s, cy + 7.4 * s), fill=c)
        db.rectangle((tx - .5 * s, cy - 3 * s, tx + .5 * s, cy + 2 * s), fill=c)
        if kind == "track":                          # асфальт: полоса с разметкой
            db.line((x + 8.5 * s, cy + 6 * s, x + 17 * s, cy + 6 * s), fill=c, width=1.4 * s)
            for k in (0, 4):
                db.line((x + 10 * s + k * s, cy + 3.2 * s, x + 12 * s + k * s, cy + 3.2 * s), fill=c, width=1.2 * s)
        else:                                        # воздух: две линии ветра
            for yy, ln in ((-3.5, 8), (.5, 6)):
                db.line((x + 9 * s, cy + yy * s, x + (9 + ln) * s, cy + yy * s), fill=c, width=1.2 * s)
            db.ellipse((x + 15 * s, cy - 5.6 * s, x + 18 * s, cy - 2.6 * s), outline=c, width=max(1, int(round(1.1 * s * SS))))

    def _info_strip(self, db, dt, P, y, w, s, items, spread=False, line_top=False) -> None:
        """Строка сведений: подпись (или значок) мелко, значение цифрами. spread — первое
        у левого края, последнее у правого. Не влезает — уходят температуры (воздух, трасса)."""
        cy = y + INFO_H / 2 * s
        fl = self.font(TEXT, 8.5 * s, 800)
        fv = self.font(MONO, 11.5 * s, 600)

        def width(it):
            lab, val, _c = it
            lw = 18 * s if lab.startswith("@") else fl.getlength(lab.upper()) + 8.5 * .14 * s * len(lab)
            return lw + 6 * s + fv.getlength(val)
        room = w - 28 * s
        items = list(items)
        drop = ["@air", "@track"]
        while items and sum(width(i) for i in items) + 14 * s * (len(items) - 1) > room and drop:
            k = drop.pop(0)
            items = [i for i in items if i[0] != k]
        gap = 18 * s
        if spread and len(items) > 1:
            gap = max(10 * s, (room - sum(width(i) for i in items)) / (len(items) - 1))
        x = P + 14 * s
        for lab, val, col in items:
            if lab.startswith("@"):
                self._icon(db, lab[1:], x, cy, s, DIM)
                lw = 18 * s
            else:
                lw = self.text(dt, x, cy, s, lab.upper(), 8.5, DIM, 800, spacing=.14)
            self.text(dt, x + lw + 6 * s, cy, s, val, 11.5, col, 600, stack=MONO)
            x += width((lab, val, col)) + gap
        ly = y if line_top else y + INFO_H * s
        db.line((P, ly, P + w, ly), fill=rgba(LINE), width=max(1, int(s)))

    def _table(self, oid, data, s):
        """Неподвижная часть таблицы: слой содержимого (без панели) и где
        строка говорящего. Колонки говорящего — отдельной плиткой: в кадре
        они гаснут под волну. Собирается из плиток (_tile)."""
        cfg = data.get("cfg") or {}
        rel = oid == "relative"
        cols = self._cols(cfg, 52 if rel else 62, rel=rel)
        chg = (not rel) and bool((cfg.get("cols") or {}).get("chg")) and data.get("race")
        flags = bool((cfg.get("cols") or {}).get("flags"))
        w = self._table_w(cols, chg)
        lay = self._layout(cols, chg)
        if rel:
            blocks = [{"title": "", "rows": data.get("rows") or []}]
        else:
            blocks = data.get("blocks") or []
        mode = cfg.get("head") or "top"          # шапка: сверху, снизу или нет
        info, bottom = self._info_items(data.get("head") or {}) if rel else ([], [])
        h = (HEAD_H + 2 if mode != "off" else 0) + (2 * INFO_H if rel else 0) + HDR_H + 6
        for bl in blocks:
            h += 22 if bl.get("title") else 0
            for r in bl["rows"]:
                h += ROW_H + (7 if r.get("sep") else 0)
        if rel and not blocks[0]["rows"]:
            h += ROW_H
        W, H = int(w * s), int(h * s)
        P = int(PAD * s)
        size = (W + 2 * P, H + 2 * P)
        # шрифты из настроек окна: место, фамилия, цифры
        self._ty = {k: TYPO[k].get(str(cfg.get("font_" + k) or ""), TYPO[k][TYPO_DEFAULT[k]]) for k in TYPO}
        sk = (round(s, 3), W, tuple(cols), bool(chg),
              tuple(cfg.get("font_" + k) or "" for k in TYPO))     # раскладка и шрифты — часть ключа любой плитки
        content = Image.new("RGBA", size, (0, 0, 0, 0))
        # шапка и подписи колонок
        if rel:
            hd = data.get("head") or {}
            small = " · ".join(x for x in (hd.get("track") or "", f"SOF {hd['sof']}" if hd.get("sof") else "") if x)
            hk = ("hd", oid, sk, data.get("last"), small, hd.get("session"), hd.get("session_color"), tuple(info), tuple(bottom))

            hargs, gh = (small, "Relative", (hd.get("session"), hd.get("session_color")),
                         [("Last", data.get("last") or "—")]), "Gap"
        else:
            small = " · ".join(x for x in (data.get("track") or "", f"SOF {data['sof']}" if data.get("sof") else "") if x)
            right = []
            if data.get("track_temp") is not None:
                right.append(("Track", f"{data['track_temp']:.0f}°"))
            gr = data.get("grid")
            if gr:
                # до старта: сколько пилотов уже в машине из всех; все на месте — зелёным
                right.append(("Grid", f"{gr['n']}/{gr['total']}", GREEN if gr["n"] >= gr["total"] else FG))
            inc = data.get("inc")
            if inc:
                n, lim = inc["n"], inc.get("limit")
                # ближе четырёх до дисквалификации — оранжевый, на лимите — красный
                tone = FG if not lim or n < lim - 4 else (ACC if n < lim else RED)
                right.append(("Inc", f"{n}x/{lim}" if lim else f"{n}x", tone))
            if data.get("race") and data.get("lap"):
                right.append(("Lap", f"{data['lap']}/{data['laps_total']}" if data.get("laps_total") else str(data["lap"])))
            est = data.get("est") or {}
            tt = data.get("remain")
            if tt is None and est.get("time") is not None:
                tt = est["time"]                       # гонка на круги: время по темпу лидера
            if tt is not None:
                tt = int(tt)
                v = f"{tt // 3600}:{tt // 60 % 60:02d}:{tt % 60:02d}" if tt >= 3600 else f"{tt // 60}:{tt % 60:02d}"
                right.append(("Left", ("~" + v) if data.get("remain") is None else v))
            if data.get("finished"):
                right.append(("Race", "FINISH", GREEN))     # после клетчатого — вместо «Left»
            if est.get("laps") is not None:
                right.append(("Laps", f"~{est['laps']:.0f}"))  # гонка на время: круги по темпу лидера
            hk = ("hd", oid, sk, small, data.get("session"), data.get("session_color"), tuple(right), data.get("gap_head"))

            hargs = (small, "Standings", (data.get("session"), data.get("session_color")), right)
            gh = data.get("gap_head") or ""
        hh = (HEAD_H + 2) * s if mode == "top" else 0
        ih = INFO_H * s if rel else 0

        def head(db, dt):
            if mode == "top":
                self.header(db, dt, P, s, W, *hargs)
            if rel:
                self._info_strip(db, dt, P, P + hh, W, s, info)
            self._hdr(dt, P, s, P + hh + ih, lay, cols, gh)
        content.alpha_composite(self._tile(hk + (mode,), (size[0], int(P + hh + ih + HDR_H * s + 2)), head))
        y = P + hh + ih + HDR_H * s
        rh = int(round(ROW_H * s))
        talk = None
        line = rgba(LINE)
        cd = ImageDraw.Draw(content)
        for bl in blocks:
            if bl.get("title"):
                tk = ("ttl", sk, bl["title"], bl.get("color"))

                def ttl(db, dt, bl=bl):
                    cy = 11 * s
                    db.rounded_rectangle((12 * s, cy - 4 * s, 20 * s, cy + 4 * s), 2 * s, fill=rgba(bl.get("color") or DIM))
                    tw = self.text(dt, 28 * s, cy, s, bl["title"].upper(), 9.5, DIM, 800, spacing=.18)
                    db.line((36 * s + tw, cy, W - 12 * s, cy), fill=line, width=max(1, int(s)))
                content.alpha_composite(self._tile(tk, (W, 22 * s), ttl), (P, int(y)))
                y += 22 * s
            for r in bl["rows"]:
                if r.get("sep"):
                    cd.line((P + 12 * s, y + 3 * s, P + W - 12 * s, y + 3 * s), fill=line, width=max(1, int(s)))
                    y += 7 * s
                r["_flags"] = flags
                yi = int(round(y))
                # левая часть строки: место, ▲▼, номер, фамилия, флаг, подложка своей
                rk = ("row", sk, r["pos"], r.get("num"), r.get("name"), r.get("color"), r.get("color2"), bool(r.get("me")),
                      bool(r.get("pit")), r.get("flag") if flags else "", r.get("tone"),
                      r.get("chg") if chg else 0)
                content.alpha_composite(self._tile(rk, (W, rh), lambda db, dt, r=r: self._row(
                    db, dt, 0, s, 0, r, lay, cols, chg, W, tail=False)), (P, yi))
                cells = []
                for k, cw in cols:
                    x0 = lay[k][0] * s
                    ck = ("cell", sk, k, self._cell_key(k, r), r.get("tone"), bool(r.get("pit")))
                    m = 8 * s
                    img = self._tile(ck, (cw * s + 2 * m, rh), lambda db, dt, r=r, k=k, cw=cw, x0=x0, m=m: self._row(
                        db, dt, m - x0, s, 0, r, lay, [(k, cw)], chg, W, head=False))
                    cells.append((img, int(P + x0 - m)))
                if r.get("talk") and cols:
                    talk = (yi, cells, P + lay[cols[0][0]][0] * s)
                else:
                    for img, x in cells:
                        content.alpha_composite(img, (x, yi))
                y += ROW_H * s
        rows_end = y
        tail = None
        if talk:
            ty, cells, x0 = talk
            box = (int(x0), int(ty), int(P + W - 10 * s), int(ty + rh))
            timg = Image.new("RGBA", (box[2] - box[0] + int(16 * s), rh), (0, 0, 0, 0))
            for img, x in cells:
                dx = x - box[0]
                if dx < 0:
                    img, dx = img.crop((-dx, 0, img.width, img.height)), 0
                timg.alpha_composite(img, (dx, 0))
            tail = (timg.crop((0, 0, box[2] - box[0], rh)), box)
        foot = None
        bh = INFO_H * s if rel else 0
        if mode == "bottom" or rel:
            # низ — отдельной плиткой: строка «Говорит» раздвигает таблицу, и низ
            # (у Relative — круг и время, шапка, если она снизу) едет вместе с панелью
            fy = int(rows_end + 4 * s)

            def foot_draw(db, dt):
                if rel:
                    self._info_strip(db, dt, P, 0, W, s, bottom, spread=True, line_top=True)
                if mode == "bottom":
                    self.header(db, dt, P, s, W, *hargs, y0=bh + 2 * s)
            fh = int(bh + ((HEAD_H + 2) * s if mode == "bottom" else 0) + 2)
            foot = (self._tile(("ft",) + hk + (mode,), (size[0], fh), foot_draw), fy)
        return {"content": content, "W": W, "H": H, "P": P, "tail": tail, "rows_end": rows_end, "foot": foot}

    def _voice_time(self, v, now):
        """Местное время начала и конца реплики: по первому появлению в данных
        (данные приходят 10 раз в секунду — анимация не теряет начало)."""
        vt = self._vt
        if not v:
            if vt["id"] is not None and vt["end"] is None:
                vt["end"] = now
            return vt
        if v.get("id") == vt.get("done"):
            return vt                          # эту реплику уже доиграли
        if v.get("id") != vt["id"]:
            vt.update(id=v.get("id"), start=now, end=None)
        if v.get("ended") and vt["end"] is None:
            vt["end"] = now
        return vt

    def _speaker(self, s, now):
        """Динамик: корпус со скруглёнными углами и две дуги, вспыхивают по очереди.
        Кадры кэшируются по яркости дуг (12 ступеней) — рисовать на кадр незачем."""
        q1 = round((.45 + .55 * max(0.0, math.sin(now * 9))) * 11)
        q2 = round((.3 + .7 * max(0.0, math.sin(now * 9 - 1.3))) * 11)
        ck = ("spk", round(s, 3), q1, q2)
        hit = self._ind.get(ck)
        if hit is None:
            hit = self._ind[ck] = self._speaker_draw(s, q1 / 11, q2 / 11)
        return hit.copy()

    def _speaker_draw(self, s, a1, a2):
        k = 4
        n = max(2, int(16 * s))
        img = Image.new("RGBA", (n * k, n * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        u = n * k / 16
        col = rgba(ACC)
        d.rounded_rectangle((1.7 * u, 6.1 * u, 5 * u, 9.9 * u), 0.9 * u, fill=col)
        d.polygon([(4.6 * u, 6.1 * u), (8.4 * u, 3.3 * u), (9.1 * u, 3.6 * u), (9.1 * u, 12.4 * u),
                   (8.4 * u, 12.7 * u), (4.6 * u, 9.9 * u)], fill=col)
        for r, a in ((2.8, a1), (5.4, a2)):
            d.arc((8 * u - r * u + 2.6 * u, 8 * u - r * u, 8 * u + r * u + 2.6 * u, 8 * u + r * u), -48, 48,
                  fill=(col[0], col[1], col[2], int(255 * a)), width=max(1, int(1.5 * u)))
        return _down(img, (n, n))

    def _wave(self, w, h, p, now, phase=0.0):
        """Звуковая волна линией: несущая бежит слева направо, огибающая «дышит»,
        к краям затухает; основная линия и бледная со сдвигом фазы."""
        k = 2
        W, H = max(2, int(w)), max(2, int(h))
        img = Image.new("RGBA", (W * k, H * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        col = rgba(ACC)
        for ph, alpha, width in ((1.4, 90, 1.2), (0.0, 255, 1.8)):
            pts = []
            for i in range(0, W + 1, 3):
                x = i / W
                edge = math.sin(math.pi * min(1.0, x * 1.15)) ** 1.5
                env = .45 + .55 * abs(math.sin(x * 5.3 - now * 3.1) * math.cos(x * 2.1 + now * 1.7))
                y = H / 2 + math.sin(x * 26 - now * 14 + ph + phase) * (H / 2 - 1) * edge * env * p
                pts.append((i * k, y * k))
            d.line(pts, fill=(col[0], col[1], col[2], alpha), width=max(1, int(round(width * k * self.dpi))))
        return _down(img, (W, H))

    def _table_frame(self, oid, data, s, now):
        """Кадр таблицы: панель, неподвижный слой из кэша и поверх — рация."""
        v = data.get("voice")
        if self._key_of[0] is not data:
            self._key_of = (data, _key(data))
        key = (oid, round(s, 3), self._key_of[1])
        if not self._tbl or self._tbl[0] != key:
            self._tbl = (key, self._table(oid, data, s))
        T = self._tbl[1]
        vt = self._voice_time(v, now)
        active = vt["id"] is not None and (vt["end"] is None or now - vt["end"] < VOICE_OUT + 0.05)
        if not active and vt["id"] is not None and vt["end"] is not None:
            vt["done"], vt["id"] = vt["id"], None
        W, H, P = T["W"], T["H"], T["P"]
        # строка «Говорит», если говорящего нет в окне: место открывается,
        # строка въезжает; уход — в обратном порядке
        strip = bool(v and not v.get("in_rows")) or (not v and active and self._vt.get("strip"))
        if v:
            self._vt["strip"] = not v.get("in_rows")
            self._vt["who"] = v
        if strip and active:
            open_ = _ease((now - vt["start"]) / .4) * (_ease((vt["end"] + VOICE_OUT - now) / .3) if vt["end"] else 1)
            show = _ease((now - vt["start"] - .15) / .3) * (_ease((vt["end"] + VOICE_OUT - .15 - now) / .3) if vt["end"] else 1)
        else:
            open_ = show = 0.0
        sh = int(round(STRIP_H * s * open_))
        # панель с таблицей склеиваем один раз на высоту: каждый кадр — только копия
        edge = YELLOW if data.get("flag_tone") == "yellow" else None     # полный жёлтый — кромка жёлтая
        bk = (id(T), sh, edge)
        base = T.get("base")
        if not base or base[0] != bk:
            img = self._panel_cut(W, H, sh, s, edge)
            img.alpha_composite(T["content"])
            T["base"] = base = (bk, img)
        img = base[1].copy()
        p = 0.0
        if active:
            p = _ease((now - vt["start"]) / VOICE_IN) * (_ease((vt["end"] + VOICE_OUT - now) / VOICE_OUT) if vt["end"] else 1)
        tail = T["tail"]
        if tail:
            timg, box = tail
            if p < 1:
                fade = timg if p <= 0 else timg.copy()
                if p > 0:
                    fade.putalpha(fade.getchannel("A").point(lambda a: int(a * (1 - p))))
                img.alpha_composite(fade, box[:2])
            if p > 0:
                self._talk_row(img, box, s, p, now, vt)
        if sh > 0 and self._vt.get("who"):
            self._strip(img, P, T["rows_end"], W, s, sh, show, now, self._vt["who"])
        if T.get("foot"):
            fimg, fy = T["foot"]
            img.alpha_composite(fimg, (0, fy + sh))
        # 60 кадров — пока что-то въезжает, гаснет или проходит отсвет; пока
        # человек просто говорит, волне хватает 30 — вдвое меньше работы
        steady = active and vt["end"] is None and now - vt["start"] > max(SWEEP_S, .5)
        waving = self._flag_wave(img, data, now, P, s)
        self.fps = 30 if steady and not waving else 60
        return img, active or waving

    def _flag_wave(self, img, data, now, P, s) -> bool:
        """Выброс жёлтого или зелёного: волна сверху вниз — полоса света и вспышка
        каждой строки в момент её прохода, как в башне трансляции (ov_paint.wave)."""
        ev = data.get("flag_event") or {}
        if ev.get("id") is not None and ev.get("id") != self._wave_id:
            self._wave_id = ev.get("id")
            age = ev.get("age")
            # окно открыли позже выброса — не догоняем
            if isinstance(age, (int, float)) and 0 <= age < 1.5:
                self._wave = {"t0": now - age, "tone": ev.get("tone")}
        w = self._wave
        if not w:
            return False
        t = now - w["t0"]
        if t >= WAVE_S + LIT_S:
            self._wave = None
            return False
        col = GREEN if w["tone"] == "green" else YELLOW
        c = rgba(col)
        W, H = img.width - 2 * P, img.height - 2 * P
        step = ROW_H * s
        rects = []                                      # (y0, y1, альфа) вспышек строк
        y = 0.0
        while y < H:
            k = (t - max(0.0, (y + step / 2) / H * WAVE_S - 0.12)) / LIT_S
            if 0 < k < 1:
                a = k / .3 if k < .3 else (1 - k) / .7
                rects.append((y, min(H, y + step), int(255 * .38 * a)))
            y += step
        band = None
        if t < WAVE_S:                                  # полоса света
            bh = int(60 * s)
            band = (-bh / 2 + t / WAVE_S * (H + bh), bh)
        ys = [r[0] for r in rects] + [r[1] for r in rects]
        if band:
            ys += [band[0], band[0] + band[1]]
        if not ys:
            return True
        y0, y1 = max(0, int(min(ys))), min(H, int(max(ys)) + 1)
        if y1 <= y0:
            return True
        # слой — только на полосу, где сейчас волна: на всё окно каждый кадр стоило заметно дороже
        layer = Image.new("RGBA", (W, y1 - y0), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer, "RGBA")
        for a0, a1, al in rects:
            d.rectangle((0, a0 - y0, W, a1 - y0), fill=c[:3] + (al,))
        if band:
            by, bh = band
            for i in range(bh):
                yy = by + i - y0
                if 0 <= yy < y1 - y0:
                    a = .55 * (1 - abs(i / bh * 2 - 1))
                    d.line((0, yy, W, yy), fill=c[:3] + (int(255 * a),))
        img.alpha_composite(layer, (P, P + y0))
        return True

    def _panel_cut(self, W, H, sh, s, edge=None):
        """Панель высотой H + sh — срезом одной заготовки высотой H + STRIP_H:
        верх берётся как есть, низ (скругление и тень) подставляется на новую
        высоту. Рисовать панель заново на каждую высоту — до 45 мс на кадр."""
        full = int(round(STRIP_H * s))
        tall = self.panel(W, H + full, s, edge=edge)
        if sh >= full:
            return tall
        P = int(PAD * s)
        B = P + int(14 * s)                          # низ: скругление, кромка, тень
        h = H + sh + 2 * P
        out = Image.new("RGBA", (tall.width, h), (0, 0, 0, 0))
        out.paste(tall.crop((0, 0, tall.width, h - B)), (0, 0))
        out.paste(tall.crop((0, tall.height - B, tall.width, tall.height)), (0, h - B))
        return out

    def _talk_row(self, img, box, s, p, now, vt):
        """Говорящий: отсвет по строке в начале речи, динамик и волна на месте колонок."""
        x0, y0, x1, y1 = box
        rw, rh = x1 - x0, y1 - y0
        P = int(PAD * s)
        k = (now - vt["start"]) / SWEEP_S
        if 0 <= k <= 1:
            # мягкий оранжевый отсвет проходит строку слева направо — как волна флага
            full = img.width - 2 * P - 8 * s
            n = max(2, int(full))
            peak = 0.20 * math.sin(math.pi * k)
            xc = -0.25 + k * 1.5
            prof = [int(255 * peak * max(0.0, 1 - abs(i / n - xc) / 0.3)) for i in range(n)]
            band = Image.new("L", (n, 1))
            band.putdata(prof)
            band = band.resize((n, max(1, rh - int(2 * s))))
            layer = Image.new("RGBA", band.size, (255, 90, 31, 0))
            layer.putalpha(band)
            img.alpha_composite(layer, (int(P + 4 * s), int(y0 + s)))
        a = min(1.0, p * 1.4)
        spk = self._speaker(s, now)
        wav = self._wave(rw - spk.width - 10 * s, 18 * s, p, now)
        for im, x in ((spk, x0), (wav, x0 + spk.width + int(8 * s))):
            if a < 1:
                im.putalpha(im.getchannel("A").point(lambda v: int(v * a)))
            img.alpha_composite(im, (int(x), int(y0 + (rh - im.height) / 2)))

    def _strip(self, img, P, y_end, W, s, sh, show, now, v):
        """Строка «Говорит» под таблицей: линия сверху, лёгкий оттенок, динамик,
        номер, фамилия, волна, частота. Видна только открытая часть."""
        h = int(STRIP_H * s)
        top = 3 * s
        cy = top + 13 * s
        spk = self._speaker(s, now)
        fq = v.get("freq") or ""
        ck = (W, h, round(s, 3), v.get("num"), v.get("name"), fq)
        hit = self._strip_cache if getattr(self, "_strip_cache", None) and self._strip_cache[0] == ck else None
        if hit is None:
            lay = Image.new("RGBA", (W, h), (0, 0, 0, 0))
            bg, tx, db, dt = _layers(lay)
            db.line((0, top, W, top), fill=rgba(LINE), width=max(1, int(s)))
            x = 8 * s + spk.width + 8 * s
            db.rounded_rectangle((x, cy - 9 * s, x + 30 * s, cy + 9 * s), 3 * s, fill=rgba("#232C3D"))
            self.text(dt, x + 15 * s, cy, s, v.get("num") or "", 11.5, FG, 800, anchor="mm")
            x += 38 * s
            nw = self.text(dt, x, cy, s, str(v.get("name") or "").upper(), 13.5, "#FFFFFF", 700, spacing=.03, maxw=150 * s)
            fw = self.font(MONO, 10.5 * s, 600).getlength(fq) if fq else 0
            if fq:
                self.text(dt, W - 10 * s, cy, s, fq, 10.5, DIM, 600, MONO, "rm")
            _flat(lay, bg, tx)
            # оттенок слева направо
            grad = Image.linear_gradient("L").rotate(90).resize((W, h)).point(lambda g: int(g * 0.10))
            tint = Image.new("RGBA", (W, h), (255, 90, 31, 0))
            tint.putalpha(grad)
            base = Image.new("RGBA", (W, h), (0, 0, 0, 0))
            base.alpha_composite(tint, (0, int(top) + 1))
            base.alpha_composite(lay)
            self._strip_cache = hit = (ck, base, x, nw, fw)
        _ck, base, x, nw, fw = hit
        out = base.copy()
        out.alpha_composite(spk, (int(8 * s), int(cy - spk.height / 2)))
        ww = int(W - (x + nw + 12 * s) - (fw + 20 * s if fq else 10 * s))
        if ww > 20:
            wav = self._wave(min(ww, int(110 * s)), 16 * s, show, now)
            out.alpha_composite(wav, (int(x + nw + 12 * s), int(cy - wav.height / 2)))
        if show < 1:
            out.putalpha(out.getchannel("A").point(lambda a: int(a * show)))
        # въезжает сверху: сдвиг до 8 px, видна только открытая часть
        dy = int((1 - show) * -8 * s)
        clip = Image.new("RGBA", (W, sh), (0, 0, 0, 0))
        clip.alpha_composite(out, (0, dy))
        img.alpha_composite(clip, (P, int(y_end)))

    # ---------- радар ----------

    @staticmethod
    def _curve(side, W, H, wd, bow, bend, n=120):
        """Средняя линия полосы: квадратичная кривая сверху вниз, изгиб — смещение середины."""
        m = RADAR_M
        x = m + wd / 2 + bow if side == "l" else W - m - wd / 2 - bow
        dx = (-1 if side == "l" else 1) * bend * 2
        y0, y1, ym = m + wd / 2, H - m - wd / 2, H / 2
        pts = []
        for i in range(n + 1):
            u = i / n
            pts.append(((1 - u) ** 2 * x + 2 * (1 - u) * u * (x + dx) + u * u * x,
                        (1 - u) ** 2 * y0 + 2 * (1 - u) * u * ym + u * u * y1))
        return pts

    @staticmethod
    def _shift(pts, o):
        """Линия, сдвинутая поперёк на o (по нормали к кривой)."""
        out = []
        for i, (x, y) in enumerate(pts):
            a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(dx, dy) or 1.0
            out.append((x - dy / ln * o, y + dx / ln * o))
        return out

    @staticmethod
    def _band(pts, r, k):
        """Контур толстой линии со скруглёнными концами — один многоугольник:
        его края PIL сглаживает уменьшением, а не швами между кругами."""
        if len(pts) < 2:
            return []
        left, right = [], []
        for i, (x, y) in enumerate(pts):
            a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / ln, dx / ln
            left.append(((x + nx * r) * k, (y + ny * r) * k))
            right.append(((x - nx * r) * k, (y - ny * r) * k))

        def cap(c, d, sign):
            # полукруг вокруг конца c, наружу по направлению d
            ang = math.atan2(d[1], d[0])
            return [((c[0] + math.cos(ang + sign * (math.pi / 2 - math.pi * j / 12)) * r) * k,
                     (c[1] + math.sin(ang + sign * (math.pi / 2 - math.pi * j / 12)) * r) * k) for j in range(13)]

        end_d = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
        start_d = (pts[0][0] - pts[1][0], pts[0][1] - pts[1][1])
        return left + cap(pts[-1], end_d, 1) + right[::-1] + cap(pts[0], start_d, 1)

    def radar(self, data, s, now=0.0):
        """Две полосы по бокам; индикатор — участок полосы там, где по длине
        машины стоит соседняя. Изгиб: + — скобками наружу «( )», − — внутрь.
        Рисуется вчетверо крупнее и уменьшается — края гладкие."""
        R = data.get("cfg") or {}
        spread, ln, wd, bend = (float(R.get(k, d)) for k, d in (("spread", 420), ("len", 110), ("width", 26), ("bend", 0)))
        bow = abs(bend)
        W, H = spread + wd + bow * 2 + 2 * RADAR_M, ln + wd + 2 * RADAR_M
        k = s * 4
        out = (max(1, int(W * s)), max(1, int(H * s)))
        size = (out[0] * 4, out[1] * 4)          # ровно вчетверо — уменьшение без дробных пикселей
        key = ("radar", spread, ln, wd, bend, round(s, 3), round(self.bg, 2))
        bases = {}
        for side in ("l", "r"):
            # полоса каждой стороны — отдельно: на экране только та, где машина
            bk = key + (side,)
            base = self._panel.get(bk)
            if base is None:
                big = Image.new("RGBA", size, (0, 0, 0, 0))
                d = ImageDraw.Draw(big)
                pts = self._curve(side, W, H, wd, bow, bend)
                d.polygon(self._band(pts, wd / 2 + 1, k), fill=(255, 255, 255, int(46 * min(1.0, self.bg / .75))))
                d.polygon(self._band(pts, wd / 2, k), fill=(20, 25, 35, int(255 * self.bg)))
                base = self._panel[bk] = _down(big, out)
            bases[side] = base
        img = Image.new("RGBA", out, (0, 0, 0, 0))
        # оранжевое — часть плашки, которую перекрывает корпус соседа
        Lc = float(data.get("car_m") or 5.0)
        cars = list(data.get("cars") or [])
        demo = bool(R.get("test")) and not cars
        if demo:
            cars = radar_script(now, Lc)               # «индикатор всегда»: сценарий мокапа
        sides = {"l": [c for c in cars if c.get("s") == "l"], "r": [c for c in cars if c.get("s") == "r"]}
        lit = {k: bool(v) for k, v in sides.items()}
        if demo or data.get("demo") or R.get("test"):
            lit = {"l": True, "r": True}               # демо и настройка вида — обе полосы всегда видны
        # плашка появляется и гаснет сама по себе за RADAR_FADE; окно только
        # что показалось (кадров давно не было) — плашки сразу на месте
        fa = self._rad_fade
        snap = now - fa["t"] > 0.5
        dt = 0.0 if snap else now - fa["t"]
        fa["t"] = now
        moving = False
        for side in ("l", "r"):
            want = 1.0 if lit[side] else 0.0
            if snap:
                fa[side] = want
            elif fa[side] != want:
                step = dt / RADAR_FADE
                fa[side] = min(want, fa[side] + step) if want > fa[side] else max(want, fa[side] - step)
            moving = moving or fa[side] != want
        for side in ("l", "r"):
            a = fa[side]
            if a <= 0:
                continue
            layer = bases[side].copy()
            for c in sides[side]:
                # 0 — ваш нос, 1 — ваш зад; всё, что вне машины, не показываем
                y0 = max(0.0, -c["dm"] / Lc)
                y1 = min(1.0, 1 - c["dm"] / Lc)
                if y1 <= y0:
                    continue
                tile, at = self._indicator(key, side, round(y0 * 200), round(y1 * 200), R, W, H, wd, bow, bend, s, c.get("lane"))
                layer.alpha_composite(tile, at)
            if a < 1:
                e = _ease(a)
                layer.putalpha(layer.getchannel("A").point(lambda v: int(v * e)))
            img.alpha_composite(layer)
        return img, moving or demo

    def _indicator(self, key, side, q0, q1, R, W, H, wd, bow, bend, s, lane=None):
        """Перекрытие: участок плашки от q0 до q1 (двухсотые её высоты), концы
        скруглены, края скругления — точно на границах корпуса соседа. Плитки
        кэшируются: на кадр — одна склейка."""
        ck = (key, side, q0, q1, R.get("color"), R.get("alpha"), lane)
        hit = self._ind.get(ck)
        if hit is not None:
            return hit
        col = rgba(R.get("color") or ACC)
        alpha = max(0.1, min(1.0, float(R.get("alpha", 100)) / 100))
        full = max(1.0, (wd - 4) / 2)
        r0 = max(1.0, (full - 1) / 2) if lane is not None else full
        top, bot = RADAR_M, H - RADAR_M
        ya, yb = top + q0 / 200 * (bot - top), top + q1 / 200 * (bot - top)
        r = min(r0, (yb - ya) / 2)                       # короткое перекрытие — круглая точка
        x = RADAR_M + wd / 2 + bow if side == "l" else W - RADAR_M - wd / 2 - bow
        dx = (-1 if side == "l" else 1) * bend * 2
        y_0, y_1 = RADAR_M + wd / 2, H - RADAR_M - wd / 2
        off = 0.0 if lane is None else (r0 + 1) * (-1 if lane == 0 else 1)
        # центр скругления не выходит за закруглённый конец плашки
        lim = math.sqrt(max(0.0, (wd / 2 - r) ** 2 - off ** 2))
        c0 = max(ya + r, y_0 - lim)
        c1 = max(c0, min(yb - r, y_1 + lim))
        pts = []
        n = 24
        for i in range(n + 1):
            y = c0 + (c1 - c0) * i / n
            u = (y - y_0) / (y_1 - y_0)
            pts.append((x + dx * 2 * u * (1 - u), y))
        if c1 - c0 < 0.02:
            pts = [pts[0], (pts[0][0], pts[0][1] + 0.02)]
        if lane is not None:
            pts = self._shift(pts, -off)                 # дорожка — поперёк плашки, по нормали к изгибу
        poly = self._band(pts, r, 1.0)
        m = 12                                           # поле под ореол, px при 100 %
        x0 = max(0.0, min(p[0] for p in poly) - m)
        y0 = max(0.0, min(p[1] for p in poly) - m)
        x1 = min(W, max(p[0] for p in poly) + m)
        y1 = min(H, max(p[1] for p in poly) + m)
        ox, oy = int(x0 * s), int(y0 * s)
        w, h = max(1, int(x1 * s) - ox), max(1, int(y1 * s) - oy)
        big = Image.new("RGBA", (w * 4, h * 4), (0, 0, 0, 0))
        ImageDraw.Draw(big).polygon([((px * s - ox) * 4, (py * s - oy) * 4) for px, py in poly],
                                    fill=(col[0], col[1], col[2], 255))
        ind = _down(big, (w, h))
        halo = ind.convert("RGBa").filter(ImageFilter.GaussianBlur(5 * s)).convert("RGBA")
        if alpha < 1:
            ind.putalpha(ind.getchannel("A").point(lambda v: int(v * alpha)))
            halo.putalpha(halo.getchannel("A").point(lambda v: int(v * alpha)))
        halo.alpha_composite(ind)
        if len(self._ind) > 3000:
            self._ind.clear()
        self._ind[ck] = (halo, (ox, oy))
        return self._ind[ck]

    # ---------- ветер ----------

    def _wind_base(self, data, s):
        """Неподвижная часть окна ветра: панель, подписи, дорожка-кольцо как
        полосы радара, машина. Меняется только при смене подписей."""
        ok = data.get("ok")
        hw = data.get("head", 0)
        key = ("wbase", round(s, 3), ok, data.get("kind"), round(data.get("kmh") or 0), round(abs(hw)), hw >= 0,
               round(self.bg, 2))
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        W, H = 170, HEAD_H + 150 + 34
        img = self.panel(int(W * s), int(H * s), s)
        bg, tx, db, dt = _layers(img)
        P = int(PAD * s)
        self.header(db, dt, P, s, int(W * s), "Wind", data.get("kind", "—") if ok else "no data")
        n = int(140 * s)
        big = n * 4
        u, c = big / 140, big / 2

        def ring(r, width, fill):
            lay = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            ImageDraw.Draw(lay).ellipse((c - r * u, c - r * u, c + r * u, c + r * u), outline=fill, width=max(1, int(width * u)))
            return lay
        g = ring(58, 16, (255, 255, 255, 36))             # кромка
        g.alpha_composite(ring(57, 14, (20, 25, 35, 191)))  # дорожка
        ImageDraw.Draw(g).rounded_rectangle((c - 8 * u, c - 18 * u, c + 8 * u, c + 18 * u), 5 * u, fill=rgba(MID),
                                            outline=(255, 255, 255, 255), width=max(1, int(1.5 * u)))
        img.alpha_composite(_down(g, (n, n)), (int(P + 15 * s), int(P + (HEAD_H + 5) * s)))
        y = P + (HEAD_H + 150 + 14) * s
        if ok:
            self.text(dt, P + 12 * s, y - 6 * s, s, "SPEED", 9, DIM, 800, spacing=.16)
            self.text(dt, P + 12 * s, y + 8 * s, s, f"{data['kmh']:.0f} km/h", 12, FG, 700, MONO)
            self.text(dt, P + (W - 12) * s, y - 6 * s, s, "HEAD" if hw >= 0 else "TAIL", 9, DIM, 800, anchor="rm", spacing=.16)
            self.text(dt, P + (W - 12) * s, y + 8 * s, s, f"{abs(hw):.0f}", 12, FG, 700, MONO, "rm")
        _flat(img, bg, tx)
        if len(self._tiles) > 4000:
            self._tiles.clear()
        self._tiles[key] = img
        return img

    def _wind_tri(self, s, deg):
        """Треугольник ветра под углом deg (целые градусы, готовые плитки):
        скруглённые углы и свечение, как у индикатора радара, остриём к машине."""
        key = ("wtri", round(s, 3), deg)
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)
        vs = [(x * ca - y * sa, x * sa + y * ca) for x, y in ((0, -38), (9.5, -56.5), (-9.5, -56.5))]
        n = int(140 * s)
        c = n / 2
        m = 14                                             # поле под свечение
        ox = math.floor(c + (min(v[0] for v in vs) - m) * s)
        oy = math.floor(c + (min(v[1] for v in vs) - m) * s)
        w = math.ceil(c + (max(v[0] for v in vs) + m) * s) - ox
        h = math.ceil(c + (max(v[1] for v in vs) + m) * s) - oy
        big = Image.new("RGBA", (w * 4, h * 4), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        pts = [((c + x * s - ox) * 4, (c + y * s - oy) * 4) for x, y in vs]
        col = rgba(ACC)
        d.polygon(pts, fill=col)
        r = 1.6 * s * 4                                    # скругление углов
        for q in range(3):
            d.line((pts[q], pts[(q + 1) % 3]), fill=col, width=max(1, int(r * 2)))
            d.ellipse((pts[q][0] - r, pts[q][1] - r, pts[q][0] + r, pts[q][1] + r), fill=col)
        tri = _down(big, (w, h))
        tile = tri.convert("RGBa").filter(ImageFilter.GaussianBlur(5 * s)).convert("RGBA")
        tile.alpha_composite(tri)
        self._tiles[key] = (tile, (ox, oy))
        return self._tiles[key]

    def _wind_ring_tile(self, s, col, op):
        """Кольцо вида «кольцо»: цвет и прозрачность квантуются — плитки на переходе."""
        key = ("wring", round(s, 3), col, op)
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        n = int(140 * s)
        big = n * 4
        u, c = big / 140, big / 2
        lay = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(lay).ellipse((c - 56 * u, c - 56 * u, c + 56 * u, c + 56 * u),
                                    outline=(*col, int(255 * op)), width=int(12 * u))
        img = _down(lay, (n, n))
        if len(self._tiles) > 4000:
            self._tiles.clear()
        self._tiles[key] = img
        return img

    def _wind_text_tile(self, s, txt):
        """Скорость в центре: белые цифры и KM/H с лёгкой тенью, без обводки."""
        key = ("wtxt", round(s, 3), txt)
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        n = int(140 * s)
        c = n / 2
        lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)

        def draw(dd, col, dy=0.0):
            self.text(dd, c, c - 4 * s + dy, s, txt, 30, col, 700, MONO, "mm")
            self.text(dd, c, c + 19 * s + dy, s, "KM/H", 9.5, col, 800, anchor="mm", spacing=.16)
        sh = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        draw(ImageDraw.Draw(sh), "#000000", 1 * s)
        sh = sh.filter(ImageFilter.GaussianBlur(2 * s))
        sh.putalpha(sh.getchannel("A").point(lambda v: int(v * .55)))
        draw(d, "#FFFFFF")
        sh.alpha_composite(lay)
        self._tiles[key] = sh
        return sh

    def _wind_ring(self, data, s, now, ang):
        """Вид «кольцо»: без панели, на прозрачном фоне. Цвет кольца — по
        скорости (WIND_BANDS), переходит к новой полосе плавно."""
        ok = data.get("ok")
        kmh = float(data.get("kmh") or 0) if ok else 0.0
        _, hexc, op = next(b for b in WIND_BANDS if kmh < b[0])
        tgt = [int(hexc[i:i + 2], 16) for i in (1, 3, 5)]
        st = self._wc
        dt = now - st.get("t", 0.0)
        st["t"] = now
        if "c" not in st or dt > 0.5:
            st["c"], st["o"] = list(tgt), op
        else:
            k = 1 - math.exp(-max(0.0, dt) / WIND_COLOR_TAU)
            st["c"] = [x + (y - x) * k for x, y in zip(st["c"], tgt)]
            st["o"] += (op - st["o"]) * k
        col = tuple(int(round(x / 4) * 4) for x in st["c"])
        o = round(st["o"] * 50) / 50
        moving = max(abs(x - y) for x, y in zip(st["c"], tgt)) > 2 or abs(st["o"] - op) > .01
        n = int(140 * s)
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        img.alpha_composite(self._wind_ring_tile(s, col, o))
        img.alpha_composite(self._wind_text_tile(s, f"{kmh:.0f}" if ok else "—"))
        if ok:
            tile, (ox, oy) = self._wind_tri(s, int(round(ang)) % 360)
            img.alpha_composite(tile, (ox, oy))
        return img, moving

    def wind(self, data, s, now):
        """Окно ветра: каждый кадр — копия готовой основы и плитка треугольника.
        Угол доводится плавно: данные могут приходить реже кадров."""
        st = self._wa
        target = float(data.get("rel") or 0)
        dt = now - st["t"]
        st["t"] = now
        if st["a"] is None or dt > 0.5:
            st["a"] = target
        else:
            st["a"] = (st["a"] + ((target - st["a"] + 180) % 360 - 180) * (1 - math.exp(-max(0.0, dt) / WIND_TAU))) % 360
        ok = data.get("ok")
        moving = bool(ok) and abs((target - st["a"] + 180) % 360 - 180) > 0.05
        if ((data.get("cfg") or {}).get("style") or "ring") == "ring":
            img, mv = self._wind_ring(data, s, now, st["a"])
            return img, moving or mv
        img = self._wind_base(data, s).copy()
        if ok:
            tile, (ox, oy) = self._wind_tri(s, int(round(st["a"])) % 360)
            P = int(PAD * s)
            img.alpha_composite(tile, (int(P + 15 * s) + ox, int(P + (HEAD_H + 5) * s) + oy))
        return img, moving

    # ---------- двигатель ----------

    def _engine_base(self, data, s, W, H, gh, bars, press, rpm, msg, unit, u, tint):
        """Неподвижная часть окна двигателя: панель, шапка, подписи, шкалы,
        отметки лампы. Меняется только при смене ламп, единиц и настроек."""
        key = ("ebase", round(s, 3), W, H, bars, press, rpm, msg, unit, tint, data.get("ok"), data.get("lamp_water"),
               data.get("lamp_oil"), bool(data.get("warn_water")), bool(data.get("warn_oil")), round(self.bg, 2))
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        img = self.panel(int(W * s), int(H * s), s, tint=tint)
        bg, tx, db, dt = _layers(img)
        P = int(PAD * s)
        cy = P + 17 * s
        self.text(dt, P + 14 * s, cy - 6 * s, s, "ENGINE", 9, ACC, 800, spacing=.2)
        self.text(dt, P + 14 * s, cy + 7 * s, s, ("WARNING" if msg else "OK"), 13.5, RED if msg else FG, 800, spacing=.08)
        db.line((P, P + 34 * s, P + W * s, P + 34 * s), fill=rgba(LINE), width=max(1, int(s)))
        y = P + 40 * s
        if not data.get("ok"):
            self.text(dt, P + W / 2 * s, y + 30 * s, s, "no data", 12, DIM, 700, anchor="mm")
        else:
            def gauge(y, name, lo, hi, lamp, on):
                self.text(dt, P + 12 * s, y + 12 * s, s, name.upper(), 9.5, DIM, 800, spacing=.18)
                self.text(dt, P + (W - 12) * s, y + 14 * s, s, unit, 12, DIM, 500, MONO, "rm")
                if not bars:
                    return
                ty = y + 30 * s
                x0, x1 = P + 12 * s, P + (W - 12) * s
                pct = lambda x: max(0.0, min(1.0, (x - lo) / (hi - lo)))          # noqa: E731
                # шкала без зон: зон iRacing не отдаёт; красная, пока горит лампа
                k = s * SS
                bar = Image.new("RGBA", (int((x1 - x0) * SS), int(8 * k)), (0, 0, 0, 0))
                ImageDraw.Draw(bar).rounded_rectangle((0, 0, bar.width - 1, bar.height - 1), 4 * k, fill=rgba(RED if on else MID, .35))
                bg.alpha_composite(bar, (int(x0 * SS), int(ty * SS)))
                if lamp:
                    # здесь у этой машины уже загоралась лампа сима
                    lx = x0 + pct(lamp) * (x1 - x0)
                    db.rectangle((lx - 1 * s, ty - 2 * s, lx + 1 * s, ty + 10 * s), fill=rgba(RED))
                sy = ty + 18 * s
                self.text(dt, x0, sy, s, f"{u(lo):.0f}", 9.5, "#5C5C66", 500, MONO)
                if lamp:
                    self.text(dt, x0 + pct(lamp) * (x1 - x0), sy, s, f"lamp {u(lamp):.0f}", 9.5, "#A04545", 500, MONO, "mm")
                self.text(dt, x1, sy, s, f"{u(hi):.0f}", 9.5, "#5C5C66", 500, MONO, "rm")

            gauge(y, "Water", 60, 140, data.get("lamp_water"), data.get("warn_water"))
            y += gh * s
            gauge(y, "Oil", 60, 150, data.get("lamp_oil"), data.get("warn_oil"))
            y += gh * s
            if rpm:
                db.line((P, y, P + W * s, y), fill=rgba(LINE), width=max(1, int(s)))
                self.text(dt, P + 12 * s, y + 14 * s, s, "RPM", 9.5, DIM, 800, spacing=.18)
                y += 28 * s
            if press:
                db.line((P, y, P + W * s, y), fill=rgba(LINE), width=max(1, int(s)))
                self.text(dt, P + 12 * s, y + 14 * s, s, "OIL PRESSURE", 9.5, DIM, 800, spacing=.18)
        _flat(img, bg, tx)
        if len(self._tiles) > 4000:
            self._tiles.clear()
        self._tiles[key] = img
        return img

    def _label(self, img, x, y, s, txt, size, color, weight, stack=MONO):
        """Цифры прижатые вправо к x, по середине y — готовой плиткой. Возвращает ширину."""
        f = self.font(stack, size * s, weight)
        w = math.ceil(f.getlength(txt)) + 2
        h = math.ceil(size * s * 1.5)
        tile = self._tile(("lbl", round(s, 3), txt, size, color, weight, stack), (w, h), lambda db, dt: self.text(
            dt, w, h / 2, s, txt, size, color, weight, stack, "rm"))
        img.alpha_composite(tile, (int(round(x - w)), int(round(y - h / 2))))
        return f.getlength(txt)

    def _marker(self, img, x, y, s):
        """Метка на шкале: положение с точностью до полпикселя, плитка на каждую долю."""
        x2 = int(round(x * 2))
        fx = (x2 % 2) / 2
        w, h = math.ceil(6 * s), math.ceil(16 * s)
        tile = self._tile(("mark", round(s, 3), fx), (w, h), lambda db, dt: db.rounded_rectangle(
            (w / 2 + fx - 2 * s, s, w / 2 + fx + 2 * s, 15 * s), 2 * s, fill=(255, 255, 255, 255)))
        img.alpha_composite(tile, (x2 // 2 - w // 2, int(round(y - 4 * s))))

    # ---------- двигатель: полукруги ----------

    def _eng_col(self, key, rgb, now, snap=False):
        """Цвет дуги переходит плавно (ENG_COLOR_TAU); мигание — сразу."""
        st = self._ec.get(key)
        if st is None or snap or now - st[1] > 0.5:
            self._ec[key] = (list(rgb), now)
            return tuple(int(x) for x in rgb), False
        k = 1 - math.exp(-max(0.0, now - st[1]) / ENG_COLOR_TAU)
        c = [a + (b - a) * k for a, b in zip(st[0], rgb)]
        self._ec[key] = (c, now)
        return tuple(int(round(x / 4) * 4) for x in c), max(abs(a - b) for a, b in zip(c, rgb)) > 2

    def _eng_arc(self, s, fq, col, mq, hot):
        """Полукруг 270°: дорожка, заполнение до fq/200, риска лампы на mq/200."""
        key = ("earc", round(s, 3), fq, col, mq, hot, round(self.bg, 2))
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        n = int(ENG_D * s)
        k = 4
        big = Image.new("RGBA", (n * k, n * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        u = n * k / ENG_D
        c, R, wd = n * k / 2, 19 * u, 5 * u

        def arc(a0, a1, fill):
            d.arc((c - R - wd / 2, c - R - wd / 2, c + R + wd / 2, c + R + wd / 2), a0, a1, fill=fill, width=int(wd))
            for a in (a0, a1):                         # скруглённые концы
                x, y = c + R * math.cos(math.radians(a)), c + R * math.sin(math.radians(a))
                d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill=fill)
        arc(135, 405, (20, 25, 35, int(min(255, 140 * self.bg / .92))))
        if fq > 0:
            arc(135, 135 + 270 * fq / 200, (*col, 255))
        if mq is not None:
            a = math.radians(135 + 270 * mq / 200)
            d.line((c + 14.5 * u * math.cos(a), c + 14.5 * u * math.sin(a), c + 23.5 * u * math.cos(a),
                    c + 23.5 * u * math.sin(a)), fill=rgba(RED), width=int(2 * u))
        img = _down(big, (n, n))
        if hot:                                        # горит лампа — дуга светится
            glow = img.convert("RGBa").filter(ImageFilter.GaussianBlur(2.5 * s)).convert("RGBA")
            glow.alpha_composite(img)
            img = glow
        if len(self._tiles) > 4000:
            self._tiles.clear()
        self._tiles[key] = img
        return img

    def _eng_face(self, s, txt, red, icon):
        """Цифра в центре и значок под ней, с лёгкой тенью, без обводки."""
        key = ("eface", round(s, 3), txt, red, icon)
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        n = int(ENG_D * s)
        c = n / 2

        def layer(shadow):
            img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
            self.text(ImageDraw.Draw(img), c, c - 1 * s + (1 * s if shadow else 0), s, txt, 13,
                      "#000000" if shadow else (RED if red else "#FFFFFF"), 700, MONO, "mm")
            big = Image.new("RGBA", (n * 4, n * 4), (0, 0, 0, 0))
            self._eng_icon(ImageDraw.Draw(big), icon, n * 2, (c + 16 * s) * 4, s * 4, red, shadow)
            img.alpha_composite(_down(big, (n, n)))
            return img
        sh = layer(True).filter(ImageFilter.GaussianBlur(1.2 * s))
        sh.putalpha(sh.getchannel("A").point(lambda v: int(v * .6)))
        sh.alpha_composite(layer(False))
        self._tiles[key] = sh
        return sh

    @staticmethod
    def _eng_icon(d, icon, x, y, u, red, shadow=False):
        """Значки как на приборке: капля — вода, маслёнка — масло, тахометр — обороты."""
        def col(c):
            return (0, 0, 0, 255) if shadow else rgba(RED if red else c)
        if icon == "water":
            pts = [(x, y - 6 * u)]
            for i in range(25):                        # капля: острый верх, круглый низ
                a = -math.pi * 0.05 + math.pi * 1.1 * (i / 24)
                pts.append((x + 4 * u * math.cos(a), y + 1.6 * u + 4 * u * math.sin(a)))
            d.polygon(pts, fill=col("#8FC3E6"))
        elif icon == "oil":
            ox, oy = x - 7 * u, y - 5 * u
            d.polygon([(ox + 1 * u, oy + 4 * u), (ox + 7.5 * u, oy + 4 * u), (ox + 9.9 * u, oy + 2.4 * u),
                       (ox + 12.5 * u, oy + 2.4 * u), (ox + 9.2 * u, oy + 5.7 * u), (ox + 9.2 * u, oy + 10 * u),
                       (ox + 1 * u, oy + 10 * u)], fill=col("#E2C04A"))
            d.ellipse((ox + 11.9 * u, oy + 7.5 * u, ox + 14.1 * u, oy + 9.7 * u), fill=col("#E2C04A"))
        else:
            r = 5 * u
            w = (0, 0, 0, 255) if shadow else (255, 255, 255, 255)
            d.arc((x - r, y + 2.5 * u - r, x + r, y + 2.5 * u + r), 180, 360, fill=w, width=max(1, int(1.4 * u)))
            d.line((x, y + 2.5 * u, x + 3 * u, y - 1 * u), fill=(0, 0, 0, 255) if shadow else rgba(ACC),
                   width=max(1, int(1.4 * u)))

    @staticmethod
    def _lim_col(v, lim):
        """По порогам машины: зелёный, с жёлтого порога — жёлтый, с красного — красный."""
        y, r = lim                                     # красного может не быть (None) — только жёлтый
        return ENG_R if r is not None and v >= r else ENG_Y if v >= y else ENG_G

    @staticmethod
    def _temp_col(v, mark, span, lamp):
        """Цвет температуры — от запомненной лампы этой машины (риски)."""
        if lamp:
            return ENG_R
        if not mark:
            return ENG_N
        dd = mark - v
        if dd >= span:
            return ENG_G
        if dd >= 3:
            return _mix(ENG_G, ENG_Y, (span - dd) / (span - 3))
        return _mix(ENG_Y, ENG_O, (3 - dd) / 3)

    @staticmethod
    def _rpm_col(r, sl, now):
        """Цвет оборотов — по лампочкам переключения сима, иначе от отсечки.
        (цвет, мигает ли)."""
        red = sl.get("red") or 0
        if sl.get("first") and sl.get("shift"):
            last = sl.get("last") or sl["shift"]
            bl = sl.get("blink") or last
            if r >= bl:
                return (ENG_R if int(now * 8) % 2 else _mix(ENG_R, (40, 20, 20), .55)), True
            if r >= last:
                return ENG_O, False
            if r >= sl["shift"]:
                return _mix(ENG_Y, ENG_O, (r - sl["shift"]) / max(1, last - sl["shift"])), False
            if r >= sl["first"]:
                return _mix(ENG_G, ENG_Y, (r - sl["first"]) / max(1, sl["shift"] - sl["first"])), False
            return ENG_N, False
        if red:
            return (ENG_R if r >= red * .995 else ENG_O if r >= red * .97 else ENG_N), False
        return ENG_N, False

    def _engine_rings(self, data, s, now):
        """Вид «полукруги»: вода, масло, обороты — без панели. Красный —
        только лампа сима; обороты — по лампочкам переключения."""
        cfg = data.get("cfg") or {}
        F = cfg.get("units") == "F"
        u = (lambda v: v * 9 / 5 + 32) if F else (lambda v: v)
        ok = data.get("ok")
        has_rpm = bool(cfg.get("rpm", 1)) and data.get("rpm") is not None
        press = bool(cfg.get("press", 1)) and data.get("press") is not None and ok
        n = int(ENG_D * s)
        gap, m = int(4 * s), int(10 * s)               # m — поле под свечение
        cnt = 3 if has_rpm and ok else 2
        W = 2 * m + cnt * n + (cnt - 1) * gap
        ph = int(11 * s) if press else 0
        H = 2 * m + n + ph
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        moving = False
        # вспышка: раз на включение лампы — по номеру вспышки из данных
        fl = self._flash
        nf = data.get("flash") or 0
        if nf and nf != fl["seen"]:
            if fl["seen"] is not None:
                fl["start"], fl["tone"] = now, "red"
            fl["seen"] = nf
        ft = now - fl["start"]
        if fl["tone"] and ft < ENG_FLASH_S:
            moving = True
            glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(glow).rounded_rectangle((m - 2 * s, m - 2 * s, W - m + 2 * s, m + n + 2 * s), 12 * s,
                                                   fill=(255, 77, 77, int(110 * (1 - ft / ENG_FLASH_S))))
            img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(5 * s)))
        if ok:
            w, o = data["water"], data["oil"]
            lw, lo_ = data.get("lamp_water"), data.get("lamp_oil")
            ww, ow = bool(data.get("warn_water")), bool(data.get("warn_oil"))
            lim = data.get("limits") or {}
            # пороги машины известны — цвет как у цифры в плашке iRacing, риска — на красном
            cw = self._lim_col(w, lim["water"]) if "water" in lim else self._temp_col(w, lw, 10, ww)
            co = self._lim_col(o, lim["oil"]) if "oil" in lim else self._temp_col(o, lo_, 8, ow)
            if "water" in lim:
                lw = lim["water"][1] if lim["water"][1] is not None else lim["water"][0]
            if "oil" in lim:
                lo_ = lim["oil"][1] if lim["oil"][1] is not None else lim["oil"][0]
            items = [("water", w, 60, 140, lw, cw, ww, f"{u(w):.0f}", False),
                     ("oil", o, 60, 150, lo_, co, ow, f"{u(o):.0f}", False)]
            if has_rpm:
                r = float(data["rpm"])
                sl = data.get("sl") or {}
                col, blink = self._rpm_col(r, sl, now)
                moving = moving or blink
                items.append(("rpm", r, 0, sl.get("red") or max(r, 1.0), None, col, False, f"{r / 1000:.1f}", blink))
        else:
            items = [("water", 0, 0, 1, None, ENG_N, False, "—", False), ("oil", 0, 0, 1, None, ENG_N, False, "—", False)]
        x = m
        for icon, v, lo, hi, mark, col, hot, txt, snap in items:
            c, mv = self._eng_col(icon, col, now, snap)
            moving = moving or mv
            fq = int(round(max(0.0, min(1.0, (v - lo) / (hi - lo))) * 200)) if hi > lo else 0
            mq = int(round(max(0.0, min(1.0, (mark - lo) / (hi - lo))) * 200)) if mark else None
            img.alpha_composite(self._eng_arc(s, fq, c, mq, hot), (x, m))
            img.alpha_composite(self._eng_face(s, txt, hot, icon), (x, m))
            x += n + gap
        if press:
            pv, pw = f"{data['press']:.1f} bar", bool(data.get("warn_press"))

            key = ("epress", round(s, 3), pv, pw, W)
            tile = self._tiles.get(key)
            if tile is None:
                # тень размытая: чёткая со сдвигом на светлом небе читалась как двойной текст
                tile = Image.new("RGBA", (W, ph), (0, 0, 0, 0))
                self.text(ImageDraw.Draw(tile), W / 2, 5 * s + 1 * s, s, pv, 9, "#000000", 700, MONO, "mm")
                tile = tile.filter(ImageFilter.GaussianBlur(1.2 * s))
                tile.putalpha(tile.getchannel("A").point(lambda v: int(v * .6)))
                top = Image.new("RGBA", (W, ph), (0, 0, 0, 0))
                self.text(ImageDraw.Draw(top), W / 2, 5 * s, s, pv, 9, RED if pw else "#FFFFFF", 700, MONO, "mm")
                tile.alpha_composite(top)
                self._tiles[key] = tile
            img.alpha_composite(tile, (0, m + n - int(1 * s)))
        return img, moving

    def engine(self, data, s, now):
        """Окно двигателя: каждый кадр — копия готовой основы, цифры и метки."""
        cfg = data.get("cfg") or {}
        if (cfg.get("style") or "rings") == "rings":
            return self._engine_rings(data, s, now)
        F = cfg.get("units") == "F"
        u = (lambda v: v * 9 / 5 + 32) if F else (lambda v: v)
        unit = "°F" if F else "°C"
        bars = bool(cfg.get("bars", 1))
        press = bool(cfg.get("press", 1)) and data.get("press") is not None
        has_rpm = bool(cfg.get("rpm", 1)) and data.get("rpm") is not None
        msg = data.get("msg") or ""
        W = 250 if bars else 224
        gh = 58 if bars else 34
        H = 34 + 6 + gh * 2 + (28 if has_rpm else 0) + (28 if press else 0) + 6
        # мигание: раз на включение лампы — по номеру вспышки из данных
        fl = self._flash
        n = data.get("flash") or 0
        moving = False
        tint = None
        if n and n != fl["seen"]:
            if fl["seen"] is not None:
                fl["start"], fl["tone"] = now, data.get("tone") or "yellow"
            fl["seen"] = n
        t = now - fl["start"]
        if fl["tone"] and t < FLASH_S * FLASH_N * 2:
            moving = True
            if int(t / FLASH_S) % 2 == 0:
                tint = "#7A6200" if fl["tone"] == "yellow" else "#7A1414"
        img = self._engine_base(data, s, W, H, gh, bars, press, has_rpm, msg, unit, u, tint).copy()
        if not data.get("ok"):
            return img, moving
        P = int(PAD * s)
        y = P + 40 * s
        ux = P + (W - 12) * s
        uw = self.font(MONO, 12 * s, 500).getlength(unit)

        def gauge(y, v, lo, hi, on):
            self._label(img, ux - uw - 2 * s, y + 12 * s, s, f"{u(v):.0f}", 22, RED if on else FG, 700)
            if bars:
                x0, x1 = P + 12 * s, ux
                self._marker(img, x0 + max(0.0, min(1.0, (v - lo) / (hi - lo))) * (x1 - x0), y + 30 * s, s)

        gauge(y, data["water"], 60, 140, data.get("warn_water"))
        y += gh * s
        gauge(y, data["oil"], 60, 150, data.get("warn_oil"))
        y += gh * s
        if has_rpm:
            self._label(img, ux, y + 14 * s, s, f"{int(data['rpm']):,}".replace(",", " "), 12, FG, 600)
            y += 28 * s
        if press:
            self._label(img, ux, y + 14 * s, s, f"{data['press']:.1f} bar", 12, RED if data.get("warn_press") else FG, 600)
        return img, moving

    # ---------- Сетапы ----------

    @staticmethod
    def _t(v) -> str:
        if v is None:
            return "—"
        v = float(v)
        return f"{int(v // 60)}:{v % 60:06.3f}" if v >= 60 else f"{v:.3f}"

    def _ell(self, d, txt, f, maxw):
        if d.textlength(txt, font=f) <= maxw:
            return txt
        while txt and d.textlength(txt + "…", font=f) > maxw:
            txt = txt[:-1]
        return txt + "…"

    def setups(self, data, s):
        """Таблица сравнения: столбец — сетап, строки — сектора, круг, отставание, круги."""
        import json
        key = (round(s, 3), json.dumps(data, sort_keys=True, ensure_ascii=False, default=str))
        hit = getattr(self, "_sp", None)
        if hit and hit[0] == key:
            return hit[1].copy()
        cfg = data.get("cfg") or {}
        cols = data.get("cols") or []
        n = int(data.get("n") or 3)
        avg = cfg.get("mode") == "avg"
        hist = bool(cfg.get("hist"))                 # все заезды: секторов и теор. круга нет
        show_theo = bool(cfg.get("theo", 1)) and not avg and not hist
        show_delta = bool(cfg.get("delta", 1))
        show_laps = bool(cfg.get("laps", 1))
        tyres = bool(cfg.get("tyres", 1)) and any(c.get("tyres") for c in cols)
        nsec = 0 if hist else n
        k = max(2, len(cols))
        colw = SET_COL_TY if tyres else SET_COL
        w = SET_LAB + colw * k
        small_rows = show_theo + show_delta + show_laps + 3      # + средний, трасса, топливо
        h = (HEAD_H + 2 + SET_TH + ROW_H * (nsec + 1) + SET_SMALL * small_rows
             + ((6 + 2 * TY_WHEEL + 14) if tyres else 0) + 6)
        if not cols:
            h = HEAD_H + 2 + ROW_H * 2 + 6
        W, H = int(w * s), int(h * s)
        P = int(PAD * s)
        img = self.panel(W, H, s)
        bg, tx, db, dt = _layers(img)
        sess = data.get("session") or ""
        right = [("сетапов", str(data.get("count") or 0)),
                 ("среднее" if avg else "лучшее", self._t(data.get("best")))]
        # мелкая строка: сессия · трасса · машина — машина уходит, если не влезает до значений справа
        f9 = self.font(TEXT, 9 * s, 800)
        room = W - 14 * s - 160 * s
        small = ""
        for cand in (" · ".join(x for x in (data.get("track"), data.get("car")) if x), data.get("track") or ""):
            txt = f"{sess}  ·  {cand}".upper()
            if f9.getlength(txt) * 1.2 + 1.8 * s * len(txt) <= room:
                small = cand
                break
        self.header(db, dt, P, s, W, small, "Сетапы", chip=(sess, data.get("session_color")), right=right)
        y = P + (HEAD_H + 2) * s
        if not cols:
            self.text(dt, P + 14 * s, y + ROW_H * s, s, "Сетап появится в столбце, когда выедете на трассу", 11.5, DIM, 600)
            out = _flat(img, bg, tx)
            self._sp = (key, out)
            return out.copy()
        x0 = P + SET_LAB * s
        cw = colw * s
        top, bottom = y, P + H - 4 * s
        # текущий сетап: подкрашенный столбец и светящаяся черта под заголовком
        for j, c in enumerate(cols):
            if c.get("cur"):
                cx = x0 + j * cw
                db.rectangle((cx, top + SET_TH * s, cx + cw, bottom), fill=rgba(ACC, .10))
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for j, c in enumerate(cols):
            if c.get("cur"):
                cx = x0 + j * cw
                gd.rectangle((cx + 10 * s, top + SET_TH * s - 2 * s, cx + cw - 10 * s, top + SET_TH * s), fill=rgba(ACC))
        if any(c.get("cur") for c in cols):
            img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4 * s)))
            img.alpha_composite(glow)
        # заголовки столбцов: имя файла и «файл / изм. N · сейчас»
        fn = self.font(TEXT, 11.5 * s, 700)
        for j, c in enumerate(cols):
            rx = x0 + (j + 1) * cw - 12 * s
            lx = x0 + j * cw + 12 * s
            nm = str(c.get("name") or "")
            maxw = rx - lx
            if c.get("pin"):
                maxw -= 12 * s
            nm = self._ell(dt, nm, fn, maxw)
            ny = top + 15 * s
            dt.text((rx, ny), nm, font=fn, fill=rgba("#FFFFFF" if c.get("cur") else FG), anchor="rm")
            if c.get("pin"):
                px = rx - dt.textlength(nm, font=fn) - 8 * s
                db.ellipse((px - 3.5 * s, ny - 5 * s, px + 3.5 * s, ny + 2 * s), fill=rgba(ACC))
                db.line((px, ny + 1 * s, px, ny + 6 * s), fill=rgba(ACC), width=1.4 * s)
            if hist:
                nses = int(c.get("sessions") or 0)
                ed = f"{nses} " + ("заезд" if nses % 10 == 1 and nses % 100 != 11 else
                                   "заезда" if 2 <= nses % 10 <= 4 and not 12 <= nses % 100 <= 14 else "заездов")
                if c.get("edit"):
                    ed = "изм. · " + ed
            else:
                ed = f"изм. {c['edit']}" if c.get("edit") else "файл"
            if c.get("cur"):
                ew = self.text(dt, rx, top + 29 * s, s, " · сейчас", 9, ACC, 700, stack=MONO, anchor="rm")
                self.text(dt, rx - ew, top + 29 * s, s, ed, 9, DIM, 700, stack=MONO, anchor="rm")
            else:
                self.text(dt, rx, top + 29 * s, s, ed, 9, DIM, 700, stack=MONO, anchor="rm")
        y = top + SET_TH * s
        tone = {"b": GREEN, "p": PURPLE}

        def label(y, txt, hgt):
            self.text(dt, P + 14 * s, y + hgt * s / 2, s, txt, 9.5, DIM, 800, spacing=.16)

        def cell(j, y, hgt, txt, col, size, weight):
            self.text(dt, x0 + (j + 1) * cw - 12 * s, y + hgt * s / 2, s, txt, size, col, weight, stack=MONO, anchor="rm")
        for k_ in range(nsec):
            label(y, f"S{k_ + 1}", ROW_H)
            for j, c in enumerate(cols):
                v = (c.get("s") or [None] * n)[k_] if k_ < len(c.get("s") or []) else None
                cl = (c.get("cls") or [""] * n)[k_] if k_ < len(c.get("cls") or []) else ""
                cell(j, y, ROW_H, self._t(v), tone.get(cl, MID), 12.5, 700 if cl else 500)
            y += ROW_H * s
        db.line((P + 4 * s, y, P + W, y), fill=rgba(LINE), width=max(1, s))
        label(y, "КРУГ", ROW_H)
        for j, c in enumerate(cols):
            cl = c.get("lap_cls") or ""
            cell(j, y, ROW_H, self._t(c.get("lap")), tone.get(cl, FG), 13.5, 700)
        y += ROW_H * s
        if show_theo:
            label(y, "ТЕОР.", SET_SMALL)
            for j, c in enumerate(cols):
                cell(j, y, SET_SMALL, self._t(c.get("theo")), DIM, 11, 500)
            y += SET_SMALL * s
        label(y, "СРЕДН.", SET_SMALL)
        for j, c in enumerate(cols):
            cell(j, y, SET_SMALL, self._t(c.get("avg")), MID, 11, 500)
        y += SET_SMALL * s
        if show_delta:
            for j, c in enumerate(cols):
                d = c.get("delta")
                cell(j, y, SET_SMALL, "—" if d is None or d <= 0 else f"+{d:.3f}", DIM, 11, 500)
            y += SET_SMALL * s
        label(y, "ТРАССА", SET_SMALL)
        for j, c in enumerate(cols):
            cell(j, y, SET_SMALL, "—" if c.get("trk") is None else f"{c['trk']:.0f} °C", DIM, 11, 500)
        y += SET_SMALL * s
        label(y, "ТОПЛ/КР", SET_SMALL)
        for j, c in enumerate(cols):
            cell(j, y, SET_SMALL, "—" if not c.get("fuel") else f"{c['fuel']:.2f} л", DIM, 11, 500)
        y += SET_SMALL * s
        if show_laps:
            label(y, "КРУГОВ", SET_SMALL)
            for j, c in enumerate(cols):
                cell(j, y, SET_SMALL, str(c.get("laps") or 0), DIM, 11, 500)
            y += SET_SMALL * s
        if tyres:
            db.line((P + 4 * s, y, P + W, y), fill=rgba(LINE), width=max(1, s))
            label(y, "РЕЗИНА", SET_SMALL)
            y += 6 * s
            for j, c in enumerate(cols):
                self._tyre_block(db, dt, x0 + j * cw + 8 * s, y + 4 * s, cw - 16 * s, s, c.get("tyres"))
        out = _flat(img, bg, tx)
        self._sp = (key, out)
        return out.copy()

    def _tyre_block(self, db, dt, x, y, w, s, ty) -> None:
        """Резина сетапа: четыре колеса (LF RF / LR RR), у каждого три части, как отдаёт
        iRacing — левая, центр, правая: износ % за круг и температура; над колесом — давление."""
        if not ty:
            self.text(dt, x + w / 2, y + TY_WHEEL * s, s, "—", 12, DIM, 600, stack=MONO, anchor="mm")
            return
        gap = 6 * s
        ww = (w - gap) / 2
        for k, c in enumerate(("LF", "RF", "LR", "RR")):
            t = ty.get(c) or {}
            bx = x + (k % 2) * (ww + gap)
            by = y + (k // 2) * (TY_WHEEL * s + 4 * s)
            db.rounded_rectangle((bx, by, bx + ww, by + TY_WHEEL * s), 3 * s, fill=(255, 255, 255, 9))
            self.text(dt, bx + 6 * s, by + 9 * s, s, c, 9.5, DIM, 800, spacing=.12)
            pr = t.get("pres")
            self.text(dt, bx + ww - 6 * s, by + 9 * s, s, "—" if pr is None else f"{pr:.0f} kPa", 11, FG, 600,
                      stack=MONO, anchor="rm")
            wear = (t.get("wear") or [None] * 3)[:3]
            temp = (t.get("temp") or [None] * 3)[:3]
            tv = [x_ for x_ in temp if x_ is not None]
            hot = max(tv) if tv and max(tv) - min(tv) > 8 else None
            cold = min(tv) if hot is not None else None
            zw = (ww - 12 * s - 8 * s) / 3
            for i in range(3):
                zx = bx + 6 * s + i * (zw + 4 * s)
                zy = by + 18 * s
                db.rounded_rectangle((zx, zy, zx + zw, zy + 36 * s), 2 * s, fill=(0, 0, 0, 64))
                wv = wear[i] if i < len(wear) else None
                col = DIM if wv is None else GREEN if wv < .20 else YELLOW if wv < .35 else RED
                self.text(dt, zx + zw / 2, zy + 11 * s, s, "—" if wv is None else f"{wv:.2f}", 13, col, 700,
                          stack=MONO, anchor="mm")
                tvv = temp[i] if i < len(temp) else None
                tc = ("#FF8A6A" if hot is not None and tvv == hot else "#7FB3FF" if cold is not None and tvv == cold
                      else MID)
                self.text(dt, zx + zw / 2, zy + 26 * s, s, "—" if tvv is None else f"{tvv:.0f}°", 11.5, tc, 600,
                          stack=MONO, anchor="mm")
                self.text(dt, zx + zw / 2, by + TY_WHEEL * s - 7 * s, s, "ЛЦП"[i], 8.5, "#7A808A", 800, anchor="mm")

    # ---------- кадр ----------

    def frame(self, oid: str, data: dict, now: float):
        """(картинка RGBA, идёт ли анимация)."""
        cfg = data.get("cfg") or {}
        s = self.dpi * (float(cfg.get("scale", 100)) / 100 if oid != "radar" else 1.0)
        try:
            self.bg = max(0.0, min(1.0, float(cfg.get("bg", 75 if oid == "radar" else 92)) / 100))
        except (TypeError, ValueError):
            self.bg = 0.92
        if oid in ("standings", "relative"):
            return self._table_frame(oid, data, s, now)
        if oid == "radar":
            return self.radar(data, s, now)
        if oid == "wind":
            return self.wind(data, s, now)
        if oid == "engine":
            return self.engine(data, s, now)
        if oid == "setups":
            return self.setups(data, s), False
        return Image.new("RGBA", (2, 2), (0, 0, 0, 0)), False


class _Big:
    """ImageDraw вдвое крупнее: те же координаты, что у панели, а рисует на
    слое x2 — после уменьшения скругления и наклоны гладкие (PIL сам не сглаживает)."""

    def __init__(self, img):
        self.d = ImageDraw.Draw(img)

    @staticmethod
    def _xy(xy):
        return [v * SS for v in xy] if not isinstance(xy[0], (tuple, list)) else [(a * SS, b * SS) for a, b in xy]

    def rounded_rectangle(self, xy, radius=0, **kw):
        if "width" in kw:
            kw["width"] = kw["width"] * SS
        self.d.rounded_rectangle(self._xy(xy), radius * SS, **kw)

    def rectangle(self, xy, **kw):
        if "width" in kw:
            kw["width"] = kw["width"] * SS
        self.d.rectangle(self._xy(xy), **kw)

    def ellipse(self, xy, **kw):
        self.d.ellipse(self._xy(xy), **kw)

    def polygon(self, xy, **kw):
        self.d.polygon(self._xy(xy), **kw)

    def line(self, xy, fill=None, width=1, **kw):
        self.d.line(self._xy(xy), fill=fill, width=max(1, int(width * SS)), **kw)


def _mix(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def _layers(img):
    """Прозрачные слои фигур (x2) и текста поверх панели — см. _flat."""
    bg = Image.new("RGBA", (img.width * SS, img.height * SS), (0, 0, 0, 0))
    tx = Image.new("RGBA", img.size, (0, 0, 0, 0))
    return bg, tx, _Big(bg), ImageDraw.Draw(tx)


def _flat(img, bg, tx):
    """ImageDraw на RGBA не смешивает, а затирает пиксели: фигуры и текст
    рисуются на своих слоях и накладываются по альфе."""
    img.alpha_composite(_down(bg, img.size))
    img.alpha_composite(tx)
    return img


def _down(img, size):
    """Уменьшение картинки, нарисованной крупнее: усреднение по квадрату в
    пространстве с умноженной альфой. LANCZOS по прозрачным краям давал
    тёмную бахрому и «звон» — кромка радара выглядела рваной."""
    w, h = size
    pm = img.convert("RGBa")
    if img.width % w == 0 and img.height % h == 0 and img.width // w == img.height // h:
        pm = pm.reduce(img.width // w)
    else:
        pm = pm.resize((w, h), Image.BOX)
    return pm.convert("RGBA")


def _key(data: dict) -> str:
    """Ключ неподвижной части таблицы: всё, кроме данных рации."""
    import json
    d = {k: v for k, v in data.items() if k not in ("voice", "flag_event", "flag_tone")}
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


def _isnum(v) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def _dark(c) -> bool:
    p = parse_color(c or "")
    if not p:
        return False
    return (p[0] * 299 + p[1] * 587 + p[2] * 114) / 1000 < 110
