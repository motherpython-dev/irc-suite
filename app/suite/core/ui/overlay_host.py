"""Хост эфирных оверлеев: прозрачные окна поверх iRacing.

Один процесс (`iRC.exe --overlays <адрес>`) держит окна всех оверлеев.
Картинку рисует `ov_paint` по тому же JSON-описанию, что render.js для OBS и
редактора, а выводит её слоистое окно Windows с попиксельной прозрачностью
(UpdateLayeredWindow): вокруг плашек — настоящая пустота, в ней виден сим, и
клики по пустоте уходят в него. Края и тень сглажены, без каймы цвета-ключа.

Почему не WebView2, как было: окно WebView2 не бывает прозрачным до рабочего
стола — ни флаг transparent, ни цвет-ключ, ни «стекло» DWM (проверено
пиксельным замером), под оверлеем оставалась серая подложка.

Свойства окна:
  * WS_EX_NOACTIVATE и MA_NOACTIVATE — ни показ, ни клик, ни перетаскивание
    не забирают фокус у iRacing: горячие клавиши повтора и камер работают;
  * WS_EX_TOOLWINDOW — окон нет в панели задач и в Alt+Tab;
  * поверх всех (TOPMOST); iRacing должен быть в оконном режиме без рамки.
Окно таскается за любую непрозрачную точку, место сохраняется на сервере.

Показом управляют данные: в ответе источника оверлея есть visible. Появление
и уход анимированы так же, как на странице: сдвиг и проявление.

Окна пилота (Standings, Relative, радар, ветер, двигатель) живут здесь же,
под именами «p:<id>»: данные — `/api/pilot/data`, картинка — `pilot_paint`.
Радар и ветер опрашиваются 60 раз в секунду, двигатель — 20, таблицы — 10.
"""
from __future__ import annotations

import ctypes
import json
import logging
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes

from .ov_paint import PAD, Painter, bezier, to_bgra
from .pilot_paint import PilotPainter
from .fx_paint import FxPainter, FxSprites

log = logging.getLogger("ui.overlay_host")

TITLE = "iRC overlay {}"
CLASS_NAME = "iRCOverlay"

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x8, 0x08000000
SW_HIDE, SW_SHOWNA = 0, 8
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
ULW_ALPHA, AC_SRC_ALPHA = 2, 1
WM_CLOSE, WM_SETCURSOR, WM_MOUSEACTIVATE, WM_NCHITTEST = 0x10, 0x20, 0x21, 0x84
WM_CONTEXTMENU, WM_NCLBUTTONDBLCLK, WM_NCRBUTTONDOWN, WM_NCRBUTTONUP = 0x7B, 0xA3, 0xA4, 0xA5
WM_TIMER, WM_SYSCOMMAND, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE = 0x113, 0x112, 0x231, 0x232
WM_DPICHANGED = 0x2E0
WM_APP_FRAME = 0x8001         # поток отрисовки: «готов кадр» — показать сразу, не ждать таймера
HTCAPTION, MA_NOACTIVATE, SC_MOVE = 2, 3, 0xF010
GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x20
# привязка окна: доля ширины и высоты от левого верхнего угла картинки до точки,
# которая стоит на месте, когда окно меняет размер
ANCHOR = {"tl": (0, 0), "tr": (1, 0), "bl": (0, 1), "br": (1, 1), "bc": (.5, 1), "tc": (.5, 0)}
IDC_SIZEALL = 32646

ENTER_SHIFT = 26              # на сколько выезжает окно при появлении, px
MOVE_S, FADE_S, HIDE_S = 0.26, 0.22, 0.32
MOVE_EASE = bezier(.2, .8, .2, 1)
FADE_EASE = bezier(.25, .1, .25, 1)
DATA_EVERY, SPEC_EVERY = 0.4, 2.0
ANIM_FPS = 60                 # кадров в секунду, пока что-то движется
PRESENT_MS = 10               # как часто окна выводят готовый кадр (таймер не точнее 10 мс)
SERVER_GONE_S = 20            # сервер молчит дольше — программа закрыта, уходим
LAYOUT_EVERY = 5.0            # как часто смотреть, не появились ли новые окна
PILOT = "p:"                  # приставка окон пилота
FX_SCREEN, FX_BUG = "replayfx", "replaybug"   # шторка повтора на весь экран и метка «ПОВТОР»
FX_POLL = 0.05                # шторка: опрос фазы, с (анимация идёт по своим часам)
PILOT_EVERY = {"radar": 1 / 60, "engine": 0.05, "wind": 1 / 60, "standings": 0.1, "relative": 0.1, "setups": 0.25}
# окно скрыто — опрос реже: радар должен появиться без заметной задержки
PILOT_HIDDEN = {"radar": 0.05}

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


u32 = ctypes.WinDLL("user32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32")
k32 = ctypes.WinDLL("kernel32")

u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
u32.RegisterClassExW.restype = wintypes.ATOM
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                wintypes.HINSTANCE, wintypes.LPVOID]
u32.CreateWindowExW.restype = wintypes.HWND
u32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                    ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                    ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
u32.UpdateLayeredWindow.restype = wintypes.BOOL
u32.GetDC.argtypes = [wintypes.HWND]
u32.GetDC.restype = wintypes.HDC
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
u32.SetWindowLongW.restype = ctypes.c_long
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                            wintypes.UINT]
u32.GetMessageW.restype = ctypes.c_int
u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.restype = LRESULT
u32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
u32.SetTimer.restype = ctypes.c_size_t
u32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
u32.LoadCursorW.restype = wintypes.HANDLE
u32.SetCursor.argtypes = [wintypes.HANDLE]
u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
u32.MonitorFromPoint.restype = wintypes.HANDLE
u32.PostQuitMessage.argtypes = [ctypes.c_int]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi.CreateCompatibleDC.restype = wintypes.HDC
gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                 ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi.CreateDIBSection.restype = wintypes.HBITMAP
gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi.SelectObject.restype = wintypes.HGDIOBJ
gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
u32.DestroyWindow.argtypes = [wintypes.HWND]
u32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
gdi.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
gdi.CreateEllipticRgn.restype = wintypes.HANDLE
gdi.CombineRgn.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.c_int]
gdi.DeleteDC.argtypes = [wintypes.HDC]
RGN_DIFF = 4
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetModuleHandleW.restype = wintypes.HMODULE


def _dpi_aware() -> None:
    """Окна рисуем в физических пикселях монитора — иначе Windows растянет
    готовую картинку и она поплывёт."""
    try:
        u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


class Window:
    """Одно окно оверлея: картинка, место на экране, появление и уход."""

    def __init__(self, host: "Host", oid: str, pos: dict) -> None:
        self.host, self.oid = host, oid
        # x, y из настроек — точка привязки (угол из ANCHOR); левый верхний угол
        # окна считается от неё, когда известен размер картинки
        self.anchor = pos.get("anchor") if pos.get("anchor") in ANCHOR else "tl"
        self.apt = [int(pos.get("x", 40)), int(pos.get("y", 120))]
        self.home = list(self.apt)
        self.locked = False
        self.want_opts = None             # (закрепить, привязка) из потока данных
        self.enter = "fade"
        self.hwnd = u32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            CLASS_NAME, TITLE.format(oid), WS_POPUP,
            self.home[0], self.home[1], 1, 1, None, None, host.hinst, None)
        if not self.hwnd:
            raise OSError(f"окно {oid} не создалось: {ctypes.get_last_error()}")
        self._keep_on_screen()
        try:
            self.scale = (u32.GetDpiForWindow(self.hwnd) or 96) / 96.0
        except Exception:
            self.scale = 1.0
        self.lock = threading.Lock()
        self.pending = None               # (bgra, w, h) от потока отрисовки
        self.want = False                 # данные говорят: в эфире
        self.size = (0, 0)
        self.mem_dc = gdi.CreateCompatibleDC(None)
        self.bmp = None
        self.old_bmp = None
        self.shown = False
        self.dragging = False
        self.anim = None                  # (t0, off0, a0, off1, a1)
        self.off, self.alpha = 0.0, 0.0   # текущий сдвиг (0..1 от ENTER_SHIFT) и прозрачность

    def _keep_on_screen(self) -> None:
        """Монитор, на котором окно стояло, могли отключить — ставим на основной."""
        pt = wintypes.POINT(self.home[0] + 20, self.home[1] + 20)
        if not u32.MonitorFromPoint(pt, 0):              # MONITOR_DEFAULTTONULL
            self.home = [40, 120]

    # ---------- из потока отрисовки ----------

    def post(self, frame, want: bool, enter: str) -> None:
        with self.lock:
            if frame is not None:
                self.pending = frame
            self.want = want
            self.enter = enter or "fade"
        if frame is not None:
            # будим поток окон сразу: по таймеру кадр ждал бы до 10 мс, и
            # промежутки между показами прыгали бы 10/20 мс — движение рвётся
            u32.PostMessageW(self.hwnd, WM_APP_FRAME, 0, 0)

    # ---------- в потоке окон ----------

    def _set_bitmap(self, bgra: bytes, w: int, h: int) -> None:
        if (w, h) != self.size and not self.dragging:
            # размер сменился — точка привязки стоит, окно растёт от неё
            fx, fy = ANCHOR[self.anchor]
            self.home = [int(round(self.apt[0] - fx * w)), int(round(self.apt[1] - fy * h))]
        if (w, h) != self.size or not self.bmp:
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0,
                                   0, 0, 0, 0, 0)
            bits = ctypes.c_void_p()
            bmp = gdi.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0,
                                       ctypes.byref(bits), None, 0)
            if not bmp:
                return
            prev = gdi.SelectObject(self.mem_dc, bmp)
            if self.bmp:
                gdi.DeleteObject(self.bmp)
            else:
                self.old_bmp = prev
            self.bmp, self.bits, self.size = bmp, bits, (w, h)
        ctypes.memmove(self.bits, bgra, len(bgra))

    def _present(self) -> None:
        """Вывести картинку с текущими сдвигом и прозрачностью."""
        if not self.bmp:
            return
        w, h = self.size
        shift = int(round(ENTER_SHIFT * self.scale * self.off))
        dx, dy = {"left": (-shift, 0), "right": (shift, 0), "up": (0, shift),
                  "down": (0, -shift)}.get(self.enter, (0, 0))
        # во время перетаскивания окно ведёт система — место не трогаем
        dst = None if self.dragging else wintypes.POINT(self.home[0] + dx, self.home[1] + dy)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(round(255 * self.alpha)))), AC_SRC_ALPHA)
        u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(dst) if dst else None,
                                ctypes.byref(wintypes.SIZE(w, h)), self.mem_dc,
                                ctypes.byref(wintypes.POINT(0, 0)), 0,
                                ctypes.byref(blend), ULW_ALPHA)

    def _animate(self, off1: float, a1: float) -> None:
        self.anim = (time.monotonic(), self.off, self.alpha, off1, a1)

    def tick(self, now: float) -> None:
        if self.want_opts is not None:
            self._apply_opts()
        with self.lock:
            frame, self.pending = self.pending, None
            want = self.want
        dirty = False
        if frame is not None:
            self._set_bitmap(*frame)
            dirty = True
        if want and self.bmp and (not self.shown or (self.anim and self.anim[4] == 0)):
            if not self.shown:
                self.off, self.alpha = 1.0, 0.0
                self._present()                      # сначала пустая картинка, потом показ
                u32.ShowWindow(self.hwnd, SW_SHOWNA)
                u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                self.shown = True
            self._animate(0.0, 1.0)
        elif not want and self.shown and not (self.anim and self.anim[4] == 0):
            self._animate(1.0, 0.0)
        if self.anim:
            t0, off0, a0, off1, a1 = self.anim
            t = now - t0
            self.off = off0 + (off1 - off0) * MOVE_EASE(t / MOVE_S)
            self.alpha = a0 + (a1 - a0) * FADE_EASE(t / FADE_S)
            dirty = True
            if t >= (HIDE_S if a1 == 0 else MOVE_S):
                self.anim = None
                self.off, self.alpha = off1, a1
                if a1 == 0:
                    self._present()
                    # спрятанное окно не должно ловить мышь
                    u32.ShowWindow(self.hwnd, SW_HIDE)
                    self.shown = False
                    return
        if dirty and self.shown:
            self._present()

    def moved(self) -> None:
        """Перетащили — запомнить новое место (без сдвига анимации)."""
        r = wintypes.RECT()
        u32.GetWindowRect(self.hwnd, ctypes.byref(r))
        self.home = [r.left, r.top]
        self._save_anchor()

    def _save_anchor(self) -> None:
        fx, fy = ANCHOR[self.anchor]
        w, h = self.size
        self.apt = [int(round(self.home[0] + fx * w)), int(round(self.home[1] + fy * h))]
        threading.Thread(target=self.host.save_position, args=(self.oid, self.apt[0], self.apt[1], self.anchor),
                         daemon=True).start()

    def set_opts(self, locked: bool, anchor: str) -> None:
        """Из потока данных: закрепить (клики сквозь окно) и угол привязки."""
        with self.lock:
            self.want_opts = (bool(locked), anchor if anchor in ANCHOR else "tl")
        u32.PostMessageW(self.hwnd, WM_APP_FRAME, 0, 0)

    def _apply_opts(self) -> None:
        with self.lock:
            opts, self.want_opts = self.want_opts, None
        if not opts:
            return
        locked, anchor = opts
        if locked != self.locked:
            # закреплённое окно не ловит мышь: клики уходят в сим, таскать нельзя
            ex = u32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            ex = (ex | WS_EX_TRANSPARENT) if locked else (ex & ~WS_EX_TRANSPARENT)
            u32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex)
            self.locked = locked
        if anchor != self.anchor:
            # новый угол привязки — окно остаётся где было, запоминаем новую точку
            self.anchor = anchor
            if self.size != (0, 0):
                self._save_anchor()

    def dpi_changed(self, dpi: int) -> None:
        self.scale = (dpi or 96) / 96.0


class Feed(threading.Thread):
    """Поток оверлея: описание и данные с сервера, отрисовка кадров."""

    def __init__(self, host: "Host", win: Window) -> None:
        super().__init__(daemon=True, name=f"overlay-{win.oid}")
        self.host, self.win = host, win
        self.painter = Painter(host.fetch)
        self.spec = None
        self.data = None
        self.last = None

    def run(self) -> None:
        next_spec = next_data = 0.0
        moving = dirty = False
        next_frame = 0.0                    # расписание кадров анимации
        while not self.host.stopping:
            now = time.monotonic()
            try:
                if now >= next_spec:
                    next_spec = now + SPEC_EVERY
                    spec = self.host.get_json(f"/api/broadcast/spec?id={self.win.oid}")
                    if spec and spec.get("ok") is not False and (
                            not self.spec or spec.get("version") != self.spec.get("version")):
                        self.spec = spec
                        self.painter.set_spec(spec)
                        dirty = True
                if self.spec and now >= next_data:
                    next_data = now + DATA_EVERY
                    data = self.host.get_json(self.spec.get("source") or "")
                    if data is not None:
                        self.data = data
                        dirty = True
                want = bool(self.data and self.data.get("visible") is not False)
                enter = (self.spec or {}).get("enter") or "fade"
                if want and (dirty or moving):
                    self.painter.set_scale(self.win.scale)
                    img, moving = self.painter.frame(self.data, time.monotonic())
                    raw = to_bgra(img)
                    if raw != self.last:
                        self.last = raw
                        self.win.post((raw, img.width, img.height), True, enter)
                    else:
                        self.win.post(None, True, enter)
                else:
                    if not want:
                        moving = False
                    self.win.post(None, want, enter)
                dirty = False
            except Exception:
                log.exception("оверлей %s: ошибка отрисовки", self.win.oid)
                moving = False
                time.sleep(1.0)
            now = time.monotonic()
            if moving:
                # ровно ANIM_FPS кадров: шаг считаем от расписания, а не «после
                # отрисовки ещё 1/60» — так выходило 40–45 кадров вразнобой
                next_frame = max(next_frame + 1 / ANIM_FPS, now) if next_frame else now + 1 / ANIM_FPS
                wait = next_frame - now
            else:
                next_frame = 0.0
                wait = max(0.01, min(next_data, next_spec) - now)
            time.sleep(max(0.0, min(wait, DATA_EVERY)))


class PilotFeed(threading.Thread):
    """Поток окна пилота: данные с /api/pilot/data, картинка — pilot_paint."""

    def __init__(self, host: "Host", win: Window) -> None:
        super().__init__(daemon=True, name=f"overlay-{win.oid}")
        self.host, self.win = host, win
        self.pid = win.oid[len(PILOT):]
        self.painter = PilotPainter(host.fetch)
        self.every = PILOT_EVERY.get(self.pid, 0.2)
        self.last = None

    def run(self) -> None:
        raw_prev = None
        moving = False
        fails = 0
        next_data = next_frame = 0.0
        data = None
        while not self.host.stopping:
            now = time.monotonic()
            try:
                dirty = False
                if now >= next_data:
                    shown = bool(data and data.get("visible"))
                    next_data = now + (self.every if shown else max(self.every, PILOT_HIDDEN.get(self.pid, 0.25)))
                    raw = self.host.fetch(f"/api/pilot/data?id={self.pid}")
                    fails = 0 if raw is not None else fails + 1
                    if fails >= 3:               # модуль выключили — окно уходит
                        data, raw_prev = None, None
                    if raw is not None and raw != raw_prev:
                        raw_prev = raw
                        try:
                            data = json.loads(raw)
                            dirty = True
                        except ValueError:
                            pass
                want = bool(data and data.get("visible"))
                if dirty and data:
                    cfg = data.get("cfg") or {}
                    opts = (bool(cfg.get("lock")), cfg.get("anchor") or "tl")
                    if opts != (self.win.locked, self.win.anchor):
                        self.win.set_opts(*opts)
                if want and (dirty or moving):
                    self.painter.set_scale(self.win.scale)
                    img, moving = self.painter.frame(self.pid, data, time.monotonic())
                    raw_img = to_bgra(img)
                    if raw_img != self.last:
                        self.last = raw_img
                        self.win.post((raw_img, img.width, img.height), True, "fade")
                    else:
                        self.win.post(None, True, "fade")
                else:
                    if not want:
                        moving = False
                    self.win.post(None, want, "fade")
            except Exception:
                log.exception("оверлей %s: ошибка отрисовки", self.win.oid)
                moving = False
                time.sleep(1.0)
            now = time.monotonic()
            if moving:
                step = 1 / max(1, getattr(self.painter, "fps", ANIM_FPS))
                next_frame = max(next_frame + step, now) if next_frame else now + step
                wait = min(next_frame, next_data) - now
            else:
                next_frame = 0.0
                wait = next_data - now
            time.sleep(max(0.0, min(wait, 0.5)))


class Sprite:
    """Деталь шторки: слоистое окно с готовой картинкой; кадр — только место и прозрачность."""

    def __init__(self, host: "Host", img) -> None:
        self.w, self.h = img.size
        self.hwnd = u32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            CLASS_NAME, TITLE.format("fx"), WS_POPUP, 0, 0, 1, 1, None, None, host.hinst, None)
        self.dc = gdi.CreateCompatibleDC(None)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.w, -self.h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        self.bmp = gdi.CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        self.old = gdi.SelectObject(self.dc, self.bmp)
        raw = to_bgra(img)
        ctypes.memmove(bits, raw, len(raw))
        self.shown, self.last, self.rgn = False, None, None
        blend = BLENDFUNCTION(0, 0, 0, AC_SRC_ALPHA)
        u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(wintypes.POINT(-20000, -20000)),
                                ctypes.byref(wintypes.SIZE(self.w, self.h)), self.dc,
                                ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)

    def place(self, x: float, y: float, a: float, cut) -> None:
        if cut != self.rgn:
            self.rgn = cut
            if cut is None:
                u32.SetWindowRgn(self.hwnd, None, True)
            elif cut[0] == "circle":
                _k, cx, cy, r = cut
                u32.SetWindowRgn(self.hwnd, gdi.CreateEllipticRgn(int(cx - r), int(cy - r), int(cx + r), int(cy + r)), True)
            else:
                _k, cx, cy, r0, r1 = cut
                outer = gdi.CreateEllipticRgn(int(cx - r1), int(cy - r1), int(cx + r1), int(cy + r1))
                inner = gdi.CreateEllipticRgn(int(cx - r0), int(cy - r0), int(cx + r0), int(cy + r0))
                gdi.CombineRgn(outer, outer, inner, RGN_DIFF)
                gdi.DeleteObject(inner)
                u32.SetWindowRgn(self.hwnd, outer, True)         # регион теперь принадлежит окну
        alpha = max(0, min(255, int(round(255 * a))))
        key = (int(round(x)), int(round(y)), alpha)
        if key == self.last:
            return
        self.last = key
        blend = BLENDFUNCTION(0, 0, alpha, AC_SRC_ALPHA)
        u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(wintypes.POINT(key[0], key[1])), None, None,
                                None, 0, ctypes.byref(blend), ULW_ALPHA)
        want = alpha > 0
        if want != self.shown:
            u32.ShowWindow(self.hwnd, SW_SHOWNA if want else SW_HIDE)
            self.shown = want

    def raise_(self) -> None:
        u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def close(self) -> None:
        u32.DestroyWindow(self.hwnd)
        gdi.SelectObject(self.dc, self.old)
        gdi.DeleteObject(self.bmp)
        gdi.DeleteDC(self.dc)


class FxStage:
    """Шторка в потоке окон: детали создаёт и двигает по таймеру окон (каждые 10 мс).
    Картинки деталей готовит поток FxFeed, сюда приходят готовыми."""

    def __init__(self, host: "Host") -> None:
        self.host = host
        self.lock = threading.Lock()
        self.want = None             # (ключ набора, W, H, вариант, [(имя, картинка)])
        self.kit = None              # (ключ набора, {имя: Sprite}, порядок, (W, H, вариант))
        self.run = None              # (фаза, t0, слово) — что сейчас играет
        self.calc = None             # FxSprites.layout
        self._raised = None          # для какой шторки детали уже подняты наверх

    def load(self, key, W, H, variant, parts, calc) -> None:
        with self.lock:
            self.want, self.calc = (key, W, H, variant, parts), calc

    def play(self, phase: str, t0: float, word: str) -> None:
        with self.lock:
            self.run = (phase, t0, word) if phase else None

    def tick(self, now: float) -> None:
        with self.lock:
            want, run, calc = self.want, self.run, self.calc
            self.want = None
        if want is not None:
            self._build(want)
        if not self.kit:
            return
        _key, sprites, order, (W, H, v) = self.kit
        if run is None:
            for sp in sprites.values():
                if sp.shown:
                    sp.place(-20000, -20000, 0.0, sp.rgn)
            return
        phase, t0, word = run
        if self._raised != t0:
            # шторка — над всеми окнами, и над оверлеями, показанными позже деталей
            self._raised = t0
            for name in order:
                sprites[name].raise_()
        try:
            lay = calc(W, H, v, phase, now - t0, word)
        except Exception:
            log.exception("шторка: расчёт кадра")
            return
        for name in order:
            x, y, a, cut = lay.get(name, (-20000, -20000, 0.0, None))
            sprites[name].place(x, y, a, cut)

    def _build(self, want) -> None:
        key, W, H, v, parts = want
        if self.kit and self.kit[0] == key:
            return
        self.close()
        sprites, order = {}, []
        for name, img in parts:
            sprites[name] = Sprite(self.host, img)
            order.append(name)
        for name in order:                                  # снизу вверх: логотип — над панелями
            sprites[name].raise_()
        self.kit = (key, sprites, order, (W, H, v))

    def close(self) -> None:
        if self.kit:
            for sp in self.kit[1].values():
                try:
                    sp.close()
                except Exception:
                    pass
        self.kit = None


class FxFeed(threading.Thread):
    """Шторка повтора (окно на весь основной экран, клики насквозь) и метка «ПОВТОР».

    Фазу шторки (in / cover / out) и сколько она уже идёт даёт режиссёр
    (`/api/broadcast/fx`); дальше анимация идёт по своим часам с 60 кадрами в
    секунду, опрос — только чтобы узнать о смене фазы. Окно шторки показано
    лишь на время анимации: всё остальное время его нет на экране.
    """

    def __init__(self, host: "Host", win: Window) -> None:
        super().__init__(daemon=True, name=f"overlay-{win.oid}")
        self.host, self.win = host, win
        self.painter = FxPainter(host.fetch)
        self.screen = win.oid == FX_SCREEN
        self.last = None

    def run(self) -> None:
        if self.screen:
            # на весь основной монитор, в физических пикселях; само окно «replayfx» не
            # показывается — шторку собирают окна-детали (FxStage)
            W, H = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
            kit = FxSprites(self.painter)
            loaded = None
        key = None                         # (номер шторки, фаза) — смена фазы
        t0 = 0.0
        data, next_poll = None, 0.0
        bug_prev = None
        while not self.host.stopping:
            now = time.monotonic()
            try:
                if now >= next_poll:
                    next_poll = now + FX_POLL
                    d = self.host.get_json("/api/broadcast/fx")
                    if d is not None:
                        data = d
                        k = (d.get("n"), d.get("phase"))
                        if k != key:
                            key, t0 = k, now - float(d.get("t") or 0)
                if self.screen:
                    v = (data or {}).get("variant") or "D"
                    if data is not None and loaded != (v, W, H):
                        # детали варианта — заранее, пока шторки нет: к нажатию «Повтор» готовы
                        loaded = (v, W, H)
                        self.host.fx_stage.load(loaded, W, H, v, kit.sprites(W, H, v), kit.layout)
                    phase = (data or {}).get("phase") or ""
                    self.host.fx_stage.play(phase, t0, (data or {}).get("word") or "ПОВТОР")
                else:
                    bug = (data or {}).get("bug")
                    if bug != bug_prev:
                        bug_prev = bug
                        if bug:
                            self.painter.fonts.get("'Exo 2'", 15, 800, False)
                            img = self.painter.bug(bug, self.win.scale)
                            self.win.post((to_bgra(img), img.width, img.height), True, "fade")
                        else:
                            self.win.post(None, False, "fade")
            except Exception:
                log.exception("оверлей %s: ошибка отрисовки", self.win.oid)
                time.sleep(1.0)
            time.sleep(max(0.0, next_poll - time.monotonic()))


class Host:
    def __init__(self, base: str, ids: list[str], positions: dict) -> None:
        self.base = base.rstrip("/")
        self.stopping = False
        self._tls = threading.local()           # постоянное соединение потока, см. fetch
        self._ok_at = time.monotonic()
        self.hinst = k32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)          # ссылку держим: иначе её соберёт GC
        self._cursor = u32.LoadCursorW(None, ctypes.c_void_p(IDC_SIZEALL))
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = self.hinst
        wc.hCursor = self._cursor
        wc.lpszClassName = CLASS_NAME
        if not u32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(f"класс окна не зарегистрирован: {ctypes.get_last_error()}")
        self.windows: dict[str, Window] = {}
        self.fx_stage = FxStage(self)              # шторка повтора — окна-детали
        self._by_hwnd: dict[int, Window] = {}
        self._new: list[tuple[str, dict]] = []      # окна, которые появились после старта
        self._new_lock = threading.Lock()
        for oid in ids:
            self._add(oid, positions.get(oid) or {}, start=False)
        first = next(iter(self.windows.values()))
        # Системный таймер Windows по умолчанию тикает раз в 15,6 мс: и пауза
        # потока отрисовки, и WM_TIMER окон шли с этим шагом, а два несовпадающих
        # ритма давали рывки. 1 мс — на время жизни процесса окон оверлеев.
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except OSError:
            pass
        u32.SetTimer(first.hwnd, 1, PRESENT_MS, None)
        for w in self.windows.values():
            self._feed(w)
        threading.Thread(target=self._layout_watch, daemon=True, name="overlay-layout").start()

    def _add(self, oid: str, pos: dict, start: bool = True) -> None:
        w = Window(self, oid, pos)
        self.windows[oid] = w
        self._by_hwnd[w.hwnd] = w
        if start:
            self._feed(w)

    def _feed(self, w: Window) -> None:
        if w.oid in (FX_SCREEN, FX_BUG):
            FxFeed(self, w).start()
            return
        (PilotFeed if w.oid.startswith(PILOT) else Feed)(self, w).start()

    def _layout_watch(self) -> None:
        """Модуль включили после старта хоста — его окна создаём на ходу
        (создаёт поток окон, в _tick: окно принадлежит потоку, который его создал)."""
        while not self.stopping:
            time.sleep(LAYOUT_EVERY)
            ids, pos = _layout_once(self.base)
            fresh = [(i, pos.get(i) or {}) for i in ids or [] if i not in self.windows]
            if fresh:
                with self._new_lock:
                    self._new = fresh

    # ---------- сеть ----------

    def fetch(self, path: str) -> bytes | None:
        """Ответ сервера. Своё соединение у каждого потока и держится открытым
        (сервер — HTTP/1.1 keep-alive): новое соединение на каждый из десятков
        запросов в секунду стоило заметную долю процессора."""
        if not path:
            return None
        if path.startswith("http"):
            try:
                raw = urllib.request.urlopen(path, timeout=3).read()
                self._ok_at = time.monotonic()
                return raw
            except Exception:
                return None
        for attempt in (0, 1):
            conn = getattr(self._tls, "conn", None)
            if conn is None:
                u = urllib.parse.urlsplit(self.base)
                conn = self._tls.conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=3)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                raw = resp.read()
                if resp.status != 200:
                    return None
                self._ok_at = time.monotonic()
                return raw
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                self._tls.conn = None          # сервер закрыл соединение — одна попытка заново
        return None

    def get_json(self, path: str):
        raw = self.fetch(path)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def save_position(self, oid: str, x: int, y: int, anchor: str = "tl") -> None:
        try:
            pilot = oid.startswith(PILOT)
            body = {"id": oid[len(PILOT):] if pilot else oid, "x": x, "y": y}
            if pilot:
                body["anchor"] = anchor
            req = urllib.request.Request(
                self.base + ("/api/pilot/position" if pilot else "/api/broadcast/position"),
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            log.warning("положение оверлея %s не сохранилось", oid)

    # ---------- окна ----------

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_NCHITTEST:
                return HTCAPTION           # таскать за любую непрозрачную точку
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                u32.SetCursor(self._cursor)
                return 1
            if msg in (WM_CONTEXTMENU, WM_NCRBUTTONDOWN, WM_NCRBUTTONUP,
                       WM_NCLBUTTONDBLCLK, WM_CLOSE):
                return 0                   # ни системного меню, ни «развернуть»
            if msg == WM_SYSCOMMAND and (wp & 0xFFF0) != SC_MOVE:
                return 0
            w = self._by_hwnd.get(hwnd)
            if msg == WM_TIMER:
                self._tick()
                try:
                    self.fx_stage.tick(time.monotonic())
                except Exception:
                    log.exception("шторка")
                return 0
            if msg == WM_APP_FRAME:
                if w:
                    w.tick(time.monotonic())
                return 0
            if msg == WM_ENTERSIZEMOVE and w:
                w.dragging = True
            elif msg == WM_EXITSIZEMOVE and w:
                w.dragging = False
                w.moved()
            elif msg == WM_DPICHANGED and w:
                w.dpi_changed(wp & 0xFFFF)
                return 0                   # размер задаёт картинка, а не система
        except Exception:
            log.exception("обработка сообщения окна")
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def _tick(self) -> None:
        now = time.monotonic()
        if self._new:
            with self._new_lock:
                fresh, self._new = self._new, []
            for oid, pos in fresh:
                if oid not in self.windows:
                    try:
                        self._add(oid, pos)
                    except Exception:
                        log.exception("окно %s не создалось", oid)
        for w in self.windows.values():
            # окну без нового кадра, без анимации и без смены показа делать нечего
            if w.pending is None and w.anim is None and w.want == w.shown and w.want_opts is None:
                continue
            try:
                w.tick(now)
            except Exception:
                log.exception("окно %s", w.oid)
        if now - self._ok_at > SERVER_GONE_S:
            log.info("сервер не отвечает %d с — программа закрыта, окна оверлеев уходят",
                     SERVER_GONE_S)
            self.stopping = True
            u32.PostQuitMessage(0)

    def loop(self) -> None:
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))


def _layout_once(base: str) -> tuple[list[str] | None, dict]:
    """Окна эфира и окна пилота (с приставкой «p:»). None — сервер не ответил."""
    ids: list[str] = []
    pos: dict = {}
    reached = False
    for path, pre in (("/api/broadcast/positions", ""), ("/api/pilot/positions", PILOT)):
        try:
            d = json.loads(urllib.request.urlopen(base.rstrip("/") + path, timeout=3).read())
        except urllib.error.HTTPError:
            reached = True                     # модуль выключен — его окон нет
            continue
        except Exception:
            continue
        reached = True
        if not isinstance(d, dict) or d.get("ok") is False:
            continue
        for i in d.get("overlays") or []:
            ids.append(pre + i)
            pos[pre + i] = (d.get("positions") or {}).get(i) or {}
    return (ids, pos) if reached else (None, {})


def _fetch_layout(base: str) -> tuple[list[str], dict]:
    """Какие оверлеи есть и где стоят. Сервер может подниматься секунду-две."""
    for _ in range(40):
        ids, pos = _layout_once(base)
        if ids:
            return ids, pos
        time.sleep(0.5)
    return [], {}


def run(base: str, storage=None) -> int:
    """Поднять окна оверлеев. Блокирует: это цикл сообщений окон.

    `storage` — папка движка прежнего хоста на WebView2; ядро по-прежнему её
    передаёт, окнам она не нужна.
    """
    _dpi_aware()
    ids, positions = _fetch_layout(base)
    if not ids:
        log.error("сервер не отдал список оверлеев — хост не запущен")
        return 2
    host = Host(base, ids, positions)
    log.info("хост оверлеев: окна %s", ", ".join(ids))
    host.loop()
    return 0
