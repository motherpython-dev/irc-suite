"""Оверлей поверх iRacing для одномониторной конфигурации.

Три свойства, без которых оверлей поверх сима бесполезен:

1. `-topmost` — рисуется поверх iRacing. Требует, чтобы сим был в режиме
   **borderless windowed**: в exclusive fullscreen поверх окна не рисуется ничего.
2. `WS_EX_NOACTIVATE` — окно НЕ забирает фокус при клике. Иначе каждый клик по
   оверлею отбирал бы ввод у сима, а чат-команды (когда дойдём до флагов) уходили
   бы не туда.
3. `-transparentcolor` — зоны цвета-ключа полностью прозрачны И кликопрозрачны:
   мышь сквозь них попадает в iRacing, а не в оверлей.

Tkinter выбран намеренно: он в stdlib. Тащить браузерный движок ради панели
на 400 пикселей — плюс сотня мегабайт к инсталлятору.

Tkinter обязан жить в главном потоке, поэтому трей запускается через
`run_detached()`, а главный поток отдаётся оверлею.
"""
from __future__ import annotations

import ctypes
import queue
import threading
import tkinter as tk
from ctypes import wintypes

from ...core.simclient import MODE_LIVE, MODE_OFFLINE, MODE_REPLAY

# ---- палитра ichamps ----
# Токены взяты из static/css/racing.css прода (тема data-theme="dark"),
# чтобы оверлей читался как часть продукта, а не как отдельная утилита.
TRANSPARENT = "#ff00fe"          # цвет-ключ, вне панели -> дырка в сим
BG = "#0B0E14"                   # --n-bg
SURFACE = "#141923"              # --n-bg-subtle / --r-surface
SURFACE_2 = "#1B2230"            # --n-bg-hover / --r-surface-2
SURFACE_3 = "#232C3D"            # --n-bg-active / --r-surface-3
LINE = "#28303F"                 # --n-border
LINE_STRONG = "#3A4456"          # --n-border-strong
FG = "#EAEEF5"                   # --n-text
DIM = "#9AA6B8"                  # --n-text-muted
LIGHT = "#6B7689"                # --n-text-light
ACCENT = "#FF5A1F"               # --n-accent (фирменный оранжевый)
ACCENT_HOVER = "#E14A12"         # --n-accent-hover
HIT = "#EF4444"                  # --n-danger
LIVE_C = "#22C55E"               # --n-success
REPLAY_C = "#F59E0B"             # --n-warning
OFF_C = "#6B7689"
EST_C = "#A78BFA"
OFF_TRACK_C = "#38BDF8"          # выезд за пределы трассы
PANEL = SURFACE                  # совместимость с прежними именами

# Фирменные Rubik/Inter в системе обычно не установлены (это веб-шрифты
# Google Fonts) — берём первый доступный, иначе системный гротеск.
FONT_DISPLAY = "Rubik"
FONT_UI = "Inter"
FONT_FALLBACK = "Segoe UI"

MODE_COLOR = {MODE_LIVE: LIVE_C, MODE_REPLAY: REPLAY_C, MODE_OFFLINE: OFF_C}

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xC0DE
CAM_KEEP = "как у меня"        # не менять вид оператора, сменить только машину

user32 = ctypes.windll.user32


def register_bundled_fonts() -> int:
    """Подключить шрифты из бандла только этому процессу.

    AddFontResourceExW с FR_PRIVATE делает шрифт видимым для приложения, не
    устанавливая его в систему: прав администратора не нужно, следов в Windows
    не остаётся. Так оверлей получает тот же Inter/Rubik, что и сайт.
    """
    import sys
    from pathlib import Path
    FR_PRIVATE = 0x10
    bases = (Path(__file__).parent / "assets" / "fonts",
             Path(getattr(sys, "_MEIPASS", ".")) / "assets" / "fonts",
             Path(sys.executable).parent / "_internal" / "assets" / "fonts",
             Path(sys.executable).parent / "assets" / "fonts")
    loaded = 0
    for base in bases:
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.ttf")):
            try:
                if ctypes.windll.gdi32.AddFontResourceExW(str(f), FR_PRIVATE, 0):
                    loaded += 1
            except Exception:
                pass
        if loaded:
            break
    return loaded


_FONT_CACHE: dict[str, str] = {}


def pick_font(*candidates: str) -> str:
    """Первое установленное семейство из списка, иначе системный гротеск."""
    key = "|".join(candidates)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    try:
        import tkinter.font as tkfont
        available = set(tkfont.families())
    except Exception:
        available = set()
    chosen = next((c for c in candidates if c in available), FONT_FALLBACK)
    _FONT_CACHE[key] = chosen
    return chosen


RADIUS_WINDOW = 14
RADIUS_CARD = 12          # соответствует --radius .75rem на сайте
RADIUS_BTN = 12           # верхняя граница; фактический подбирается по высоте


def center_text(canvas: tk.Canvas, item: int, w: float, h: float) -> None:
    """Поставить текст в оптический центр области.

    `anchor="center"` центрирует по метрикам шрифта (ascent + descent), а не по
    видимым глифам: у строки без букв с нижним выносом низ «пустой», и текст
    визуально уезжает вверх. Поэтому меряем фактический bbox и доводим.
    """
    canvas.coords(item, w / 2, h / 2)
    box = canvas.bbox(item)
    if not box:
        return
    x0, y0, x1, y1 = box
    canvas.move(item, (w - (x0 + x1)) / 2, (h - (y0 + y1)) / 2)


def btn_radius(h: int) -> int:
    """Радиус кнопки: до половины высоты, иначе скругление не читается.

    Фиксированные 8 px на кнопке высотой ~30 давали угол, который визуально
    выглядит прямым.
    """
    return max(6, min(RADIUS_BTN, h // 2))


def _hwnd_of(root: tk.Tk) -> int:
    return user32.GetParent(root.winfo_id()) or root.winfo_id()


DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


def apply_round_corners(root: tk.Tk, radius: int = RADIUS_WINDOW) -> None:
    """Скруглить углы САМОГО окна.

    Сначала пробуем DWM (Windows 11): система скругляет со сглаживанием.
    Обрезка регионом (CreateRoundRectRgn + SetWindowRgn) даёт ступенчатый,
    «рваный» край — она остаётся только запасным вариантом для Windows 10.
    """
    hwnd = _hwnd_of(root)
    try:
        val = ctypes.c_int(DWMWCP_ROUND)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(val), ctypes.sizeof(val))
        if res == 0:
            return
    except Exception:
        pass
    try:
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        if w <= 1 or h <= 1:
            return
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, w + 1, h + 1, radius * 2, radius * 2)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


_IMG_CACHE: dict = {}


def rounded_png(w: int, h: int, r: int, fill: str, bg: str,
                stripe: str | None = None, stripe_w: int = 4):
    """Скруглённый прямоугольник как изображение.

    Canvas со `smooth=True` даёт рваный край на малых радиусах, поэтому рисуем
    в PIL с четырёхкратным супер-сэмплингом и уменьшаем — края получаются
    сглаженными. `stripe` — цветная метка слева, скруглённая заодно с карточкой.

    Прозрачности у виджетов Tk нет, поэтому подкладываем цвет фона `bg`.
    """
    key = (w, h, r, fill, bg, stripe, stripe_w)
    if key in _IMG_CACHE:
        return _IMG_CACHE[key]
    from PIL import Image, ImageDraw, ImageTk
    k = 4
    img = Image.new("RGB", (w * k, h * k), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=r * k, fill=fill)
    if stripe:
        # полоса = та же скруглённая форма, обрезанная по ширине
        band = Image.new("RGB", (w * k, h * k), bg)
        ImageDraw.Draw(band).rounded_rectangle(
            [0, 0, w * k - 1, h * k - 1], radius=r * k, fill=stripe)
        img.paste(band.crop((0, 0, stripe_w * k, h * k)), (0, 0))
    photo = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
    _IMG_CACHE[key] = photo
    return photo


def _apply_no_activate(root: tk.Tk) -> None:
    """Окно перестаёт забирать фокус и пропадает из Alt+Tab."""
    try:
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        get_l = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_l = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        style = get_l(hwnd, GWL_EXSTYLE)
        set_l(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass


MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
        "shift": MOD_SHIFT, "win": MOD_WIN}
VKS = {f"f{i}": 0x6F + i for i in range(1, 13)}     # F1..F12 = 0x70..0x7B


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """'ctrl+alt+r' -> (модификаторы, virtual-key). None, если не разобрали."""
    mods, vk = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in VKS:
            vk = VKS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
    return (mods, vk) if vk and mods else None


class RoundButton(tk.Canvas):
    """Кнопка со скруглением. Обычный tk.Button всегда прямоугольный."""

    def __init__(self, parent, text: str, command, font, primary: bool = False,
                 pad_x: int = 12, pad_y: int = 6, bg=None):
        self.bg_host = bg or SURFACE
        self.fill = ACCENT if primary else SURFACE_2
        self.fill_hover = ACCENT_HOVER if primary else SURFACE_3
        self.fg = "#FFFFFF" if primary else FG
        self.command = command

        tmp = tk.Label(parent, text=text, font=font)
        w = tmp.winfo_reqwidth() + pad_x * 2
        h = tmp.winfo_reqheight() + pad_y * 2
        tmp.destroy()

        super().__init__(parent, width=w, height=h, bg=self.bg_host,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text, self._font = text, font
        self._img = self._img_hover = None
        self._bg_id = self.create_image(0, 0, anchor="nw")
        self._txt_id = self.create_text(w / 2, h / 2, text=text, fill=self.fg,
                                        font=font)
        self._draw(w, h)

        # Фон обязан перерисовываться под ФАКТИЧЕСКИЙ размер виджета: при
        # упаковке Tk сжимает кнопку (запрошенные 62x31 превращались в 57x25),
        # и нарисованная под старый размер картинка обрезалась ровно по углам —
        # скругление уезжало за границу, кнопка выглядела прямоугольной.
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height))
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self.itemconfigure(self._bg_id, image=self._img_hover))
        self.bind("<Leave>", lambda e: self.itemconfigure(self._bg_id, image=self._img))

    def _draw(self, w: int, h: int) -> None:
        if w < 4 or h < 4 or (w, h) == getattr(self, "_size", None):
            return
        self._size = (w, h)
        r = btn_radius(h)
        self._img = rounded_png(w, h, r, self.fill, self.bg_host)
        self._img_hover = rounded_png(w, h, r, self.fill_hover, self.bg_host)
        self.itemconfigure(self._bg_id, image=self._img)
        self.tag_raise(self._txt_id)
        center_text(self, self._txt_id, w, h)


class RoundEntry(tk.Canvas):
    """Поле ввода со скруглением.

    tk.Entry скруглить нельзя, поэтому кладём его внутрь Canvas, у которого
    фоном служит скруглённая картинка. Сам Entry делаем без рамки и с тем же
    фоном — стыка не видно.
    """

    def __init__(self, parent, textvariable, font, width: int = 190,
                 height: int = 28, bg=None, placeholder: str = ""):
        self.bg_host = bg or SURFACE
        self._size = None
        super().__init__(parent, width=width, height=height, bg=self.bg_host,
                         highlightthickness=0, bd=0)
        self._bg_id = self.create_image(0, 0, anchor="nw")

        self.entry = tk.Entry(self, textvariable=textvariable, font=font,
                              bg=SURFACE_2, fg=FG, insertbackground=ACCENT,
                              bd=0, highlightthickness=0, relief="flat",
                              disabledbackground=SURFACE_2)
        self._win_id = self.create_window(12, height / 2, anchor="w",
                                          window=self.entry,
                                          width=width - 24, height=height - 10)
        self._draw(width, height)
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height))

        # Подсказку рисуем ВНУТРИ Entry, а не на канве: create_window кладёт
        # виджет поверх всего, и текст канвы им закрывался.
        self.var = textvariable
        self.placeholder = placeholder
        self._ph_on = False
        self.entry.bind("<FocusIn>", self._ph_hide, add="+")
        self.entry.bind("<FocusOut>", self._ph_show, add="+")
        self._ph_show()

    def _ph_show(self, _=None) -> None:
        if not self.var.get():
            self._ph_on = True
            self.entry.configure(fg=LIGHT)
            self.entry.insert(0, self.placeholder)

    def _ph_hide(self, _=None) -> None:
        if self._ph_on:
            self._ph_on = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=FG)

    def value(self) -> str:
        """Текст поля без подсказки."""
        return "" if self._ph_on else self.var.get()

    def set_value(self, text: str) -> None:
        self._ph_on = False
        self.entry.configure(fg=FG)
        self.var.set(text)

    def _draw(self, w: int, h: int) -> None:
        if w < 8 or h < 8 or (w, h) == self._size:
            return
        self._size = (w, h)
        self._img = rounded_png(w, h, btn_radius(h), SURFACE_2, self.bg_host)
        self.itemconfigure(self._bg_id, image=self._img)
        self.coords(self._win_id, 12, h / 2)
        self.itemconfigure(self._win_id, width=w - 24, height=h - 10)


class RoundSelect(tk.Canvas):
    """Выпадающий список со скруглением.

    tk.OptionMenu — это Menubutton с нативным меню Windows: ни сам виджет, ни
    выпадающая часть скруглению не поддаются. Поэтому рисуем кнопку сами, а
    список показываем во всплывающем окне, которому DWM даёт скруглённые углы.
    """

    def __init__(self, parent, font, on_pick, width: int = 150,
                 height: int = 26, bg=None):
        self.bg_host = bg or SURFACE
        self.font = font
        self.on_pick = on_pick
        self.values: list[str] = []
        self.value = ""
        self._popup: tk.Toplevel | None = None
        self._size = None

        super().__init__(parent, width=width, height=height, bg=self.bg_host,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bg_id = self.create_image(0, 0, anchor="nw")
        self._txt_id = self.create_text(10, height / 2, anchor="w", fill=FG,
                                        text="", font=font)
        # стрелка рисуется треугольником: символ «⌄» в разных шрифтах
        # отрисовывается по-разному и сидит не по центру строки
        self._arrow_id = self.create_polygon(0, 0, 0, 0, 0, 0, fill=DIM,
                                             outline="")
        self._draw(width, height)
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height))
        self.bind("<Button-1>", lambda e: self.toggle())

    def _draw(self, w: int, h: int) -> None:
        if w < 4 or h < 4 or (w, h) == self._size:
            return
        self._size = (w, h)
        self._img = rounded_png(w, h, btn_radius(h), SURFACE_2, self.bg_host)
        self._img_hover = rounded_png(w, h, btn_radius(h), SURFACE_3, self.bg_host)
        self.itemconfigure(self._bg_id, image=self._img)
        self.tag_raise(self._txt_id)
        self.tag_raise(self._arrow_id)
        self._align_text(w, h)
        ax, ay, aw = w - 18, h / 2 + 1, 5
        self.coords(self._arrow_id,
                    ax, ay - aw / 2, ax + aw * 2, ay - aw / 2, ax + aw, ay + aw / 2)

    def _align_text(self, w: float, h: float) -> None:
        """Текст слева, но по вертикали — в оптический центр."""
        self.coords(self._txt_id, 10, h / 2)
        box = self.bbox(self._txt_id)
        if box:
            self.move(self._txt_id, 0, (h - (box[1] + box[3])) / 2)

    def set_values(self, values: list[str], current: str) -> None:
        self.values = values
        self.set(current)

    def set(self, value: str) -> None:
        self.value = value
        self.itemconfigure(self._txt_id, text=value)
        if self._size:
            self._align_text(*self._size)

    def toggle(self) -> None:
        self.close() if self._popup else self.open()

    def close(self) -> None:
        if self._popup:
            self._popup.destroy()
            self._popup = None

    def open(self) -> None:
        if not self.values:
            return
        w = self.winfo_width()
        item_h = self.winfo_height()
        pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=SURFACE)
        h = item_h * len(self.values) + 8
        # список раскрываем ВВЕРХ: селектор стоит у нижнего края оверлея
        pop.geometry(f"{w}x{h}+{self.winfo_rootx()}+{self.winfo_rooty() - h - 2}")

        holder = tk.Canvas(pop, width=w, height=h, bg=SURFACE,
                           highlightthickness=0, bd=0)
        holder.pack()
        bgim = rounded_png(w, h, RADIUS_CARD, SURFACE_2, SURFACE)
        holder.create_image(0, 0, anchor="nw", image=bgim)
        holder.image = bgim                    # держим ссылку

        for i, val in enumerate(self.values):
            y = 4 + i * item_h
            tag = f"it{i}"
            holder.create_rectangle(4, y, w - 4, y + item_h, fill="", outline="",
                                    tags=tag)
            holder.create_text(12, y + item_h / 2, anchor="w", tags=tag,
                               text=val, font=self.font,
                               fill=ACCENT if val == self.value else FG)
            holder.tag_bind(tag, "<Button-1>",
                            lambda e, v=val: (self.close(), self.on_pick(v)))
            holder.tag_bind(tag, "<Enter>", lambda e, t=tag, hl=holder:
                            hl.itemconfigure(t, fill=SURFACE_3))
            holder.tag_bind(tag, "<Leave>", lambda e, t=tag, hl=holder:
                            hl.itemconfigure(t, fill=""))

        self._popup = pop
        pop.update_idletasks()
        _apply_no_activate(pop)
        apply_round_corners(pop, RADIUS_CARD)
        pop.bind("<FocusOut>", lambda e: self.close())


def load_logo(size: int = 22):
    """Логотип ichamps из ассетов. Ищем и рядом с бандлом, и во временной
    распаковке PyInstaller, и рядом с exe."""
    import sys
    from pathlib import Path
    names = ("icon.png", "logo.png", "icon.ico")
    bases = (Path(__file__).parent / "assets",
             Path(getattr(sys, "_MEIPASS", ".")) / "assets",
             Path(sys.executable).parent / "assets",
             Path(sys.executable).parent / "_internal" / "assets")
    for base in bases:
        for name in names:
            f = base / name
            if not f.exists():
                continue
            try:
                from PIL import Image, ImageTk
                img = Image.open(f).convert("RGBA")
                bbox = img.getbbox()
                if bbox:
                    img = img.crop(bbox)
                # пропорции сохраняем: логотип не квадратный после обрезки
                # по содержимому, и resize в квадрат его сплющивал
                img.thumbnail((size, size), Image.LANCZOS)
                flat = Image.new("RGBA", (size, size), SURFACE)
                flat.alpha_composite(img, ((size - img.width) // 2,
                                           (size - img.height) // 2))
                return ImageTk.PhotoImage(flat.convert("RGB"))
            except Exception:
                continue
    return None


class HotkeyListener(threading.Thread):
    """Глобальный хоткей показать/скрыть.

    Комбинация может быть занята другой программой (RegisterHotKey вернёт
    ERROR_HOTKEY_ALREADY_REGISTERED) — на этой машине, например, занят
    Ctrl+Alt+R. Поэтому перебираем список кандидатов и берём первый свободный,
    а какой именно достался — показываем в интерфейсе.

    Tkinter не потокобезопасен, поэтому поток лишь кладёт событие в очередь,
    а разбирает его таймер главного потока.
    """

    def __init__(self, q: queue.Queue, candidates: list[str]):
        super().__init__(daemon=True, name="hotkey")
        self.q = q
        self.candidates = candidates
        self.registered = False
        self.active = ""

    def run(self) -> None:
        # RegisterHotKey привязывает хоткей к ПОТОКУ, поэтому и очередь
        # сообщений должна крутиться здесь же.
        for text in self.candidates:
            combo = parse_hotkey(text)
            if not combo:
                continue
            if user32.RegisterHotKey(None, HOTKEY_ID, combo[0], combo[1]):
                self.registered = True
                self.active = text
                break
        if not self.registered:
            return
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.q.put("toggle")
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)


class Overlay:
    WIDTH = 430

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.visible = True
        self._hotkeys: queue.Queue = queue.Queue()
        self._rows: list[dict] = []
        self._tw_cache: dict = {}
        self._keep: list = []      # Tk не держит ссылки на PhotoImage
        self._last_width = 0
        self._pending = None
        self._search_popup = None
        self._drag = (0, 0)

        register_bundled_fonts()      # до создания Tk: иначе список семейств уже кэширован
        # Корень Tk один на всё приложение (модули включаются на лету), поэтому
        # окно оверлея — Toplevel поверх общего хоста.
        from ...core.ui import host
        self.root = tk.Toplevel(host.root()) if host.is_running() else tk.Tk()
        self.root.withdraw()
        self.f_display = pick_font(FONT_DISPLAY, FONT_UI)   # Rubik -> Inter -> Segoe UI
        self.f_ui = pick_font(FONT_UI, FONT_DISPLAY)
        self.root.title("ichamps Race Control Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(self.cfg.get("overlay_alpha", 0.92)))
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass                      # не Windows — оверлей будет просто окном

        x = int(self.cfg.get("overlay_x", 24))
        y = int(self.cfg.get("overlay_y", 24))
        self.root.geometry(f"{self.WIDTH}x600+{x}+{y}")

        self._build()
        self.root.deiconify()
        self.root.update_idletasks()
        self._fit_height()
        _apply_no_activate(self.root)
        apply_round_corners(self.root)
        self.canvas.bind("<Configure>",
                         lambda e: self._render_list(self._rows, force=True))

        self.hotkeys = HotkeyListener(
            self._hotkeys,
            self.cfg.get("overlay_hotkeys") or ["ctrl+alt+r"])
        self.hotkeys.start()
        self.root.after(300, self._show_hotkey)
        self._tick()

    def _fit_height(self) -> None:
        """Подогнать высоту окна под содержимое.

        Tk масштабирует шрифты по своему `tk scaling` (на этой машине 1.334 при
        системных 100%), поэтому любая фиксированная высота на другой машине
        обрежет нижний ряд кнопок. Считаем требуемую высоту и добавляем место
        под ленту.
        """
        try:
            self.root.update_idletasks()
            chrome = sum(w.winfo_reqheight() for w in self.panel.winfo_children()
                         if w is not self.canvas)
            h = min(int(self.root.winfo_screenheight() * 0.8), chrome + 340)
            self.root.geometry(f"{self.WIDTH}x{h}")
            self.root.update_idletasks()
        except Exception:
            pass

    def _show_hotkey(self) -> None:
        """Показать, какая комбинация реально досталась (могла быть занята)."""
        if self.hotkeys.registered:
            self.lbl_hotkey.configure(text=self.hotkeys.active.replace('+', ' + ').title())
        else:
            self.lbl_hotkey.configure(text="хоткей занят", fg=HIT)

    # ---------- построение ----------

    def _build(self) -> None:
        # внешний контейнер прозрачен: клики мимо панели уходят в сим
        outer = tk.Frame(self.root, bg=TRANSPARENT)
        outer.pack(fill="both", expand=True)

        self.panel = tk.Frame(outer, bg=BG, highlightthickness=1,
                              highlightbackground=LINE)
        self.panel.pack(fill="both", expand=True)

        # --- шапка (за неё же таскаем окно) ---
        head = tk.Frame(self.panel, bg=SURFACE, cursor="fleur")
        head.pack(fill="x")
        for w in (head,):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # логотип ichamps; если ассет не нашёлся — фирменный оранжевый квадрат
        self._logo_img = load_logo(22)
        if self._logo_img is not None:
            self.logo = tk.Label(head, image=self._logo_img, bg=SURFACE, bd=0)
        else:
            self.logo = tk.Label(head, text="iC", bg=ACCENT, fg="#FFFFFF",
                                 font=(self.f_display, 9, "bold"), width=3, pady=2)
        self.logo.pack(side="left", padx=(10, 8), pady=9)
        self.logo.bind("<Button-1>", self._drag_start)
        self.logo.bind("<B1-Motion>", self._drag_move)

        self.dot = tk.Canvas(head, width=10, height=10, bg=SURFACE,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 5), pady=8)
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=OFF_C, outline="")

        # статус-лейблы в стиле "uppercase tracking-wide" из гайдлайна
        self.lbl_mode = tk.Label(head, text="O F F L I N E", bg=SURFACE, fg=OFF_C,
                                 font=(self.f_display, 8, "bold"))
        self.lbl_mode.pack(side="left")
        self.lbl_mode.bind("<Button-1>", self._drag_start)
        self.lbl_mode.bind("<B1-Motion>", self._drag_move)

        self.lbl_meta = tk.Label(head, text="—", bg=SURFACE, fg=DIM,
                                 font=(self.f_ui, 8))
        self.lbl_meta.pack(side="left", padx=8)
        self.lbl_meta.bind("<Button-1>", self._drag_start)
        self.lbl_meta.bind("<B1-Motion>", self._drag_move)

        tk.Button(head, text="✕", command=self.hide, bg=SURFACE, fg=DIM,
                  bd=0, activebackground=SURFACE_3, activeforeground=FG,
                  font=(self.f_ui, 9), cursor="hand2").pack(side="right", padx=6)

        # --- баннер режима повтора ---
        self.banner = tk.Label(self.panel, bg="#2A2210", fg=REPLAY_C,
                               font=(self.f_display, 8, "bold"), anchor="w", padx=10)
        # пакуется только в REPLAY

        # --- кнопки ---
        # ВАЖЕН ПОРЯДОК: панели прижимаются к низу и упаковываются раньше
        # ленты. Иначе лента с expand=True съедает высоту, и нижний ряд
        # (вместе с выбором камеры) уезжает за край окна.
        bar = tk.Frame(self.panel, bg=SURFACE)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(True)
        self._btn(bar, "К LIVE", self._to_live, primary=True).pack(
            side="left", padx=(8, 4), pady=5)
        self._btn(bar, "Камера", lambda: self._cmd("cam_at_incident")).pack(
            side="left", padx=4, pady=5)
        # прыжки по СОБСТВЕННЫМ маркерам инцидентов iRacing — они есть в ленте
        # всегда, даже если наш детектор эпизод не поймал
        self._btn(bar, "‹ iR", lambda: self._cmd("jump_sim_incident", forward=False)).pack(
            side="left", padx=(4, 0), pady=5)
        self._btn(bar, "iR ›", lambda: self._cmd("jump_sim_incident", forward=True)).pack(
            side="left", padx=(1, 4), pady=5)
        self.lbl_hotkey = tk.Label(bar, text="", bg=SURFACE, fg=DIM,
                                   font=(self.f_ui, 7))
        self.lbl_hotkey.pack(side="right", padx=10)

        # --- флаги и штрафы ---
        flags = tk.Frame(self.panel, bg=SURFACE)
        flags.pack(fill="x", side="bottom")

        row1 = tk.Frame(flags, bg=SURFACE)
        row1.pack(fill="x")
        self._btn(row1, "ЖЁЛТЫЙ", lambda: self._flag("yellow"),
                  primary=False).pack(side="left", padx=(8, 6), pady=(4, 2))
        self.btn_yellow = row1.winfo_children()[-1]
        self.btn_yellow.itemconfigure(self.btn_yellow._txt_id, fill=REPLAY_C)

        tk.Label(row1, text="круги:", bg=SURFACE, fg=DIM,
                 font=(self.f_ui, 8)).pack(side="left")
        self._btn(row1, "−", lambda: self._flag("pace_laps", delta=-1),
                  pad_x=8).pack(side="left", padx=2, pady=(4, 2))
        self.lbl_pace = tk.Label(row1, text="2", bg=SURFACE, fg=FG,
                                 font=(self.f_display, 9, "bold"), width=2)
        self.lbl_pace.pack(side="left")
        self._btn(row1, "+", lambda: self._flag("pace_laps", delta=1),
                  pad_x=8).pack(side="left", padx=2, pady=(4, 2))

        row2 = tk.Frame(flags, bg=SURFACE)
        row2.pack(fill="x")
        tk.Label(row2, text="#", bg=SURFACE, fg=DIM,
                 font=(self.f_display, 9, "bold")).pack(side="left", padx=(9, 0))
        self.car_var = tk.StringVar()
        self.car_var.trace_add("write", lambda *_: self._search_drivers())
        self.car_box = RoundEntry(row2, self.car_var, (self.f_display, 9, "bold"),
                                  width=196, height=28, bg=SURFACE,
                                  placeholder="номер или фамилия")
        self.car_box.pack(side="left", padx=(2, 8), pady=(2, 6))
        self.car_entry = self.car_box.entry
        self.car_entry.bind("<FocusIn>", lambda e: self._search_drivers())
        self.car_entry.bind("<Escape>", lambda e: self._close_search())
        self.car_entry.bind("<Return>", lambda e: self._pick_first_driver())

        self._btn(row2, "DT", lambda: self._penalty("drive_through")).pack(
            side="left", padx=2, pady=(2, 6))
        self._btn(row2, f"+{int(self.cfg.get('penalty_seconds', 5))}с",
                  lambda: self._penalty("time_penalty")).pack(
            side="left", padx=2, pady=(2, 6))
        self._btn(row2, "СНЯТЬ ЧФ", lambda: self._flag("clear")).pack(
            side="left", padx=2, pady=(2, 6))

        # --- выбор камеры для перехода к инциденту ---
        cam = tk.Frame(self.panel, bg=SURFACE)
        cam.pack(fill="x", side="bottom")
        tk.Label(cam, text="Камера инцидента:", bg=SURFACE, fg=DIM,
                 font=(self.f_ui, 8)).pack(side="left", padx=(10, 6), pady=(0, 8))

        self.cam_menu = RoundSelect(cam, font=(self.f_ui, 8),
                                    on_pick=self._pick_camera, width=170,
                                    height=24, bg=SURFACE)
        self.cam_menu.pack(side="left", pady=(0, 7))
        self.cam_menu.set(CAM_KEEP)
        self._cam_names: list[str] = []

        self.toast = tk.Label(self.panel, text="", bg=SURFACE, fg=DIM,
                              font=(self.f_ui, 8), anchor="w", padx=10)
        self.toast.pack(fill="x", side="bottom")

        # --- лента инцидентов: занимает всё, что осталось ---
        self.canvas = tk.Canvas(self.panel, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=(6, 4))

    def _refresh_cameras(self) -> None:
        """Список групп камер приходит из сессии, поэтому наполняем его, только
        когда сим подключён, и пересобираем при смене сессии."""
        data = self.app.cameras()
        names = [CAM_KEEP] + [g["name"] for g in data["groups"]]
        if names == self._cam_names:
            return
        self._cam_names = names

        current = data["current"] or CAM_KEEP
        self.cam_menu.set_values(names, current if current in names else CAM_KEEP)

    def _pick_camera(self, name: str) -> None:
        value = "" if name == CAM_KEEP else name
        r = self.app.set_camera(value)
        if r.get("ok"):
            self.cam_menu.set(name)
            self._notify(f"Камера инцидента: {name}")
        else:
            self._notify(r.get("message", "не удалось"), False)

    def _btn(self, parent, text, cmd, primary=False, pad_x=12):
        # именно RoundButton: tk.Button всегда прямоугольный, скруглить его
        # штатными средствами Tk нельзя
        return RoundButton(parent, text, cmd, font=(self.f_display, 8, "bold"),
                           primary=primary, bg=SURFACE, pad_x=pad_x)

    # ---------- перетаскивание ----------

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")
        self.cfg["overlay_x"], self.cfg["overlay_y"] = x, y

    # ---------- действия ----------

    def _notify(self, text: str, ok: bool = True) -> None:
        self.toast.configure(text=text, fg=DIM if ok else HIT)
        self.root.after(4000, lambda: self.toast.configure(text=""))

    def _to_live(self) -> None:
        r = self.app.resume_live()
        self._notify("Возврат к LIVE" if r.get("ok") else
                     r.get("message", "не выполнено"), r.get("ok", False))

    def _cmd(self, name: str, forward: bool = True) -> None:
        r = self.app.command(name, {"forward": forward})
        if not r.get("ok"):
            self._notify(r.get("message", "не выполнено"), False)

    # ---------- поиск пилота ----------

    def _search_drivers(self) -> None:
        """Подсказки по номеру и фамилии прямо под полем ввода."""
        q = self.car_box.value() if hasattr(self, "car_box") else self.car_var.get()
        # выбранный пилот подставляется как «24 · Иванов» — по такому запросу
        # искать бессмысленно, список просто закрываем
        if " · " in q:
            self._close_search()
            return
        matches = self.app.find_drivers(q, limit=6)
        if not matches:
            self._close_search()
            return
        self._show_search(matches)

    def _pick_first_driver(self) -> None:
        m = self.app.find_drivers(self.car_var.get(), limit=1)
        if m:
            self._apply_driver(m[0])

    def _apply_driver(self, d: dict) -> None:
        self._close_search()
        self.car_box.set_value(f"{d['car_number']} · {d['name'].split()[-1][:12]}")

    def car_number(self) -> str:
        """Номер машины из поля: до разделителя, если выбран пилот."""
        return self.car_box.value().split(" · ")[0].strip().lstrip("#")

    def _close_search(self) -> None:
        if self._search_popup:
            self._search_popup.destroy()
            self._search_popup = None

    def _show_search(self, items: list[dict]) -> None:
        self._close_search()
        w, item_h = 230, 24
        h = item_h * len(items) + 8
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=SURFACE)
        anchor = getattr(self, "car_box", self.car_entry)
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() - h - 2      # вверх: панель у нижнего края
        pop.geometry(f"{w}x{h}+{x}+{y}")

        cv = tk.Canvas(pop, width=w, height=h, bg=SURFACE,
                       highlightthickness=0, bd=0)
        cv.pack()
        bgim = rounded_png(w, h, RADIUS_CARD, SURFACE_2, SURFACE)
        cv.create_image(0, 0, anchor="nw", image=bgim)
        cv.image = bgim

        for i, d in enumerate(items):
            yy = 4 + i * item_h
            tag = f"drv{i}"
            cv.create_rectangle(4, yy, w - 4, yy + item_h, fill="", outline="",
                                tags=tag)
            cv.create_text(14, yy + item_h / 2, anchor="w", tags=tag,
                           text=f"#{d['car_number']}", fill=ACCENT,
                           font=(self.f_display, 8, "bold"))
            cv.create_text(56, yy + item_h / 2, anchor="w", tags=tag,
                           text=d["name"][:26], fill=FG, font=(self.f_ui, 8))
            cv.tag_bind(tag, "<Button-1>", lambda e, dd=d: self._apply_driver(dd))
            cv.tag_bind(tag, "<Enter>", lambda e, t=tag: cv.itemconfigure(t, fill=SURFACE_3))
            cv.tag_bind(tag, "<Leave>", lambda e, t=tag: cv.itemconfigure(t, fill=""))

        self._search_popup = pop
        pop.update_idletasks()
        _apply_no_activate(pop)
        apply_round_corners(pop, RADIUS_CARD)

    def _flag(self, action: str, **params) -> None:
        """Команды, которые не наказывают: жёлтый, круги, снятие флагов."""
        params["car"] = self.car_number()
        r = self.app.flag_action(action, params)
        if r.get("ok"):
            if "pace_laps" in r:
                self.lbl_pace.configure(text=str(r["pace_laps"]))
            self._notify({"yellow": "Жёлтый флаг брошен",
                          "pace_laps": f"Кругов под жёлтым: {r.get('pace_laps')}",
                          "clear": f"Флаги сняты с #{params['car']}"}.get(
                              action, "Команда отправлена"))
        else:
            self._notify(r.get("message", "не выполнено"), False)

    def _penalty(self, action: str) -> None:
        """Штрафы требуют подтверждения: отменить выданный в живой гонке
        штраф нельзя, а кнопки стоят рядом с безобидными."""
        car = self.car_number()
        if not car:
            self._notify("Укажите номер машины", False)
            return
        label = "DT" if action == "drive_through" else             f"+{int(self.cfg.get('penalty_seconds', 5))}с"
        if self._pending != (action, car):
            self._pending = (action, car)
            self._notify(f"Ещё раз — подтвердить {label} для #{car}", False)
            self.root.after(4000, self._clear_pending)
            return
        self._clear_pending()
        params = {"car": car, "seconds": int(self.cfg.get("penalty_seconds", 5))}
        r = self.app.flag_action(action, params)
        self._notify(f"{label} выдан #{car}" if r.get("ok")
                     else r.get("message", "не выполнено"), r.get("ok", False))

    def _set_car(self, number: str) -> None:
        """Подставить машину из карточки, не открывая список подсказок."""
        self._close_search()
        self.car_box.set_value(number)
        self._close_search()

    def _clear_pending(self) -> None:
        self._pending = None

    def _show_incident(self, inc_id: int) -> None:
        r = self.app.show_incident(inc_id)
        self._notify("Повтор открыт" if r.get("ok") else
                     r.get("message", "не выполнено"), r.get("ok", False))

    # ---------- показ/скрытие ----------

    def request_toggle(self) -> None:
        """Потокобезопасная точка входа — из трея или любого другого потока."""
        self._hotkeys.put("toggle")

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def show(self) -> None:
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        _apply_no_activate(self.root)
        apply_round_corners(self.root)

    def hide(self) -> None:
        self.visible = False
        self.root.withdraw()

    # ---------- обновление ----------

    def _tick(self) -> None:
        try:
            while True:
                if self._hotkeys.get_nowait() == "toggle":
                    self.toggle()
        except queue.Empty:
            pass

        if self.visible:
            try:
                self._refresh()
            except Exception:
                pass
        self.root.after(500, self._tick)

    def _refresh(self) -> None:
        st = self.app.state()
        mode = st["mode"]
        color = MODE_COLOR.get(mode, OFF_C)
        self.dot.itemconfigure(self._dot_id, fill=color)
        self.lbl_mode.configure(text=" ".join(mode), fg=color)

        if st["connected"]:
            t = int(st["session_time"])
            clock = f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}"
            meta = f"{st['track'] or 'сессия'} · {clock}"
            if st["session_time_estimated"]:
                meta += " (оценочно)"
            # в тест-драйве нет ResultsPositions: поле не видно, ловятся
            # только свои инциденты. Без этой пометки пустая лента выглядит
            # как поломка
            if not st.get("results_available"):
                # спектатору «свои инциденты» недоступны в принципе:
                # для него единственный источник — геометрия выездов
                meta += " · выезды" if st.get("spectator") else " · только свои"
        else:
            meta = "iRacing не найден"
        self.lbl_meta.configure(text=meta)

        if st["connected"]:
            self._refresh_cameras()

        if mode == MODE_REPLAY:
            self.banner.configure(
                text=f"ПОВТОР · детект контекста на паузе · "
                     f"новых: {st['missed_in_replay']}")
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", after=self.lbl_mode.master)
        elif self.banner.winfo_ismapped():
            self.banner.pack_forget()

        self._render_list(self.app.incidents()[:8])

    CARD_H = 54
    CARD_GAP = 6

    def _render_list(self, incidents: list[dict], force: bool = False) -> None:
        sig = [(i["id"], i["reviewed"]) for i in incidents]
        same = incidents and sig == [(r["id"], r["reviewed"]) for r in self._rows]
        if same and not force and self._last_width == self.canvas.winfo_width():
            return                      # ни данные, ни ширина не менялись
        self._rows = incidents
        self._last_width = self.canvas.winfo_width()

        c = self.canvas
        c.delete("all")
        self._keep.clear()
        # winfo_width() до первой отрисовки возвращает 1, а `1 or X` — это 1,
        # из-за чего карточки рисовались шириной 200 вместо реальной. Поэтому
        # проверяем осмысленность значения явно.
        cw = c.winfo_width()
        width = cw if cw > 50 else self.WIDTH - 16

        if not incidents:
            st = self.app.state()
            if not st["connected"]:
                text = "iRacing не найден"
            elif not st.get("results_available"):
                text = ("Сессия не отдаёт счётчики инцидентов\nловим выезды по всему полю"
                        if st.get("spectator") else
                        "Видны только свои инциденты\n"
                        f"мой счётчик: {st.get('player_incidents', 0)}x")
            else:
                text = "Инцидентов пока нет"
            c.create_text(width / 2, 46, text=text, fill=DIM, justify="center",
                          font=(self.f_ui, 9))
            return

        y = 4
        for inc in incidents:
            self._draw_card(c, inc, 2, y, width - 4, self.CARD_H)
            y += self.CARD_H + self.CARD_GAP
        c.configure(scrollregion=(0, 0, width, y))

    def _draw_card(self, c: tk.Canvas, inc: dict, x: float, y: float,
                   w: float, h: float) -> None:
        off = inc.get("kind") == "offtrack"
        accent = EST_C if inc["estimated"] else (OFF_TRACK_C if off else HIT)
        tag = f"card{inc['id']}"

        # фон карточки вместе с цветной меткой — одним изображением, чтобы
        # скругление метки совпадало со скруглением карточки
        img = rounded_png(int(w), int(h), RADIUS_CARD, SURFACE_2, BG, stripe=accent)
        img_h = rounded_png(int(w), int(h), RADIUS_CARD, SURFACE_3, BG, stripe=accent)
        bg_id = c.create_image(x, y, image=img, anchor="nw", tags=tag)
        self._keep.extend((img, img_h))

        num_id = c.create_text(x + 34, y + h / 2, text=f"#{inc['car_number']}",
                               fill=FG, font=(self.f_display, 11, "bold"), tags=tag)
        box = c.bbox(num_id)
        if box:                      # тот же оптический центр, что и в кнопках
            c.move(num_id, 0, (y + h / 2) - (box[1] + box[3]) / 2)

        name = inc["driver_name"][:22]
        c.create_text(x + 60, y + 17, text=name, fill=FG, anchor="w",
                      font=(self.f_display, 9, "bold"), tags=tag)

        badge = (f"вне трассы {inc['duration']:.0f}с" if off
                 else f"+{inc['inc_delta']}x")
        bx = x + 66 + self._text_width(name, (self.f_display, 9, "bold"))
        bx = self._draw_badge(c, bx, y + 17, badge, accent, tag)

        # Вес инцидента. Геометрия выезда веса не знает — он приходит только из
        # счётчика сессии, поэтому пока его нет, показываем ожидаемый (Off Track
        # в iRacing = 1x) с тильдой, чтобы не выдавать догадку за факт.
        if off:
            if inc["inc_delta"] > 0:
                bx = self._draw_badge(c, bx + 5, y + 17,
                                      f"+{inc['inc_delta']}x", HIT, tag)
            else:
                bx = self._draw_badge(c, bx + 5, y + 17, "~1x", LIGHT, tag)

        if inc["estimated"]:
            self._draw_badge(c, bx + 5, y + 17, "оценочно", EST_C, tag)

        t = int(max(0, inc["session_time"]))
        c.create_text(x + w - 12, y + 17, anchor="e", fill=LIGHT,
                      text="%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60),
                      font=(self.f_ui, 8), tags=tag)

        near = (", ".join("#" + n for n in inc["nearby"])
                if inc["nearby"] else "рядом никого")
        c.create_text(x + 60, y + 36, text=f"круг {inc['lap']}  ·  {near}"[:46],
                      fill=DIM, anchor="w", font=(self.f_ui, 8), tags=tag)

        c.tag_bind(tag, "<Button-1>",
                   lambda e, i=inc["id"], n=inc["car_number"]: (
                       self._set_car(n), self._show_incident(i)))
        c.tag_bind(tag, "<Enter>", lambda e, b=bg_id, i=img_h: c.itemconfigure(b, image=i))
        c.tag_bind(tag, "<Leave>", lambda e, b=bg_id, i=img: c.itemconfigure(b, image=i))

    def _draw_badge(self, c: tk.Canvas, x: float, y: float, text: str,
                    color: str, tag: str) -> float:
        """Рисует бейдж, возвращает правую границу — чтобы класть следующий."""
        font = (self.f_ui, 7, "bold")
        wpx = self._text_width(text, font) + 12
        img = rounded_png(int(wpx), 16, 5, SURFACE_3, SURFACE_2)
        self._keep.append(img)
        c.create_image(x, y - 8, image=img, anchor="nw", tags=tag)
        tid = c.create_text(x + wpx / 2, y, text=text, fill=color, font=font,
                            tags=tag)
        box = c.bbox(tid)
        if box:
            c.move(tid, 0, y - (box[1] + box[3]) / 2)
        return x + wpx

    def _text_width(self, text: str, font) -> int:
        import tkinter.font as tkfont
        key = (text, font)
        if key not in self._tw_cache:
            self._tw_cache[key] = tkfont.Font(font=font).measure(text)
        return self._tw_cache[key]

    # ---------- жизненный цикл ----------

    def run(self) -> None:
        """Блокирует главный поток."""
        self.root.mainloop()

    def stop(self) -> None:
        try:
            from . import config
            config.save(self.cfg)          # запомнить позицию окна
        except Exception:
            pass
        try:
            self.hotkeys_stop = True
            self.root.destroy()      # quit() убил бы общий mainloop Suite
        except Exception:
            pass
