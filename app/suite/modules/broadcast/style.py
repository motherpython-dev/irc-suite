"""Графический язык эфирных оверлеев: наклонные плашки и узкий шрифт.

Ориентир — трансляционная графика NASCAR: пилон позиций с крупными узкими
цифрами, плашки со срезанным углом, полоса флага сессии вдоль края, счётчик
кругов в шапке. Цвета берём свои — палитра iRChamps, оранжевый акцент.

Почему через PIL, а не средствами Tk: Tk не умеет ни наклонный текст, ни
параллелограммы, а именно они делают этот стиль узнаваемым. Здесь всё
рисуется в картинку с четырёхкратным запасом и уменьшается — иначе
диагонали получаются лесенкой.

Всё, что рисуется, кэшируется по параметрам: оверлеи обновляются несколько
раз в секунду, и пересоздавать одни и те же плашки на каждом кадре нельзя.
"""
from __future__ import annotations

import functools
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------- палитра ----------

BG = "#0B0E14"          # фон окна
SURFACE = "#141923"     # строка
SURFACE_2 = "#1A2130"   # строка через одну
SURFACE_3 = "#232C3D"   # чип номера
LINE = "#2A3344"
FG = "#E6EDF3"
DIM = "#8B98A9"
ACCENT = "#FF5A1F"      # оранжевый iRChamps
ACCENT_2 = "#FF8A4C"
LIVE = "#3FB950"
PIT = "#F0A020"

# Флаги сессии: цвет полосы вдоль пилона.
FLAG_COLORS = {
    "green": "#2EA043",
    "yellow": "#E3B341",
    "red": "#DA3633",
    "white": "#E6EDF3",
    "checkered": "#E6EDF3",
    "none": "#2A3344",
}

# Дивизионы и классы: чип номера красится по ним. Ключи приводим к верхнему
# регистру, незнакомые получают нейтральный цвет.
CLASS_COLORS = {
    "GT3": "#FF5A1F", "PRO": "#FF5A1F",
    "GT4": "#3B82F6", "AM": "#3B82F6",
    "SILVER": "#94A3B8", "GOLD": "#E3B341",
}
CLASS_FALLBACK = "#5A6B85"

SLANT = 0.22            # тангенс наклона: срез угла и наклон текста
SS = 4                  # кратность отрисовки перед уменьшением

ASSETS = Path(__file__).resolve().parents[2] / "assets" / "fonts"


def class_color(name: str) -> str:
    return CLASS_COLORS.get((name or "").strip().upper(), CLASS_FALLBACK)


@functools.lru_cache(maxsize=8)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """Шрифт для отрисовки в картинку.

    Bahnschrift — узкий DIN, он есть в Windows 10 и 11 и ближе всего к
    эфирному стилю. Если его нет, откатываемся на вшитый Rubik: шире и
    спокойнее, но лучше, чем системный по умолчанию.
    """
    candidates = {
        "num": [r"C:\Windows\Fonts\bahnschrift.ttf", str(ASSETS / "Rubik-Bold.ttf")],
        "text": [r"C:\Windows\Fonts\bahnschrift.ttf", str(ASSETS / "Rubik-Medium.ttf")],
    }[kind]
    for path in candidates:
        try:
            f = ImageFont.truetype(path, size * SS)
            if "bahnschrift" in path.lower():
                try:                       # вариативный шрифт: берём узкое жирное
                    f.set_variation_by_name(
                        "SemiBold Condensed" if kind == "num" else "SemiLight Condensed")
                except Exception:
                    pass
            return f
        except Exception:
            continue
    return ImageFont.load_default()


def _shear(img: Image.Image, slant: float = SLANT) -> Image.Image:
    """Наклонить картинку вправо, как курсив."""
    w, h = img.size
    dx = int(h * slant)
    out = Image.new("RGBA", (w + dx, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), img)
    return out.transform((w + dx, h), Image.AFFINE, (1, slant, -dx * 0, 0, 1, 0),
                         resample=Image.BICUBIC)


@functools.lru_cache(maxsize=512)
def text_png(text: str, size: int, color: str, kind: str = "text",
             italic: bool = True) -> ImageTk.PhotoImage:
    """Надпись картинкой: узкая, при желании наклонная."""
    font = _font(kind, size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), text, font=font)
    w = max(1, box[2] - box[0] + SS * 2)
    h = max(1, box[3] - box[1] + SS * 2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((-box[0] + SS, -box[1] + SS), text, font=font, fill=color)
    if italic:
        img = _shear(img)
    img = img.resize((max(1, img.width // SS), max(1, img.height // SS)),
                     Image.LANCZOS)
    return ImageTk.PhotoImage(img)


@functools.lru_cache(maxsize=256)
def bar_png(w: int, h: int, color: str, slant_right: bool = True,
            slant_left: bool = False, edge: str = "", edge_w: int = 0) -> ImageTk.PhotoImage:
    """Плашка со срезанными углами — основа всей графики.

    `edge` рисует вертикальную полосу слева: ей показываем флаг сессии и
    отмечаем лидера.
    """
    W, H = w * SS, h * SS
    dx = int(H * SLANT)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    left_top = dx if slant_left else 0
    left_bot = 0 if slant_left else 0
    right_top = W if not slant_right else W
    poly = [
        (left_top, 0),
        (right_top, 0),
        (right_top - (dx if slant_right else 0), H),
        (left_bot, H),
    ]
    if slant_left:
        poly[0] = (dx, 0)
        poly[3] = (0, H)
    d.polygon(poly, fill=color)
    if edge and edge_w:
        e = edge_w * SS
        d.polygon([(poly[0][0], 0), (poly[0][0] + e, 0), (e, H), (0, H)]
                  if slant_left else [(0, 0), (e, 0), (e, H), (0, H)], fill=edge)
    img = img.resize((w + max(1, dx // SS), h), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


@functools.lru_cache(maxsize=64)
def chip_png(w: int, h: int, color: str) -> ImageTk.PhotoImage:
    """Чип номера машины: параллелограмм под цвет класса."""
    return bar_png(w, h, color, slant_right=True, slant_left=True)


@functools.lru_cache(maxsize=256)
def num_chip_png(number: str, cls: str, w: int, h: int) -> ImageTk.PhotoImage:
    """Чип с номером машины: параллелограмм под цвет класса и цифры внутри.

    Отдаём одной картинкой, чтобы окна на Label обошлись одним виджетом —
    склейка чипа и цифр из двух элементов разъезжается при разной ширине.
    """
    W, H = w * SS, h * SS
    dx = int(H * SLANT)
    img = Image.new("RGBA", (W + dx, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(dx, 0), (W + dx, 0), (W, H), (0, H)], fill=class_color(cls))

    text = str(number)
    font = _font("num", max(7, int(h * 0.62)))
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    layer = Image.new("RGBA", (tw + SS * 2, th + SS * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((-box[0] + SS, -box[1] + SS), text, font=font,
                               fill="#0B0E14")
    layer = _shear(layer)
    img.paste(layer, ((img.width - layer.width) // 2, (H - layer.height) // 2), layer)

    img = img.resize(((W + dx) // SS, H // SS), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def surname(full: str) -> str:
    """Фамилия заглавными — как в эфире показывают пилотов.

    «Тимур Астахов» -> «АСТАХОВ», «A. Petrov» -> «PETROV». Если фамилию не
    выделить, отдаём имя целиком: лучше длинно, чем пусто.
    """
    parts = [p for p in (full or "").replace("\u00a0", " ").split() if p]
    if not parts:
        return ""
    tail = parts[-1]
    if len(tail) <= 2 and len(parts) > 1:      # инициал в конце
        tail = parts[0]
    return tail.upper()
