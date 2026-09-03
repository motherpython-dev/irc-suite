"""Генерирует assets/icon.ico и assets/icon-256.png из логотипа iRC.

Качество мелких размеров решает источник. Растровый логотип 625x248,
ужатый до 16 px, превращается в кашу, поэтому исходником берётся вектор
(`logo-irc.svg`), а растеризует его headless-Chrome — единственный движок,
который здесь есть и не тянет за собой cairo. Если Chrome или SVG нет,
откатываемся на PNG: сборка не должна вставать из-за иконки.

Каждый кадр .ico собирается отдельно: плитка и логотип рисуются в 8 раз
крупнее и уменьшаются LANCZOS под конкретный размер. Если отдать Pillow
один холст и список размеров, он ужмёт его сам и одинаково грубо для всех.

После уменьшения мелкие кадры проходят лёгкую нерезкую маску: даунсэмплинг
неизбежно замыливает контур, а на 16-24 px это половина читаемости.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = [16, 20, 24, 32, 48, 64, 128, 256]
SUPERSAMPLE = 8

BG = (13, 17, 23, 255)           # тёмная плитка — токен --n-bg
ACCENT = (255, 90, 31, 255)
FG = (230, 237, 243, 255)

LOGO_SHARE = 0.82                # доля ширины плитки под логотип
RADIUS = 0.22                    # скругление плитки, доля стороны

CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _browser() -> str | None:
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in CHROME if Path(p).exists()), None)


def render_svg(svg: Path, width: int = 2500) -> Image.Image | None:
    """Растеризовать SVG в PNG нужной ширины через headless-браузер.

    SVG вставляется в страницу картинкой с явными размерами: иначе браузер
    рисует его в собственном размере из атрибутов, и увеличения не выходит.
    """
    exe = _browser()
    if exe is None:
        return None
    src = Image.open(svg.with_suffix(".png")) if svg.with_suffix(".png").exists() else None
    ratio = (src.height / src.width) if src else 248 / 625
    height = round(width * ratio)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copy2(svg, work / "logo.svg")
        (work / "page.html").write_text(
            '<!doctype html><meta charset="utf-8">'
            "<style>html,body{margin:0;padding:0;background:transparent}"
            f"img{{display:block;width:{width}px;height:{height}px}}</style>"
            '<img src="logo.svg">', "utf-8")
        out = work / "out.png"
        try:
            subprocess.run(
                [exe, "--headless", "--disable-gpu", "--hide-scrollbars",
                 "--default-background-color=00000000",
                 f"--screenshot={out}", f"--window-size={width},{height}",
                 (work / "page.html").as_uri()],
                check=True, capture_output=True, timeout=90)
        except Exception as exc:                     # noqa: BLE001
            print(f"растеризация SVG не удалась ({exc}) — беру растровый логотип")
            return None
        if not out.exists():
            return None
        img = Image.open(out).convert("RGBA")
        return img.crop(img.getbbox() or (0, 0, img.width, img.height))


def placeholder(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 16
    d.rounded_rectangle((pad, pad, size - pad, size - pad),
                        radius=size // 6, fill=BG, outline=ACCENT, width=size // 32)
    cell = size // 8
    x0, y0 = size // 4, size // 2 - cell
    for row in range(2):
        for col in range(4):
            if (row + col) % 2:
                d.rectangle((x0 + col * cell, y0 + row * cell,
                             x0 + (col + 1) * cell, y0 + (row + 1) * cell), fill=FG)
    d.rectangle((x0, y0, x0 + 4 * cell, y0 + 2 * cell),
                outline=ACCENT, width=max(2, size // 100))
    return img


def master() -> Image.Image:
    """Логотип в максимальном доступном качестве, обрезанный по содержимому."""
    svg = ASSETS / "logo-irc.svg"
    if svg.exists():
        img = render_svg(svg)
        if img is not None:
            print(f"источник: {svg.name} (вектор, {img.width}x{img.height})")
            return img
    for name in ("logo-irc.png", "logo.png", "icon.png"):
        p = ASSETS / name
        if p.exists():
            print(f"источник: {p.name} (растр)")
            img = Image.open(p).convert("RGBA")
            return img.crop(img.getbbox() or (0, 0, img.width, img.height))
    print("логотип не найден — рисую плейсхолдер")
    return placeholder()


def frame(logo: Image.Image, n: int) -> Image.Image:
    """Один кадр иконки: плитка с логотипом, отрисованные в 8x и уменьшенные."""
    S = n * SUPERSAMPLE
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * RADIUS), fill=BG)

    w = int(S * LOGO_SHARE)
    h = max(1, round(w * logo.height / logo.width))
    if h > S * 0.9:                                  # на случай квадратного лого
        h = int(S * 0.9)
        w = max(1, round(h * logo.width / logo.height))
    scaled = logo.resize((w, h), Image.LANCZOS)
    tile.paste(scaled, ((S - w) // 2, (S - h) // 2), scaled)

    out = tile.resize((n, n), Image.LANCZOS)
    if n <= 32:
        # только мелкие кадры и в меру: сильная маска даёт ореол по контуру,
        # и на 48 px знак начинает выглядеть грязнее исходного
        out = out.filter(ImageFilter.UnsharpMask(radius=0.5, percent=55, threshold=0))
    return out


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    logo = master()
    frames = {n: frame(logo, n) for n in SIZES}

    ico = ASSETS / "icon.ico"
    frames[256].save(ico, format="ICO", sizes=[(n, n) for n in SIZES],
                     append_images=[frames[n] for n in SIZES if n != 256])
    frames[256].save(ASSETS / "icon-256.png")
    print(f"готово: {ico} и icon-256.png; размеров: {len(SIZES)}")


if __name__ == "__main__":
    main()
