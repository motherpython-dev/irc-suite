"""Хост эфирных оверлеев: прозрачные родные окна поверх iRacing.

Один процесс (`iRC.exe --overlays <адрес>`) держит окна всех оверлеев на
одном движке WebView2 — пять окон делят его процессы, а не заводят по
своему. В каждом окне — та же страница `/ov/<id>`, что берёт OBS, поэтому
картинка в эфире с одного монитора и с отдельной машины одинаковая.

Показом управляет сама страница: она видит в данных, в эфире ли оверлей,
проигрывает анимацию и через мост просит показать или спрятать окно.
Прятать окно обязательно: прозрачное окно ловит клики даже пустым, и
убранный из эфира оверлей мешал бы мыши.

Две особенности pywebview, которые хост обходит:
  * прозрачное окно движок показывает сам при загрузке страницы, `hidden`
    ему не указ, и при этом активирует его. Поэтому после старта хост сам
    находит окна, прячет лишние и возвращает фокус тому, кто был активен;
  * штатный show() тоже активирует окно — iRacing теряет клавиатуру, и
    перестают работать горячие клавиши повтора и камер. Показываем
    системным ShowWindow(SW_SHOWNA): без активации (проверено на прототипе).
Окна создаются с focus=False (WS_EX_NOACTIVATE) — клик и перетаскивание
фокус тоже не забирают.
"""
from __future__ import annotations

import ctypes
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("ui.overlay_host")

SW_HIDE, SW_SHOWNA = 0, 8
GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
TITLE = "iRC overlay {}"


class Bridge:
    """Мост страницы оверлея к её окну.

    Поля с подчёркиванием pywebview странице не выставляет — ссылка на окно
    в открытом поле ломала инициализацию моста.
    """

    def __init__(self, host: "Host", oid: str) -> None:
        self._host, self._oid = host, oid

    def show(self) -> None:
        self._host.show(self._oid)

    def hide(self) -> None:
        self._host.hide(self._oid)

    def resize(self, w, h) -> None:
        self._host.resize(self._oid, int(w), int(h))


class Host:
    def __init__(self, base: str, ids: list[str], positions: dict,
                 prev_foreground: int = 0) -> None:
        import webview
        self.base = base.rstrip("/")
        self.ids = list(ids)
        self.windows: dict = {}
        self._hwnds: dict[str, int] = {}
        self._save_timers: dict[str, threading.Timer] = {}
        # чего хочет страница; пока окна не подготовлены, только запоминаем
        self._want: dict[str, bool] = {oid: False for oid in ids}
        self._ready = False
        self._prev_fg = prev_foreground
        for oid in ids:
            pos = positions.get(oid) or {}
            w = webview.create_window(
                TITLE.format(oid), f"{self.base}/ov/{oid}",
                width=380, height=240,
                x=int(pos.get("x", 40)), y=int(pos.get("y", 120)),
                frameless=True, on_top=True, transparent=True, focus=False,
                easy_drag=True, hidden=True, js_api=Bridge(self, oid))
            w.events.moved += (lambda o: lambda x, y: self._moved(o, x, y))(oid)
            self.windows[oid] = w
        threading.Thread(target=self._prepare, daemon=True,
                         name="overlay-prepare").start()

    # ---------- подготовка окон ----------

    def _prepare(self) -> None:
        """Служебный стиль, всё спрятать, нужное показать, фокус вернуть."""
        u32 = ctypes.windll.user32
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if all(self._hwnd(o) for o in self.ids):
                break
            time.sleep(0.2)
        time.sleep(0.8)                         # движок успевает сам их показать
        for oid in self.ids:
            h = self._hwnd(oid)
            if not h:
                log.warning("окно оверлея %s не найдено", oid)
                continue
            # как у прежних Tk-оверлеев: окна не лезут в панель задач и
            # Alt+Tab. Стиль меняем на спрятанном окне — у видимого панель
            # задач его не перечитывает.
            u32.ShowWindow(h, SW_HIDE)
            ex = u32.GetWindowLongW(h, GWL_EXSTYLE)
            u32.SetWindowLongW(h, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        self._ready = True
        for oid, want in self._want.items():
            if want:
                self.show(oid)
        _restore_foreground(self._prev_fg)
        log.info("окна оверлеев готовы; в эфире: %s",
                 ", ".join(o for o, w in self._want.items() if w) or "ничего")

    def _hwnd(self, oid: str) -> int:
        h = self._hwnds.get(oid)
        if not h or not ctypes.windll.user32.IsWindow(h):
            h = ctypes.windll.user32.FindWindowW(None, TITLE.format(oid))
            self._hwnds[oid] = h
        return h

    # ---------- показ ----------

    def show(self, oid: str) -> None:
        self._want[oid] = True
        if not self._ready:
            return
        u32 = ctypes.windll.user32
        h = self._hwnd(oid)
        if not h:
            return
        u32.ShowWindow(h, SW_SHOWNA)
        # поверх всех, но без активации: iRacing остаётся с клавиатурой
        u32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def hide(self, oid: str) -> None:
        self._want[oid] = False
        if not self._ready:
            return
        h = self._hwnd(oid)
        if h:
            ctypes.windll.user32.ShowWindow(h, SW_HIDE)

    def resize(self, oid: str, w: int, h: int) -> None:
        """Подогнать окно под содержимое, НЕ показывая его.

        Штатный resize() pywebview показывает спрятанное окно: у пит-стопов по
        ходу гонки добавляются строки, окно подстраивало высоту и всплывало на
        экране, хотя в эфир его не выводили. Меняем размер системным вызовом
        без показа. Страница сообщает CSS-пиксели, окну нужны физические —
        пересчитываем по DPI монитора.
        """
        hwnd = self._hwnd(oid)
        if not hwnd or w <= 0 or h <= 0:
            return
        u32 = ctypes.windll.user32
        try:
            scale = (u32.GetDpiForWindow(hwnd) or 96) / 96.0
        except Exception:
            scale = 1.0
        u32.SetWindowPos(hwnd, 0, 0, 0, int(round(w * scale)), int(round(h * scale)),
                         SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)

    # ---------- положение ----------

    def _moved(self, oid: str, x: int, y: int) -> None:
        """Во время перетаскивания событий десятки — пишем, когда отпустили."""
        t = self._save_timers.get(oid)
        if t:
            t.cancel()
        t = threading.Timer(0.6, self._save_position, args=(oid, int(x), int(y)))
        t.daemon = True
        self._save_timers[oid] = t
        t.start()

    def _save_position(self, oid: str, x: int, y: int) -> None:
        try:
            req = urllib.request.Request(
                f"{self.base}/api/broadcast/position",
                data=json.dumps({"id": oid, "x": x, "y": y}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            log.warning("положение оверлея %s не сохранилось", oid)


def _restore_foreground(hwnd: int) -> None:
    """Вернуть передний план окну, что было активным до запуска хоста.

    Фоновому процессу Windows передний план не отдаёт; обходим тем же
    приёмом, что у панели: на миг цепляемся к очереди ввода текущего
    активного потока.
    """
    u32 = ctypes.windll.user32
    if not hwnd or not u32.IsWindow(hwnd) or u32.GetForegroundWindow() == hwnd:
        return
    fg = u32.GetForegroundWindow()
    mine = ctypes.windll.kernel32.GetCurrentThreadId()
    theirs = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(theirs and theirs != mine and u32.AttachThreadInput(theirs, mine, True))
    try:
        u32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u32.AttachThreadInput(theirs, mine, False)


def _fetch_layout(base: str) -> tuple[list[str], dict]:
    """Какие оверлеи есть и где стоят. Сервер может подниматься секунду-две."""
    url = base.rstrip("/") + "/api/broadcast/positions"
    for _ in range(40):
        try:
            d = json.loads(urllib.request.urlopen(url, timeout=3).read())
            return list(d.get("overlays") or []), dict(d.get("positions") or {})
        except Exception:
            time.sleep(0.5)
    return [], {}


def run(base: str, storage) -> int:
    """Поднять окна оверлеев. Блокирует: это цикл сообщений окон."""
    import webview
    from .panel_native import _die_together, _free_storage

    # кто был активен до нас — ему и вернём фокус после автопоказа движка
    prev_fg = ctypes.windll.user32.GetForegroundWindow()
    _die_together()
    storage = _free_storage(storage)
    storage.mkdir(parents=True, exist_ok=True)
    ids, positions = _fetch_layout(base)
    if not ids:
        log.error("сервер не отдал список оверлеев — хост не запущен")
        return 2
    Host(base, ids, positions, prev_fg)
    log.info("хост оверлеев: окна %s", ", ".join(ids))
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage))
    return 0
