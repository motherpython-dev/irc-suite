"""Шторка повтора и метка «ПОВТОР» — для окон поверх сима (PIL).

Те же четыре варианта и тайминги, что в мокапе docs/mockups/replay-fx.html и
на странице для OBS (web/ov_replay.html):
  A — шеврон: оранжевая полоса со скосом и графит проходят слева направо;
  B — ламели: пластины с чередующихся сторон, светится только торец на ходу;
  C — диафрагма: экран закрывается кругом, по краю — светящееся кольцо;
  D — скос: две панели сходятся по диагонали, светятся только края на ходу.
Под шторкой — логотип и слово («ПОВТОР» или «● LIVE»). У B и D логотип
появляется только после того, как погасло свечение краёв, — линия и логотип
вместе на экране не бывают.

Движение — только сдвиг готовых плиток: панели и свечение рисуются один раз на
размер экрана, кадр — склейка со смещениями (полноэкранное размытие на каждый
кадр стоило бы десятки миллисекунд).
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .ov_paint import Fonts, bezier

EASE = bezier(.7, 0, .3, 1)
DARK = (20, 21, 25, 255)                 # #141519 — заливка шторки
ACC = (255, 90, 31)
LIVE_RED = (255, 45, 45)
# длительность закрытия и открытия по вариантам, с (как в мокапе); держим — у режиссёра
TIMES = {"A": (0.5, 0.58), "B": (0.66, 1.0), "C": (0.55, 0.6), "D": (0.5, 0.85)}
VARIANTS = tuple(TIMES)


def _ease_lin(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


def _keys(t: float, pts) -> float:
    """Ключевые кадры непрозрачности: [(доля, значение), ...], линейно между ними."""
    if t <= pts[0][0]:
        return pts[0][1]
    for (a, va), (b, vb) in zip(pts, pts[1:]):
        if t <= b:
            return va + (vb - va) * ((t - a) / (b - a) if b > a else 1)
    return pts[-1][1]


EG_IN = [(0, 0), (.15, 1), (.8, 1), (1, 0)]
EG_OUT = [(0, 0), (.2, 1), (.85, 1), (1, 0)]


def _paste(canvas: Image.Image, src: Image.Image, x: float, y: float, alpha: float = 1.0) -> None:
    """alpha_composite с отрицательными координатами и общей прозрачностью."""
    if alpha <= 0.003:
        return
    x, y = int(round(x)), int(round(y))
    sx, sy = max(0, -x), max(0, -y)
    dx, dy = max(0, x), max(0, y)
    w = min(src.width - sx, canvas.width - dx)
    h = min(src.height - sy, canvas.height - dy)
    if w <= 0 or h <= 0:
        return
    part = src.crop((sx, sy, sx + w, sy + h))
    if alpha < 0.997:
        a = part.getchannel("A").point(lambda v: int(v * alpha))
        part.putalpha(a)
    canvas.alpha_composite(part, (dx, dy))


def _glow_line(size, p0, p1, width, glow) -> Image.Image:
    """Светящаяся линия: ореол (размытие) и яркая сердцевина."""
    w, h = size
    k = 4                                            # ореол считаем вчетверо мельче — быстро
    halo = Image.new("L", (max(1, w // k), max(1, h // k)), 0)
    ImageDraw.Draw(halo).line([(p0[0] / k, p0[1] / k), (p1[0] / k, p1[1] / k)], fill=255, width=max(1, int(glow / k)))
    halo = halo.filter(ImageFilter.GaussianBlur(glow / k / 1.6)).resize((w, h), Image.BILINEAR)
    core = Image.new("L", (w, h), 0)
    ImageDraw.Draw(core).line([p0, p1], fill=255, width=max(1, int(width)))
    out = Image.new("RGBA", (w, h), ACC + (0,))
    a = Image.eval(halo, lambda v: int(v * .85))
    out.putalpha(Image.composite(Image.new("L", (w, h), 255), a, core))
    hot = Image.new("RGBA", (w, h), (255, 230, 215, 0))
    hot.putalpha(core.filter(ImageFilter.GaussianBlur(max(0.5, width / 3))).point(lambda v: int(v * .7)))
    out.alpha_composite(hot)
    return out


class FxPainter:
    def __init__(self, fetch) -> None:
        self.fetch = fetch
        self.fonts = Fonts(fetch)
        self._cache: dict = {}
        self._logo = None

    # ---------- общее ----------

    def _logo_img(self, w: int) -> Image.Image | None:
        key = ("logo", w)
        if key in self._cache:
            return self._cache[key]
        if self._logo is None:
            raw = self.fetch("/assets/logo.png")
            try:
                self._logo = Image.open(io.BytesIO(raw)).convert("RGBA") if raw else False
            except Exception:                   # noqa: BLE001
                self._logo = False
        if not self._logo:
            return None
        lw = max(8, int(w * 0.16))
        img = self._logo.resize((lw, max(1, int(lw * self._logo.height / self._logo.width))), Image.LANCZOS)
        self._cache[key] = img
        return img

    def _mid(self, W: int, H: int, word: str) -> Image.Image:
        """Логотип и слово под ним — одна плитка."""
        key = ("mid", W, H, word)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        logo = self._logo_img(W)
        size = W * 0.02
        f, _it, _st = self.fonts.get("'Exo 2'", size, 800, False)
        sp = size * 0.5
        live = word == "LIVE"
        text = word
        dot = size * .55 if live else 0                 # «●» — кругом: в Exo 2 такого знака нет
        d0 = ImageDraw.Draw(Image.new("L", (1, 1)))
        tw = sum(d0.textlength(ch, font=f) for ch in text) + sp * (len(text) - 1) + (dot + sp if live else 0)
        lw, lh = (logo.size if logo else (0, 0))
        gap = W * 0.012
        mw = int(max(lw, tw) + 40)
        mh = int(lh + gap + size * 1.6 + 40)
        img = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
        if logo:
            img.alpha_composite(logo, ((mw - lw) // 2, 20))
        d = ImageDraw.Draw(img)
        x, y = (mw - tw) / 2, 20 + lh + gap + size * .8
        if live:
            d.ellipse((x, y - dot / 2, x + dot, y + dot / 2), fill=LIVE_RED + (255,))
            x += dot + sp
        for ch in text:
            d.text((x, y), ch, font=f, fill=(255, 255, 255, 255), anchor="lm")
            x += d.textlength(ch, font=f) + sp
        self._cache[key] = img
        return img

    def _put_mid(self, canvas, word, alpha, dy=0.0, scale=1.0) -> None:
        W, H = canvas.size
        m = self._mid(W, H, word)
        if scale != 1.0 and alpha > 0.003:
            m = m.resize((max(1, int(m.width * scale)), max(1, int(m.height * scale))), Image.BILINEAR)
        _paste(canvas, m, (W - m.width) / 2, (H - m.height) / 2 + dy * H, alpha)

    def _mid_alpha(self, v: str, phase: str, t: float, t_in: float) -> tuple[float, float, float]:
        """(прозрачность, сдвиг по высоте, масштаб) логотипа со словом."""
        if phase == "in":
            if v in ("B", "D"):
                return 0.0, 0.0, 1.0
            k = _ease_lin((t / t_in - .45) / .55)
            return k, .04 * (1 - k), .96 + .04 * k
        if phase == "cover":
            if v in ("B", "D"):
                k = _ease_lin((t - .1) / .35)
                return k, .04 * (1 - k), .96 + .04 * k
            return 1.0, 0.0, 1.0
        if phase == "out":
            k = _ease_lin(t / .3)
            return 1 - k, -.04 * k, 1 + .02 * k
        return 0.0, 0.0, 1.0

    # ---------- кадр ----------

    def frame(self, W: int, H: int, variant: str, phase: str, t: float, word: str) -> Image.Image:
        v = variant if variant in TIMES else "D"
        t_in, t_out = TIMES[v]
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if not phase:
            return canvas
        getattr(self, "_" + v)(canvas, phase, t, t_in, t_out)
        a, dy, sc = self._mid_alpha(v, phase, t, t_in)
        self._put_mid(canvas, word, a, dy, sc)
        return canvas

    # Фигуры рисуются прямо на кадре (ImageDraw с режимом RGBA смешивает цвет с
    # тем, что под ним): на 3440×1440 склейка полноэкранных плиток стоила 40–50 мс
    # на кадр, заливка многоугольников — единицы миллисекунд.

    @staticmethod
    def _glow(cv, pts, alpha):
        """Светящаяся линия по точкам. Ореол — маска в четверть разрешения только в
        полосе вокруг линии и честное наложение (alpha_composite): ImageDraw на
        прозрачном месте смешивал бы оранжевый с чёрным — ореол выходил грязным."""
        if alpha <= 0.01:
            return
        W, H = cv.size
        # длинная или изогнутая линия — кусками: у куска узкая полоса, а у всей
        # диагонали или кольца полоса была бы на пол-экрана (≈30 мс на кадр)
        step = W / 10
        out, cur, run = [], [pts[0]], 0.0
        for a_, b_ in zip(pts, pts[1:]):
            seg = math.hypot(b_[0] - a_[0], b_[1] - a_[1])
            if seg > step:                               # длинный отрезок — делим
                k = int(seg // step) + 1
                for j in range(1, k + 1):
                    cur.append((a_[0] + (b_[0] - a_[0]) * j / k, a_[1] + (b_[1] - a_[1]) * j / k))
                    out.append(cur)
                    cur = [cur[-1]]
                run = 0.0
                continue
            cur.append(b_)
            run += seg
            if run >= step:
                out.append(cur)
                cur, run = [b_], 0.0
        if len(cur) > 1:
            out.append(cur)
        if len(out) > 1:
            for part in out:
                FxPainter._glow_part(cv, part, alpha)
            ImageDraw.Draw(cv, "RGBA").line(pts, fill=(255, 232, 220, int(170 * alpha)), width=max(1, int(W * .0006)))
            return
        FxPainter._glow_part(cv, pts, alpha)
        ImageDraw.Draw(cv, "RGBA").line(pts, fill=(255, 232, 220, int(170 * alpha)), width=max(1, int(W * .0006)))

    @staticmethod
    def _glow_part(cv, pts, alpha):
        W, H = cv.size
        g = max(4, int(W * .02))                         # ширина ореола
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, y0 = max(0, int(min(xs)) - g), max(0, int(min(ys)) - g)
        x1, y1 = min(W, int(max(xs)) + g + 1), min(H, int(max(ys)) + g + 1)
        if x1 <= x0 or y1 <= y0:
            return
        k = 4
        mw, mh = max(1, (x1 - x0) // k), max(1, (y1 - y0) // k)
        m = Image.new("L", (mw, mh), 0)
        md = ImageDraw.Draw(m)
        loc = [((px - x0) / k, (py - y0) / k) for px, py in pts]
        md.line(loc, fill=int(255 * .9 * alpha), width=max(1, int(g / k / 2)))
        m = m.filter(ImageFilter.GaussianBlur(g / k / 3))
        m = m.resize((x1 - x0, y1 - y0), Image.BILINEAR)
        tile = Image.new("RGBA", m.size, ACC + (0,))
        core = Image.new("L", m.size, 0)
        ImageDraw.Draw(core).line([(px - x0, py - y0) for px, py in pts], fill=int(255 * alpha), width=max(2, int(W * .0016)))
        tile.putalpha(ImageChops.lighter(m, core))
        cv.alpha_composite(tile, (x0, y0))

    # A — шеврон
    def _A(self, cv, phase, t, t_in, t_out):
        W, H = cv.size
        k = math.tan(math.radians(18))
        bw = 1.6 * W
        top, bot = -0.1 * H, 1.1 * H

        def x_of(delay, start, end, dur):
            return (start + (end - start) * EASE((t - delay) / dur)) * bw
        if phase == "in":
            xo, xd = x_of(0, -1.1, -.25, .5), x_of(.08, -1.1, -.25, .5)
        elif phase == "cover":
            xo = xd = -.25 * bw
        else:
            xd, xo = x_of(0, -.25, .75, .5), x_of(.08, -.25, .75, .5)
        d = ImageDraw.Draw(cv, "RGBA")
        sk = k * (bot - top)
        for x, col in ((xo, (255, 106, 46, 255)), (xd, DARK)):
            # skewX(-18°): верх сдвинут вправо относительно низа
            d.polygon([(x + sk / 2, top), (x + bw + sk / 2, top), (x + bw - sk / 2, bot), (x - sk / 2, bot)], fill=col)

    # B — ламели
    def _B(self, cv, phase, t, t_in, t_out):
        W, H = cv.size
        n = 8
        sh = H / n
        d = ImageDraw.Draw(cv, "RGBA")
        gw = max(2, W * .003)
        for i in range(n):
            right = i % 2 == 0                       # чётные — слева, передний край справа
            y0, y1 = i * sh, (i + 1) * sh + 1
            if phase == "in":
                e = EASE((t - i * .03) / .42)
                x = (-1.05 if right else 1.05) * W * (1 - e)
                ea = _keys((t - i * .03) / .5, EG_IN)
            elif phase == "cover":
                x, ea = 0.0, 0.0
            else:
                dl = .3 + i * .03
                e = EASE((t - dl) / .42)
                x = (1.05 if right else -1.05) * W * e
                ea = _keys((t - dl) / .45, EG_OUT)
            if x >= W or x + W <= 0:
                continue
            d.rectangle((x, y0, x + W, y1), fill=DARK)
            if ea > 0.01:
                ex = x + W - gw / 2 if right else x + gw / 2
                self._glow(cv, [(ex, y0), (ex, y1)], ea)

    # C — диафрагма
    def _C(self, cv, phase, t, t_in, t_out):
        W, H = cv.size
        R = math.sqrt((W * W + H * H) / 2) * 1.2
        cx, cy = W / 2, H / 2
        if phase == "in":
            e = EASE(t / .55)
            r = R * e
            rs, ra = .02 + .98 * e, _keys(t / .55, [(0, 1), (.8, 1), (1, 0)])
        elif phase == "cover":
            r, ra, rs = R, 0.0, 1.0
        else:
            e = EASE(t / .55)
            r = R * (1 - e)
            rs, ra = 1 - .98 * e, _keys(t / .55, [(0, 0), (.2, 1), (1, 1)])
        d = ImageDraw.Draw(cv, "RGBA")
        if r > 0.5:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=DARK)
        if ra > 0.01:
            rr = 0.75 * W * rs                        # кольцо: 150cqw в диаметре
            n = max(24, int(rr / 6))
            pts = [(cx + rr * math.cos(2 * math.pi * i / n), cy + rr * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
            self._glow(cv, pts, ra)

    # D — скос
    def _D(self, cv, phase, t, t_in, t_out):
        W, H = cv.size
        BW, BH = W * 1.4, H * 1.4
        ox, oy = -.2 * W, -.2 * H
        if phase == "in":
            e = EASE(t / .5)
            ea = _keys(t / .56, EG_IN)
        elif phase == "cover":
            e, ea = 1.0, 0.0
        else:
            e = 1 - EASE((t - .3) / .5)
            ea = _keys((t - .3) / .5, EG_OUT)
        tx, ty = .3 * BW * (1 - e), .6 * BH * (1 - e)
        d = ImageDraw.Draw(cv, "RGBA")

        def pt(fx, fy, sx, sy):
            return (ox + fx * BW + sx, oy + fy * BH + sy)
        # панели заходят друг на друга на 0,4 % — без щели на шве
        d.polygon([pt(0, 0, -tx, -ty), pt(1, 0, -tx, -ty), pt(1, .304, -tx, -ty), pt(0, .704, -tx, -ty)], fill=DARK)
        d.polygon([pt(0, .696, tx, ty), pt(1, .296, tx, ty), pt(1, 1, tx, ty), pt(0, 1, tx, ty)], fill=DARK)
        if ea > 0.01:
            self._glow(cv, [pt(0, .7, -tx, -ty), pt(1, .3, -tx, -ty)], ea)
            self._glow(cv, [pt(0, .7, tx, ty), pt(1, .3, tx, ty)], ea)

    # ---------- метка «ПОВТОР» ----------

    def bug(self, info: dict, s: float = 1.0) -> Image.Image:
        """◀◀ ПОВТОР · номер · ФАМИЛИЯ · КРУГ N — или ● LIVE после возврата."""
        live = info.get("mode") == "live"
        H = int(40 * s)
        f_t, _a, _b = self.fonts.get("'Exo 2'", 15 * s, 800, False)
        f_n, _a, _b = self.fonts.get("'Exo 2'", 14 * s, 800, False)
        f_p, _a, _b = self.fonts.get("'Exo 2'", 15 * s, 900, False)
        d0 = ImageDraw.Draw(Image.new("L", (1, 1)))

        def sw(txt, f, sp):
            return sum(d0.textlength(c, font=f) for c in txt) + sp * max(0, len(txt) - 1)
        pad = 14 * s
        parts = []
        if live:
            parts.append(("dot", None, 0, LIVE_RED))
            parts.append(("LIVE", f_t, 3 * s, None))
        else:
            parts.append(("rew", None, 0, ACC))
            parts.append(("ПОВТОР", f_t, 3 * s, None))
            parts.append(("plate", None, 0, None))
            nm = str(info.get("name") or "").split()
            parts.append(((nm[-1] if nm else "").upper() + (f" · КРУГ {info['lap']}" if info.get("lap") else ""),
                          f_n, 1.1 * s, (195, 201, 211)))
        gap = 12 * s
        pw = 44 * s
        ICON = {"plate": pw, "rew": 18 * s, "dot": 9 * s}
        W = int(pad + sum(ICON[p[0]] if p[0] in ICON else sw(p[0], p[1], p[2]) for p in parts) + gap * (len(parts) - 1) + pad + 4 * s)
        img = Image.new("RGBA", (W + 20, H + 20), (0, 0, 0, 0))
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle((10, 14, 10 + W, 10 + H), int(6 * s), fill=140)
        img.putalpha(sh.filter(ImageFilter.GaussianBlur(6 * s)))
        img = Image.composite(Image.new("RGBA", img.size, (0, 0, 0, 255)), Image.new("RGBA", img.size, (0, 0, 0, 0)),
                              img.getchannel("A"))
        body = Image.new("RGBA", (W, H), (22, 21, 26, 245))
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, W - 1, H - 1), int(6 * s), fill=255)
        bd = ImageDraw.Draw(body)
        bd.rectangle((0, int(8 * s), int(3 * s), H - int(8 * s)), fill=(LIVE_RED if live else ACC) + (255,))
        panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        panel.paste(body, (0, 0), m)
        img.alpha_composite(panel, (10, 10))
        d = ImageDraw.Draw(img)
        x, cy = 10 + pad + 4 * s, 10 + H / 2
        for txt, f, sp, col in parts:
            if txt == "rew":                          # ◀◀ — два треугольника
                hh = 6 * s
                for k in (0, 9 * s):
                    d.polygon([(x + k, cy), (x + k + 9 * s, cy - hh), (x + k + 9 * s, cy + hh)], fill=col + (255,))
                x += 18 * s + gap
                continue
            if txt == "dot":
                r = 4.5 * s
                d.ellipse((x, cy - r, x + 2 * r, cy + r), fill=col + (255,))
                x += 9 * s + gap * .6
                continue
            if txt == "plate":
                c = info.get("colors") or ["#5C6370"]
                pl = Image.new("RGBA", (int(pw), int(24 * s)), (0, 0, 0, 0))
                pm = Image.new("L", pl.size, 0)
                ImageDraw.Draw(pm).rounded_rectangle((0, 0, pl.width - 1, pl.height - 1), int(4 * s), fill=255)
                from .ov_paint import parse_color
                c1 = parse_color(c[0]) or (92, 99, 112, 255)
                c2 = parse_color(c[1]) if len(c) > 1 else c1
                g = Image.linear_gradient("L").rotate(90).resize(pl.size)
                pl.paste(Image.composite(Image.new("RGBA", pl.size, tuple(c2[:3]) + (255,)),
                                         Image.new("RGBA", pl.size, tuple(c1[:3]) + (255,)), g), (0, 0), pm)
                pd = ImageDraw.Draw(pl)
                pd.text((pl.width / 2 + 1, pl.height / 2 + 1), str(info.get("num") or ""), font=f_p, fill=(0, 0, 0, 200), anchor="mm")
                pd.text((pl.width / 2, pl.height / 2), str(info.get("num") or ""), font=f_p, fill=(255, 255, 255, 255), anchor="mm")
                img.alpha_composite(pl, (int(x), int(cy - pl.height / 2)))
                x += pw + gap
                continue
            for ch in txt:
                cc = col or (255, 255, 255)
                d.text((x, cy), ch, font=f, fill=tuple(cc[:3]) + (255,), anchor="lm")
                x += d.textlength(ch, font=f) + sp
            x += gap - sp
        return img


# =====================================================================
#  Шторка из отдельных окон-деталей (для окон поверх сима)
#
#  Перерисовка всего экрана на каждый кадр (3440×1440 — 25–30 мс в Python)
#  давала 30–40 неровных кадров, и панели прыгали по 150–200 px — анимация
#  рвалась. Здесь каждая деталь — панель, пластина, линия свечения, логотип —
#  своё слоистое окно с готовой картинкой, а кадр — только сдвиг окон и их
#  прозрачность (UpdateLayeredWindow без новой картинки — 0,1 мс). Диафрагма —
#  вырез окна по кругу (SetWindowRgn).
#
#  sprites() — картинки деталей; layout() — где каждая стоит в момент t:
#  {имя: (x, y, прозрачность 0..1, вырез или None)}. Вырез: ("circle", cx, cy, r)
#  или ("ring", cx, cy, r0, r1) в координатах окна.
# =====================================================================

def _solid(w: int, h: int, col) -> Image.Image:
    return Image.new("RGBA", (max(1, int(w)), max(1, int(h))), col)


class FxSprites:
    def __init__(self, painter: FxPainter) -> None:
        self.p = painter

    def sprites(self, W: int, H: int, v: str) -> list[tuple[str, Image.Image]]:
        """Детали варианта по порядку снизу вверх; логотип со словом — последними."""
        out: list[tuple[str, Image.Image]] = []
        if v == "A":
            k = math.tan(math.radians(18))
            bw, bh = 1.6 * W, 1.2 * H
            sk = k * bh
            size = (int(bw + sk) + 2, int(bh) + 2)
            for name, col in (("bo", (255, 106, 46, 255)), ("bd", DARK)):
                img = Image.new("RGBA", size, (0, 0, 0, 0))
                ImageDraw.Draw(img).polygon([(sk, 0), (bw + sk, 0), (bw, bh), (0, bh)], fill=col)
                out.append((name, img))
        elif v == "B":
            sh = H / 8
            g = max(4, int(W * .02))
            for i in range(8):
                out.append((f"s{i}", _solid(W, math.ceil(sh) + 1, DARK)))
            for i in range(8):
                img = Image.new("RGBA", (2 * g + 2, math.ceil(sh) + 1), (0, 0, 0, 0))
                FxPainter._glow(img, [(g, 0), (g, img.height)], 1.0)
                out.append((f"e{i}", img))
        elif v == "C":
            out.append(("disk", _solid(W, H, DARK)))
            out.append(("halo", _solid(W, H, ACC + (255,))))
            out.append(("ring", _solid(W, H, (255, 150, 110, 255))))
        else:                                            # D
            BW, BH = W * 1.4, H * 1.4
            ht = math.ceil(.704 * BH) + 2
            top = Image.new("RGBA", (int(BW) + 1, ht), (0, 0, 0, 0))
            ImageDraw.Draw(top).polygon([(0, 0), (BW, 0), (BW, .304 * BH), (0, .704 * BH)], fill=DARK)
            y0 = int(.296 * BH) - 2
            bot = Image.new("RGBA", (int(BW) + 1, int(BH) - y0 + 1), (0, 0, 0, 0))
            ImageDraw.Draw(bot).polygon([(0, .696 * BH - y0), (BW, .296 * BH - y0), (BW, BH - y0), (0, BH - y0)], fill=DARK)
            g = max(4, int(W * .02))
            gy0 = int(.3 * BH) - g
            eg = Image.new("RGBA", (int(BW) + 1, int(.4 * BH) + 2 * g + 1), (0, 0, 0, 0))
            FxPainter._glow(eg, [(0, .7 * BH - gy0), (BW, .3 * BH - gy0)], 1.0)
            out += [("pt", top), ("pb", bot), ("gt", eg), ("gb", eg)]
        for word in ("ПОВТОР", "LIVE"):
            out.append(("m_" + word, self.p._mid(W, H, word)))
        return out

    def layout(self, W: int, H: int, v: str, phase: str, t: float, word: str) -> dict:
        lay: dict = {}
        t_in, t_out = TIMES.get(v, TIMES["D"])
        if v == "A":
            k = math.tan(math.radians(18))
            bw, bh = 1.6 * W, 1.2 * H
            sk = k * bh

            def x_of(delay, start, end, dur):
                return (start + (end - start) * EASE((t - delay) / dur)) * bw
            if phase == "in":
                xo, xd = x_of(0, -1.1, -.25, .5), x_of(.08, -1.1, -.25, .5)
            elif phase == "cover":
                xo = xd = -.25 * bw
            else:
                xd, xo = x_of(0, -.25, .75, .5), x_of(.08, -.25, .75, .5)
            lay["bo"] = (xo - sk / 2, -.1 * H, 1.0, None)
            lay["bd"] = (xd - sk / 2, -.1 * H, 1.0, None)
        elif v == "B":
            sh = H / 8
            g = max(4, int(W * .02))
            gw = max(2, W * .003)
            for i in range(8):
                right = i % 2 == 0
                if phase == "in":
                    x = (-1.05 if right else 1.05) * W * (1 - EASE((t - i * .03) / .42))
                    ea = _keys((t - i * .03) / .5, EG_IN)
                elif phase == "cover":
                    x, ea = 0.0, 0.0
                else:
                    dl = .3 + i * .03
                    x = (1.05 if right else -1.05) * W * EASE((t - dl) / .42)
                    ea = _keys((t - dl) / .45, EG_OUT)
                lay[f"s{i}"] = (x, i * sh, 1.0, None)
                ex = x + W - gw / 2 if right else x + gw / 2
                lay[f"e{i}"] = (ex - g - 1, i * sh, ea, None)
        elif v == "C":
            R = math.sqrt((W * W + H * H) / 2) * 1.2
            cx, cy = W / 2, H / 2
            if phase == "in":
                e = EASE(t / .55)
                r, rs, ra = R * e, .02 + .98 * e, _keys(t / .55, [(0, 1), (.8, 1), (1, 0)])
            elif phase == "cover":
                r, rs, ra = R, 1.0, 0.0
            else:
                e = EASE(t / .55)
                r, rs, ra = R * (1 - e), 1 - .98 * e, _keys(t / .55, [(0, 0), (.2, 1), (1, 1)])
            rr = .75 * W * rs
            w1, w2 = max(2, W * .0016), max(4, W * .012)
            lay["disk"] = (0, 0, 1.0 if r > .5 else 0.0, ("circle", cx, cy, r))
            lay["halo"] = (0, 0, .28 * ra, ("ring", cx, cy, rr - w2, rr + w2))
            lay["ring"] = (0, 0, ra, ("ring", cx, cy, rr - w1, rr + w1))
        else:                                            # D
            BW, BH = W * 1.4, H * 1.4
            ox, oy = -.2 * W, -.2 * H
            if phase == "in":
                e, ea = EASE(t / .5), _keys(t / .56, EG_IN)
            elif phase == "cover":
                e, ea = 1.0, 0.0
            else:
                e, ea = 1 - EASE((t - .3) / .5), _keys((t - .3) / .5, EG_OUT)
            tx, ty = .3 * BW * (1 - e), .6 * BH * (1 - e)
            y0 = int(.296 * BH) - 2
            g = max(4, int(W * .02))
            gy0 = int(.3 * BH) - g
            lay["pt"] = (ox - tx, oy - ty, 1.0, None)
            lay["pb"] = (ox + tx, oy + ty + y0, 1.0, None)
            lay["gt"] = (ox - tx, oy - ty + gy0, ea, None)
            lay["gb"] = (ox + tx, oy + ty + gy0, ea, None)
        a, dy, _sc = self.p._mid_alpha(v, phase, t, t_in)
        for wd in ("ПОВТОР", "LIVE"):
            m = self.p._mid(W, H, wd)
            lay["m_" + wd] = ((W - m.width) / 2, (H - m.height) / 2 + dy * H, a if wd == word else 0.0, None)
        return lay
