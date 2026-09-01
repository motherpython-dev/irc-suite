"""Генерирует assets/icon.ico из логотипа ichamps.

Ищет исходник среди SOURCES (первый найденный побеждает). Если ни одного нет —
рисует плейсхолдер, чтобы сборка не вставала.

Логотип нетипично тонкий по краям, поэтому перед масштабированием изображение
обрезается по фактическому содержимому и добавляется небольшой отступ: иначе
в 16x16 для трея шестиугольник превращается в кашу.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
SOURCES = ["logo.png", "icon.png", "logo.jpg", "icon.jpg"]

BG = (13, 17, 23, 255)
ACCENT = (255, 139, 0, 255)      # оранжевый ichamps
FG = (230, 237, 243, 255)


def placeholder(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 16
    d.rounded_rectangle((pad, pad, size - pad, size - pad),
                        radius=size // 6, fill=BG, outline=ACCENT, width=size // 32)
    # стилизованная клетчатая полоса
    cell = size // 8
    x0, y0 = size // 4, size // 2 - cell
    for row in range(2):
        for col in range(4):
            if (row + col) % 2 == 0:
                continue
            x = x0 + col * cell
            y = y0 + row * cell
            d.rectangle((x, y, x + cell, y + cell), fill=FG)
    d.rectangle((x0, y0, x0 + 4 * cell, y0 + 2 * cell), outline=ACCENT, width=max(2, size // 100))
    return img


def find_source() -> Image.Image | None:
    for name in SOURCES:
        p = ASSETS / name
        if p.exists():
            print(f"использую логотип: {p}")
            return Image.open(p).convert("RGBA")
    return None


def square(img: Image.Image, pad_ratio: float = 0.04) -> Image.Image:
    """Обрезать по содержимому, вписать в квадрат с небольшим полем."""
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    pad = int(max(img.size) * pad_ratio)
    side = max(img.size) + pad * 2
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    src = find_source()
    if src is None:
        src = placeholder()
        print(f"исходник не найден ({', '.join(SOURCES)}) — рисую плейсхолдер")
    canvas = square(src)
    out = ASSETS / "icon.ico"
    canvas.save(out, format="ICO", sizes=SIZES)
    print(f"готово: {out}  ({canvas.size[0]}px, размеров в ico: {len(SIZES)})")


if __name__ == "__main__":
    main()
