"""Оверлей поверх iRacing для одномониторной конфигурации.

Три свойства, без которых оверлей поверх сима бесполезен:

1. `-topmost` — рисуется поверх iRacing. Требует, чтобы сим был в режиме
   **borderless windowed**: в exclusive fullscreen поверх окна не рисуется ничего.
2. `WS_EX_NOACTIVATE` — окно НЕ забирает фокус при клике. Иначе каждый клик по
   оверлею отбирал бы ввод у сима, а чат-команды (когда дойдём до флагов) уходили
   бы не туда.
3. Окно — ровно панель, без пустых зон: мимо неё мышь и так попадает в
   iRacing. (Цвет-ключ `-transparentcolor` нужен окнам графики эфира — им
   общие помощники ниже.)

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

FONT_FALLBACK = "Segoe UI"

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xC0DE
CAM_KEEP = "как у меня"        # не менять вид оператора, сменить только машину

user32 = ctypes.windll.user32


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


def load_logo(size: int = 22, bg: str = "#141923"):
    """Логотип iRC из ассетов.

    Знак горизонтальный, поэтому вписываем по ВЫСОТЕ: квадратный ресайз
    сплющил бы его. Ищем и рядом с бандлом, и во временной распаковке
    PyInstaller, и рядом с exe.
    """
    import sys
    from pathlib import Path
    names = ("logo-irc.png", "logo.png", "icon.png", "icon.ico")
    # ассеты лежат в пакете suite, а модуль — на два уровня глубже
    # (suite/modules/racecontrol/overlay.py), поэтому parents[2]
    bases = (Path(__file__).resolve().parents[2] / "assets",
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
                # вписываем по высоте, ширина — сколько получится
                scale = size / img.height
                img = img.resize((max(1, int(img.width * scale)), size),
                                 Image.LANCZOS)
                flat = Image.new("RGBA", img.size, bg)
                flat.alpha_composite(img)
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


# ---- язык «Тайминг» (тот же, что web/ui/irc.css) ----
# Скруглений нет, рамка 1 px, высоты 32/40, строка ленты 36. Шрифты — Exo 2
# (интерфейс) и Roboto Mono (цифры) из web/ui/fonts, подключаются процессу.
T_BG = "#141923"          # --panel (меню и карточки irchamps.gg)
T_RAISE = "#1B2230"       # --raise
T_HOVER = "#232C3D"       # --hover
T_LINE = "#28303F"        # --line
T_LINE2 = "#3A4456"       # --line2
T_FG = "#F2F2F4"
T_DIM = "#8E8E98"
T_FAINT = "#5C5C66"
T_ACC = "#FF5A1F"
T_ON = "#00C566"
T_AIR = "#FF2D2D"
T_AIR_DARK = "#7A0C0C"
T_WARN = "#FFB800"
T_BEST = "#B46CFF"
T_OFF_TRACK = "#4FB6E8"
T_HIT_TEXT = "#FF6B6B"

T_MODE = {MODE_LIVE: T_AIR, MODE_REPLAY: T_WARN, MODE_OFFLINE: T_FAINT}
PEN_LABEL = {"drive_through": "Проезд", "time_penalty": "Штраф", "eol": "В конец колонны",
             "dq": "Дисквалификация"}


def register_ui_fonts() -> None:
    """Exo 2 и Roboto Mono из web/ui/fonts — только этому процессу (FR_PRIVATE)."""
    import sys
    from pathlib import Path
    bases = (Path(__file__).resolve().parents[2] / "web" / "ui" / "fonts",
             Path(getattr(sys, "_MEIPASS", ".")) / "suite" / "web" / "ui" / "fonts")
    for base in bases:
        if base.is_dir():
            for f in base.glob("*.ttf"):
                try:
                    ctypes.windll.gdi32.AddFontResourceExW(str(f), 0x10, 0)
                except Exception:
                    pass
            return


def surname(name: str) -> str:
    p = str(name or "").split()
    if not p:
        return ""
    t = p[-1]
    if len(t) <= 2 and len(p) > 1:          # инициал в конце
        t = p[0]
    return t.upper()


class Key(tk.Label):
    """Клавиша набора: плоская, рамка 1 px, высота 32 (малая) или 40.

    Обычный tk.Button на Windows рисуется системной кнопкой и не принимает
    наш вид, поэтому клавиша — это Label с привязкой клика.
    """

    STYLE = {  # вид: фон, текст, рамка
        "": (T_RAISE, T_FG, T_LINE2), "pri": (T_ACC, "#000000", T_ACC),
        "air": (T_AIR, "#FFFFFF", T_AIR), "sel": (T_FG, "#000000", T_FG),
        "warn": (T_RAISE, T_WARN, T_WARN), "hit": (T_RAISE, T_HIT_TEXT, T_AIR),
        "go": (T_RAISE, T_ON, T_ON), "arm": (T_AIR, "#FFFFFF", T_AIR),
        "off": (T_RAISE, T_FAINT, T_LINE2),
    }

    def __init__(self, parent, text, command, font, kind: str = "", large: bool = False,
                 padx: int = 12):
        super().__init__(parent, text=text.upper(), font=font, cursor="hand2",
                         padx=padx, pady=12 if large else 8, bd=0,
                         highlightthickness=1)
        self.command = command
        self.kind = ""
        self.set_kind(kind)
        self.bind("<Button-1>", lambda e: self.command and self.kind != "off" and self.command())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        bg, fg, line = self.STYLE.get(kind, self.STYLE[""])
        self.configure(bg=bg, fg=fg, highlightbackground=line, highlightcolor=line)

    def _hover(self, on: bool) -> None:
        if self.kind in ("", "warn", "hit", "go"):
            self.configure(bg=T_HOVER if on else T_RAISE)


class FlatSelect(tk.Label):
    """Выбор из списка: поле со стрелкой, список всплывает вверх."""

    def __init__(self, parent, font, on_pick, width_chars: int = 18):
        super().__init__(parent, text="", font=font, bg="#0B0E14", fg=T_FG, anchor="w",
                         padx=10, pady=6, width=width_chars, cursor="hand2",
                         highlightthickness=1, highlightbackground=T_LINE2, bd=0)
        self.font, self.on_pick = font, on_pick
        self.values: list[str] = []
        self.value = ""
        self._popup: tk.Toplevel | None = None
        self.bind("<Button-1>", lambda e: self.close() if self._popup else self.open())

    def set_values(self, values: list[str], current: str) -> None:
        self.values = values
        self.set(current)

    def set(self, value: str) -> None:
        self.value = value
        self.configure(text=f"{value}  ▾")

    def close(self) -> None:
        if self._popup:
            self._popup.destroy()
            self._popup = None

    def open(self) -> None:
        if not self.values:
            return
        pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=T_LINE2)
        box = tk.Frame(pop, bg=T_RAISE)
        box.pack(padx=1, pady=1)
        for v in self.values:
            it = tk.Label(box, text=v, font=self.font, anchor="w", padx=10, pady=6,
                          bg=T_RAISE, fg=T_ACC if v == self.value else T_FG, cursor="hand2")
            it.pack(fill="x")
            it.bind("<Enter>", lambda e, w=it: w.configure(bg=T_HOVER))
            it.bind("<Leave>", lambda e, w=it: w.configure(bg=T_RAISE))
            it.bind("<Button-1>", lambda e, v=v: (self.close(), self.on_pick(v)))
        pop.update_idletasks()
        w = max(self.winfo_width(), pop.winfo_reqwidth())
        h = pop.winfo_reqheight()
        # список раскрываем ВВЕРХ: поле стоит у нижнего края окна
        pop.geometry(f"{w}x{h}+{self.winfo_rootx()}+{self.winfo_rooty() - h - 2}")
        self._popup = pop
        _apply_no_activate(pop)


class Overlay:
    """Рейс-контроль поверх сима: лента, решение по инциденту, флаги.

    Тот же порядок работы, что во вкладке главного окна: выбрать инцидент,
    посмотреть повтор, выбрать исход. Клавиатура сюда не доходит — окно не
    забирает фокус у сима, — поэтому всё мышью.
    """

    WIDTH = 432
    ROWS = 7                    # строк ленты в окне
    ROW_H = 36

    def __init__(self, app, host_root=None):
        self.app = app
        self.cfg = app.cfg
        self.visible = True
        self._hotkeys: queue.Queue = queue.Queue()
        self._incs: list[dict] = []
        self._list_sig = None
        self._dec_sig = None
        self._drag = (0, 0)
        self.sel = 0                               # выбранный инцидент
        self.pick = {"outcome": "", "car": "", "armed": ""}
        self.edit = 0
        self._clear_arm = False
        self._blink_on = False
        self._cam_names: list[str] = []

        register_ui_fonts()           # до создания Tk: иначе список семейств уже кэширован
        from ...core.ui import host
        parent = host_root if host_root is not None else (
            host.root() if host.is_running() else None)
        self.root = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.root.withdraw()
        ui = pick_font("Exo 2", "Segoe UI")
        heavy = pick_font("Exo 2 ExtraBold", "Exo 2", "Segoe UI")
        mono = pick_font("Roboto Mono", "Consolas")
        # размеры в пикселях (минус): тогда tk scaling не раздувает окно
        self.f_ui = (ui, -13)
        self.f_key = (ui, -11, "bold")
        self.f_head = (heavy, -11)
        self.f_name = (ui, -13, "bold")
        self.f_tag = (ui, -10, "bold")
        self.f_num = (mono, -12)
        self.f_chip = (mono, -12, "bold")

        self.root.title("iRC Race Control")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(self.cfg.get("overlay_alpha", 0.94)))
        self.root.configure(bg=T_LINE2)
        x = int(self.cfg.get("overlay_x", 24))
        y = int(self.cfg.get("overlay_y", 24))
        self.root.geometry(f"{self.WIDTH}x600+{x}+{y}")

        self._build()
        self.root.deiconify()
        self._fit_height()
        _apply_no_activate(self.root)

        self.hotkeys = HotkeyListener(self._hotkeys, self.cfg.get("overlay_hotkeys") or ["ctrl+alt+r"])
        self.hotkeys.start()
        self.root.after(300, self._show_hotkey)
        self._tick()

    # ---------- построение ----------

    def _build(self) -> None:
        # рамка 1 px — это фон корня, панель лежит внутри с отступом 1
        self.panel = tk.Frame(self.root, bg=T_BG)
        self.panel.pack(fill="both", expand=True, padx=1, pady=1)

        # шапка панели: за неё таскаем окно
        head = tk.Frame(self.panel, bg=T_BG, height=36, cursor="fleur")
        head.pack(fill="x")
        head.pack_propagate(False)
        # логотип iRC; если ассет не нашёлся — акцентная метка, как у панелей
        self._logo_img = load_logo(18, T_BG)
        if self._logo_img is not None:
            logo = tk.Label(head, image=self._logo_img, bg=T_BG, bd=0)
            logo.pack(side="left", padx=(12, 10))
            logo.bind("<Button-1>", self._drag_start)
            logo.bind("<B1-Motion>", self._drag_move)
        else:
            tk.Frame(head, bg=T_ACC, width=3, height=12).pack(side="left", padx=(12, 8))
        title = tk.Label(head, text="РЕЙС-КОНТРОЛЬ", font=self.f_head, bg=T_BG, fg=T_FG)
        title.pack(side="left")
        self.lbl_mode = tk.Label(head, text="● OFFLINE", font=self.f_tag, bg=T_BG, fg=T_FAINT)
        self.lbl_mode.pack(side="left", padx=(10, 0))
        close = tk.Label(head, text="✕", font=self.f_ui, bg=T_BG, fg=T_DIM, padx=12, cursor="hand2")
        close.pack(side="right", fill="y")
        close.bind("<Button-1>", lambda e: self.hide())
        close.bind("<Enter>", lambda e: close.configure(bg=T_HOVER, fg=T_FG))
        close.bind("<Leave>", lambda e: close.configure(bg=T_BG, fg=T_DIM))
        self.lbl_meta = tk.Label(head, text="", font=self.f_num, bg=T_BG, fg=T_DIM)
        self.lbl_meta.pack(side="right")
        for w in (head, title, self.lbl_mode, self.lbl_meta):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        self._sep(self.panel)
        self._head = head

        # баннер повтора — пакуется только в REPLAY
        self.banner = tk.Label(self.panel, font=self.f_tag, bg="#1A1505", fg=T_WARN,
                               anchor="w", padx=12, pady=6)

        # лента: фиксированная высота на ROWS строк
        self.list = tk.Frame(self.panel, bg=T_BG, height=self.ROWS * self.ROW_H)
        self.list.pack(fill="x")
        self.list.pack_propagate(False)
        self._sep(self.panel)

        # решение по выбранному инциденту
        self.dec = tk.Frame(self.panel, bg=T_BG)
        self.dec.pack(fill="x")
        self._sep(self.panel)

        # флаги сессии
        flags = tk.Frame(self.panel, bg=T_BG)
        flags.pack(fill="x", padx=12, pady=(12, 8))
        Key(flags, "Жёлтый", lambda: self._flag("yellow"), self.f_key, "warn").pack(side="left")
        tk.Label(flags, text="КРУГИ", font=self.f_tag, bg=T_BG, fg=T_FAINT).pack(side="left", padx=(12, 6))
        Key(flags, "−", lambda: self._flag("pace_laps", delta=-1), self.f_key, padx=10).pack(side="left")
        self.lbl_pace = tk.Label(flags, text="2", font=(self.f_num[0], -15), bg=T_BG, fg=T_FG, width=3)
        self.lbl_pace.pack(side="left")
        Key(flags, "+", lambda: self._flag("pace_laps", delta=1), self.f_key, padx=10).pack(side="left")
        self.btn_clear = Key(flags, "Снять все ЧФ", self._clear_all, self.f_key)
        self.btn_clear.pack(side="right")

        # повтор и камеры
        bar = tk.Frame(self.panel, bg=T_BG)
        bar.pack(fill="x", padx=12, pady=(0, 8))
        Key(bar, "К LIVE", self._to_live, self.f_key, "pri").pack(side="left")
        Key(bar, "‹ iR", lambda: self._cmd("jump_sim_incident", forward=False), self.f_key, padx=10).pack(side="left", padx=(8, 0))
        Key(bar, "iR ›", lambda: self._cmd("jump_sim_incident", forward=True), self.f_key, padx=10).pack(side="left", padx=(4, 0))
        Key(bar, "Камера", lambda: self._cmd("cam_at_incident"), self.f_key).pack(side="left", padx=(8, 0))

        cam = tk.Frame(self.panel, bg=T_BG)
        cam.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(cam, text="КАМЕРА ИНЦИДЕНТА", font=self.f_tag, bg=T_BG, fg=T_FAINT).pack(side="left")
        self.cam_menu = FlatSelect(cam, self.f_ui, self._pick_camera, width_chars=16)
        self.cam_menu.pack(side="left", padx=(10, 0))
        self.cam_menu.set(CAM_KEEP)
        self.lbl_hotkey = tk.Label(cam, text="", font=self.f_num, bg=T_BG, fg=T_FAINT)
        self.lbl_hotkey.pack(side="right")

        # строка уведомлений
        self._sep(self.panel)
        self.toast = tk.Label(self.panel, text="", font=self.f_ui, bg=T_BG, fg=T_DIM,
                              anchor="w", padx=12, pady=6)
        self.toast.pack(fill="x")

    @staticmethod
    def _sep(parent) -> None:
        tk.Frame(parent, bg=T_LINE, height=1).pack(fill="x")

    def _fit_height(self) -> None:
        """Высота окна — по содержимому: панель решения меняет размер."""
        try:
            self.root.update_idletasks()
            h = self.panel.winfo_reqheight() + 2
            if self.root.winfo_height() != h:
                self.root.geometry(f"{self.WIDTH}x{h}")
        except Exception:
            pass

    def _show_hotkey(self) -> None:
        """Показать, какая комбинация реально досталась (могла быть занята)."""
        if self.hotkeys.registered:
            self.lbl_hotkey.configure(text=self.hotkeys.active.upper())
        else:
            self.lbl_hotkey.configure(text="хоткей занят", fg=T_HIT_TEXT)

    # ---------- лента ----------

    def _current(self) -> dict | None:
        return next((i for i in self._incs if i["id"] == self.sel), None)

    def _keep_current(self) -> None:
        if self._current() or not self._incs:
            return
        nxt = next((i for i in self._incs if not i.get("decision")), self._incs[0])
        self.sel, self.edit = nxt["id"], 0
        self._reset_pick()

    def _render_list(self) -> None:
        sig = (self.sel, tuple((i["id"], i.get("decision"), i.get("synced")) for i in self._incs))
        if sig == self._list_sig:
            return
        self._list_sig = sig
        for w in self.list.winfo_children():
            w.destroy()
        if not self._incs:
            st = self.app.state()
            if not st["connected"]:
                text = "iRacing не найден"
            elif not st.get("results_available"):
                text = ("Сессия не отдаёт счётчики инцидентов —\nловим выезды по всему полю"
                        if st.get("spectator") else
                        f"Видны только свои инциденты\nмой счётчик: {st.get('player_incidents', 0)}x")
            else:
                text = "Инцидентов пока нет"
            tk.Label(self.list, text=text, font=self.f_ui, bg=T_BG, fg=T_DIM,
                     justify="left", anchor="nw", padx=12, pady=12).pack(fill="both", expand=True)
            return
        for inc in self._incs[:self.ROWS]:
            self._row(inc)

    def _row(self, inc: dict) -> None:
        cur = inc["id"] == self.sel
        bg = T_HOVER if cur else T_BG
        row = tk.Frame(self.list, bg=bg, height=self.ROW_H - 1, cursor="hand2")
        row.pack(fill="x")
        row.pack_propagate(False)
        tk.Frame(self.list, bg=T_LINE, height=1).pack(fill="x")
        dec = inc.get("decision") or ""
        bar = T_ACC if cur else (T_BG if dec else T_AIR)
        tk.Frame(row, bg=bar, width=3).pack(side="left", fill="y")
        chip = tk.Label(row, text=inc["car_number"], font=self.f_chip, bg="#E8E8EC", fg="#000000",
                        width=4)
        chip.pack(side="left", padx=(9, 10), pady=8)
        name = tk.Label(row, text=surname(inc["driver_name"])[:14], font=self.f_name, bg=bg,
                        fg=T_DIM if dec else T_FG)
        name.pack(side="left")
        off = inc.get("kind") == "offtrack"
        what = f"вне трассы {inc.get('duration') or 0:.0f} с" if off else f"+{inc['inc_delta']}x"
        tag = tk.Label(row, text=what.upper(), font=self.f_tag, bg=bg,
                       fg=T_OFF_TRACK if off else T_HIT_TEXT)
        tag.pack(side="left", padx=(8, 0))
        text, color = self._dec_tag(inc)
        right = tk.Label(row, text=text, font=self.f_tag, bg=bg, fg=color, padx=12)
        right.pack(side="right")
        widgets = (row, name, tag, right)
        for w in widgets + (chip,):
            w.bind("<Button-1>", lambda e, i=inc["id"]: self._select(i))
            w.bind("<Double-Button-1>", lambda e, i=inc["id"]: self._show_incident(i))
        if not cur:
            for w in widgets:
                w.bind("<Enter>", lambda e, ws=widgets: [x.configure(bg=T_RAISE) for x in ws])
                w.bind("<Leave>", lambda e, ws=widgets: [x.configure(bg=T_BG) for x in ws])

    @staticmethod
    def _dec_tag(inc: dict) -> tuple[str, str]:
        dec = inc.get("decision") or ""
        # ✓ — решение в Судействе на сайте, ○ — ждёт отправки
        sync = (" ✓" if inc.get("synced") else " ○") if dec else ""
        if dec == "penalty":
            d = inc.get("decision_detail") or {}
            return (f"{PEN_LABEL.get(d.get('penalty'), 'Наказание')}{' ' + str(d['seconds']) + ' с' if d.get('seconds') else ''}"
                    f" · #{d.get('car', '')}{sync}").upper(), T_HIT_TEXT
        if dec == "racing":
            return "ГОНОЧНЫЙ" + sync, T_DIM
        if dec == "review":
            return "ПОСЛЕ ГОНКИ" + sync, T_WARN
        return "НОВЫЙ", T_ACC

    def _select(self, inc_id: int) -> None:
        if inc_id == self.sel:
            return
        self.sel, self.edit = inc_id, 0
        self._reset_pick()
        self._render()

    # ---------- панель решения ----------

    def _render_decision(self) -> None:
        inc = self._current()
        sig = (self.sel, bool(self._incs), inc and inc.get("decision"), inc and inc.get("synced"),
               self.edit, tuple(self.pick.values()), int(self.cfg.get("penalty_seconds", 5)))
        if sig == self._dec_sig:
            return
        self._dec_sig = sig
        for w in self.dec.winfo_children():
            w.destroy()
        box = tk.Frame(self.dec, bg=T_BG)
        box.pack(fill="x", padx=12, pady=12)
        if not inc:
            tk.Label(box, text=("Выберите инцидент в ленте. Двойной клик — повтор." if self._incs
                                else "Решение по инциденту — здесь, когда он появится."),
                     font=self.f_ui, bg=T_BG, fg=T_DIM).pack(anchor="w")
            self._fit_height()
            return
        top = tk.Frame(box, bg=T_BG)
        top.pack(fill="x")
        t = int(max(0, inc["session_time"]))
        clock = "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)
        near = ", ".join("#" + n for n in inc.get("nearby") or []) or "никого"
        info = tk.Frame(top, bg=T_BG)
        info.pack(side="left", fill="x", expand=True)
        tk.Label(info, text=f"#{inc['car_number']} {surname(inc['driver_name'])}", font=self.f_name,
                 bg=T_BG, fg=T_FG, anchor="w").pack(anchor="w")
        tk.Label(info, text=f"круг {inc['lap']} · {clock} · рядом {near}"[:48], font=self.f_num,
                 bg=T_BG, fg=T_DIM, anchor="w").pack(anchor="w")
        Key(top, "▶ Повтор", lambda: self._show_incident(inc["id"]), self.f_key, "pri").pack(side="right")

        dec = inc.get("decision") or ""
        if dec and self.edit != inc["id"]:
            text, color = self._dec_tag(inc)
            row = tk.Frame(box, bg=T_BG)
            row.pack(fill="x", pady=(10, 0))
            tk.Label(row, text=text, font=self.f_tag, bg=T_BG, fg=color).pack(side="left")
            if dec == "penalty":
                tk.Label(row, text="отменить — только в симе", font=self.f_ui, bg=T_BG,
                         fg=T_FAINT).pack(side="right")
            else:
                Key(row, "Изменить", self._edit, self.f_key).pack(side="right")
            self._fit_height()
            return

        o = self.pick["outcome"]
        grid = tk.Frame(box, bg=T_BG)
        grid.pack(fill="x", pady=(10, 0))
        for col, (key, label, kind) in enumerate((("racing", "Гоночный", ""), ("penalty", "Наказать", "hit"),
                                                  ("review", "После гонки", "warn"))):
            k = kind
            if o == key:
                k = "air" if key == "penalty" else "sel"
            Key(grid, label, lambda x=key: self._outcome(x), self.f_key, k, large=True).grid(
                row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
            grid.columnconfigure(col, weight=1, uniform="oc")

        if o == "penalty":
            secs = int(self.cfg.get("penalty_seconds", 5))
            cars = [inc["car_number"]] + [c for c in (inc.get("nearby") or []) if c != inc["car_number"]]
            tk.Label(box, text="КОГО", font=self.f_tag, bg=T_BG, fg=T_FAINT).pack(anchor="w", pady=(12, 4))
            who = tk.Frame(box, bg=T_BG)
            who.pack(fill="x")
            for c in cars[:6]:
                Key(who, f"#{c}", lambda c=c: self._pick_car(c), self.f_key,
                    "sel" if self.pick["car"] == c else "", padx=10).pack(side="left", padx=(0, 6))
            tk.Label(box, text="ЧТО · ВТОРОЙ КЛИК ПОДТВЕРЖДАЕТ", font=self.f_tag, bg=T_BG,
                     fg=T_FAINT).pack(anchor="w", pady=(12, 4))
            pens = tk.Frame(box, bg=T_BG)
            pens.pack(fill="x")
            self._armed_key = None
            for n, (pen, label) in enumerate(PEN_LABEL.items()):
                text = label + (f" {secs} с" if pen == "time_penalty" else "")
                armed = self.pick["armed"] == pen
                k = Key(pens, ("Подтвердить: " if armed else "") + text, lambda p=pen: self._penalty(p),
                        self.f_key, "arm" if armed else "hit")
                k.grid(row=n // 2, column=n % 2, sticky="ew", padx=(0 if n % 2 == 0 else 8, 0),
                       pady=(0 if n < 2 else 8, 0))
                pens.columnconfigure(n % 2, weight=1, uniform="pen")
                if armed:
                    self._armed_key = k
            if self._armed_key:
                self._blink()
        if self.edit == inc["id"]:
            Key(box, "Не менять", self._cancel_edit, self.f_key).pack(anchor="w", pady=(10, 0))
        self._fit_height()

    def _blink(self) -> None:
        """Мигает клавиша, которая ждёт подтверждения."""
        k = getattr(self, "_armed_key", None)
        if not k or not k.winfo_exists():
            return
        self._blink_on = not self._blink_on
        k.configure(bg=T_AIR_DARK if self._blink_on else T_AIR)
        self.root.after(350, self._blink)

    def _reset_pick(self) -> None:
        self.pick = {"outcome": "", "car": "", "armed": ""}

    def _render(self) -> None:
        self._keep_current()
        self._render_list()
        self._render_decision()

    # ---------- действия ----------

    def _outcome(self, key: str) -> None:
        inc = self._current()
        if not inc:
            return
        if key == "racing":
            self._decide(inc, {"outcome": "racing"})
            return
        if key == "review":
            # заметку маршалу можно дописать в главном окне; здесь — сразу
            self._decide(inc, {"outcome": "review"})
            return
        self.pick = {"outcome": "" if self.pick["outcome"] == key else key,
                     "car": inc["car_number"], "armed": ""}
        self._render()

    def _pick_car(self, car: str) -> None:
        self.pick.update(car=car, armed="")
        self._render()

    def _penalty(self, pen: str) -> None:
        inc = self._current()
        if not inc:
            return
        if self.pick["armed"] != pen:
            # наказание в живой гонке не отменить — второй клик подтверждает
            self.pick["armed"] = pen
            self._render()
            self.root.after(3500, lambda: self._disarm(pen))
            return
        self._decide(inc, {"outcome": "penalty", "penalty": pen, "car": self.pick["car"] or inc["car_number"],
                           "seconds": int(self.cfg.get("penalty_seconds", 5))})

    def _disarm(self, pen: str) -> None:
        if self.pick["armed"] == pen:
            self.pick["armed"] = ""
            self._render()

    def _edit(self) -> None:
        self.edit = self.sel
        self._reset_pick()
        self._render()

    def _cancel_edit(self) -> None:
        self.edit = 0
        self._reset_pick()
        self._render()

    def _decide(self, inc: dict, b: dict) -> None:
        r = self.app.decide(inc["id"], b["outcome"], b.get("penalty", ""),
                            int(b.get("seconds") or 0), b.get("car", ""), "")
        if r.get("ok"):
            text = {"racing": f"#{inc['car_number']}: гоночный инцидент",
                    "review": f"#{inc['car_number']}: маршалу после гонки",
                    "penalty": f"{PEN_LABEL.get(b.get('penalty'), '')}"
                               f"{' ' + str(b['seconds']) + ' с' if b.get('penalty') == 'time_penalty' else ''}"
                               f" · #{b.get('car')} — выдано"}[b["outcome"]]
            self._notify(text, T_ON if b["outcome"] == "racing" else T_WARN if b["outcome"] == "review" else T_HIT_TEXT)
            self.edit = 0
            self._reset_pick()
            self._incs = self.app.incidents()[:self.ROWS]
            nxt = next((i for i in self._incs if not i.get("decision")), None)
            if nxt:
                self.sel = nxt["id"]
        else:
            self._notify(r.get("message", "не выполнено"), T_HIT_TEXT)
        self._render()

    def _notify(self, text: str, color: str = T_DIM) -> None:
        self.toast.configure(text=text, fg=color)
        stamp = text
        self.root.after(4000, lambda: self.toast.cget("text") == stamp and self.toast.configure(text=""))

    def _to_live(self) -> None:
        r = self.app.resume_live()
        self._notify("Вернулись в прямой эфир" if r.get("ok") else r.get("message", "не выполнено"),
                     T_DIM if r.get("ok") else T_HIT_TEXT)

    def _cmd(self, name: str, forward: bool = True) -> None:
        r = self.app.command(name, {"forward": forward})
        if not r.get("ok"):
            self._notify(r.get("message", "iRacing не запущен или сессия не активна"), T_HIT_TEXT)

    def _flag(self, action: str, **params) -> None:
        """Команды сессии: жёлтый, круги под жёлтым, снять все чёрные флаги."""
        r = self.app.flag_action(action, params)
        if r.get("ok"):
            if "pace_laps" in r:
                self.lbl_pace.configure(text=str(r["pace_laps"]))
            self._notify({"yellow": f"Жёлтый флаг · кругов за пейс-каром: {self.lbl_pace.cget('text')}",
                          "pace_laps": f"Кругов под жёлтым: {r.get('pace_laps')}",
                          "clear_all": "Чёрные флаги сняты со всех"}.get(action, "Команда отправлена"),
                         T_WARN if action == "yellow" else T_ON)
        else:
            self._notify(r.get("message", "не выполнено"), T_HIT_TEXT)

    def _clear_all(self) -> None:
        """Снять чёрные флаги со всего поля — через второй клик."""
        if not self._clear_arm:
            self._clear_arm = True
            self.btn_clear.configure(text="ЕЩЁ РАЗ — СНЯТЬ СО ВСЕХ")
            self.btn_clear.set_kind("arm")
            self.root.after(4000, self._clear_disarm)
            return
        self._clear_disarm()
        self._flag("clear_all")

    def _clear_disarm(self) -> None:
        self._clear_arm = False
        self.btn_clear.configure(text="СНЯТЬ ВСЕ ЧФ")
        self.btn_clear.set_kind("")

    def _show_incident(self, inc_id: int) -> None:
        r = self.app.show_incident(inc_id)
        self._notify("Повтор открыт" if r.get("ok") else r.get("message", "не выполнено"),
                     T_DIM if r.get("ok") else T_HIT_TEXT)

    # ---------- камера инцидента ----------

    def _refresh_cameras(self) -> None:
        """Группы камер приходят из сессии — пересобираем при её смене."""
        data = self.app.cameras()
        names = [CAM_KEEP] + [g["name"] for g in data["groups"]]
        if names == self._cam_names:
            return
        self._cam_names = names
        current = data["current"] or CAM_KEEP
        self.cam_menu.set_values(names, current if current in names else CAM_KEEP)

    def _pick_camera(self, name: str) -> None:
        r = self.app.set_camera("" if name == CAM_KEEP else name)
        if r.get("ok"):
            self.cam_menu.set(name)
            self._notify(f"Камера инцидента: {name}")
        else:
            self._notify(r.get("message", "не удалось"), T_HIT_TEXT)

    # ---------- перетаскивание ----------

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")
        self.cfg["overlay_x"], self.cfg["overlay_y"] = x, y

    # ---------- показ/скрытие ----------

    def request_toggle(self) -> None:
        """Потокобезопасная точка входа — из трея или любого другого потока."""
        self._hotkeys.put("toggle")

    def request_visible(self, visible: bool) -> None:
        """Показать или скрыть из чужого потока (HTTP, трей)."""
        self._hotkeys.put("show" if visible else "hide")

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def show(self) -> None:
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        _apply_no_activate(self.root)

    def hide(self) -> None:
        self.visible = False
        self.cam_menu.close()
        self.root.withdraw()

    # ---------- обновление ----------

    def _tick(self) -> None:
        try:
            while True:
                cmd = self._hotkeys.get_nowait()
                if cmd == "toggle":
                    self.toggle()
                elif cmd == "show":
                    self.show()
                elif cmd == "hide":
                    self.hide()
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
        self.lbl_mode.configure(text=f"● {mode}", fg=T_MODE.get(mode, T_FAINT))
        if st["connected"]:
            t = int(st["session_time"])
            meta = f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}" + (" ≈" if st["session_time_estimated"] else "")
            # в тест-драйве поле не видно: без пометки пустая лента выглядит поломкой
            if not st.get("results_available"):
                meta = ("выезды · " if st.get("spectator") else "только свои · ") + meta
            self._refresh_cameras()
        else:
            meta = ""
        self.lbl_meta.configure(text=meta + "  ")
        if "pace_laps" in st:
            self.lbl_pace.configure(text=str(st["pace_laps"]))

        if mode == MODE_REPLAY:
            self.banner.configure(text=f"■ ПОВТОР · СОСЕДЕЙ НЕ ВИДНО · НОВЫХ ЗА ЭТО ВРЕМЯ: {st['missed_in_replay']}")
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", after=self._head)
                self._fit_height()
        elif self.banner.winfo_ismapped():
            self.banner.pack_forget()
            self._fit_height()

        self._incs = self.app.incidents()[:self.ROWS]
        self._render()

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
            self.root.destroy()      # quit() убил бы общий mainloop Suite
        except Exception:
            pass
