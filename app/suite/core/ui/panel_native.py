"""Панель как родное окно приложения на движке WebView2.

Панель написана на HTML — это даёт одну кодовую базу с пультом, который
открывают с телефона. Но окном её показывал Chrome в режиме `--app`, и в
панели задач жила фавиконка вместо иконки программы: окном владел браузер.

WebView2 — системный движок Edge, он встраивается в НАШЕ окно. Страница та
же, а окно своё: свой значок, своё имя, никакой браузерной обвязки и
независимость от того, установлен ли у оператора Chrome. Runtime входит в
Windows 11 и ставится вместе с Edge на Windows 10.

Окно живёт ОТДЕЛЬНЫМ ПРОЦЕССОМ (см. `--panel` в bootstrap/main.py). Причина
простая: главный поток приложения занят циклом Tk, на котором держатся
оверлеи, а WebView2 требует собственного цикла сообщений. Делить один поток
они не могут, и разводить их по процессам надёжнее, чем по потокам: упавшая
панель не уносит с собой рейс-контрол.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("ui.panel_native")

APP_ID = "gg.irchamps.iRC.Panel"      # своя запись в панели задач, отдельно от Python

WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010


def available() -> bool:
    """Есть ли всё нужное: библиотека и системный движок."""
    try:
        import webview                                       # noqa: F401
        import webview.platforms.edgechromium                # noqa: F401
    except Exception:                                        # noqa: BLE001
        return False
    return True


def _windows_of(pid: int) -> list[tuple[int, str]]:
    """Видимые окна процесса. Для своего — чтобы повесить иконку, для
    чужого — чтобы поднять уже открытую панель вместо второй копии."""
    u32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                found.append((hwnd, buf.value))
        return True

    u32.EnumWindows(visit, 0)
    return found


def _to_front(hwnd: int) -> None:
    """Показать окно поверх остальных.

    Окно создаёт отдельный отсоединённый процесс, а Windows не даёт фоновому
    процессу забрать передний план: окно появлялось ЗА другими, и со стороны
    выглядело так, будто программа ушла в трей без окна.

    Обходим штатным приёмом: коротко делаем окно topmost и сразу снимаем
    флаг. Так окно оказывается сверху, но не залипает поверх всего.
    """
    u32 = ctypes.windll.user32
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    u32.ShowWindow(hwnd, 5)                   # SW_SHOW
    u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
    u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    u32.BringWindowToTop(hwnd)

    # SetForegroundWindow фоновому процессу Windows обычно запрещает. Штатный
    # обход — на миг подключиться к очереди ввода того потока, который сейчас
    # владеет передним планом: для системы это выглядит как передача фокуса
    # внутри одной группы.
    fg = u32.GetForegroundWindow()
    mine = k32_current_thread()
    theirs = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(theirs and theirs != mine
                    and u32.AttachThreadInput(theirs, mine, True))
    try:
        u32.SetForegroundWindow(hwnd)
        u32.SetActiveWindow(hwnd)
    finally:
        if attached:
            u32.AttachThreadInput(theirs, mine, False)


def _describe(hwnd: int) -> str:
    """Где оказалось окно: видимость, размеры, место в z-порядке.

    Нужно потому, что «окно не открылось» и «окно открылось за чужими
    окнами» со стороны выглядят одинаково, а лечатся по-разному.
    """
    u32 = ctypes.windll.user32
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    order, cur = [], u32.GetTopWindow(None)
    while cur:
        if u32.IsWindowVisible(cur):
            order.append(cur)
        cur = u32.GetWindow(cur, 2)                 # GW_HWNDNEXT
    place = order.index(hwnd) + 1 if hwnd in order else 0
    return (f"видимо={bool(u32.IsWindowVisible(hwnd))} "
            f"свёрнуто={bool(u32.IsIconic(hwnd))} "
            f"впереди={hwnd == u32.GetForegroundWindow()} "
            f"z={place}/{len(order)} "
            f"область={r.left},{r.top} {r.right - r.left}x{r.bottom - r.top}")


def _prepare_window(icon: Path) -> None:
    """Повесить иконку iRC и вывести окно вперёд.

    pywebview не отдаёт наружу форму окна (`window.native` пуст к моменту
    старта), поэтому идём напрямую: находим окно процесса и шлём ему сообщения.
    Способ не зависит от версии библиотеки и бэкенда.
    """
    u32 = ctypes.windll.user32
    for _ in range(60):                       # окно появляется не мгновенно
        windows = _windows_of(os.getpid())
        if windows:
            break
        time.sleep(0.25)
    else:
        log.warning("окно панели не найдено — ни иконки, ни показа")
        return

    if icon.exists():
        big = u32.LoadImageW(None, str(icon), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
        small = u32.LoadImageW(None, str(icon), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        for hwnd, _title in windows:
            u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
    else:
        log.warning("иконка не найдена: %s", icon)

    for hwnd, _title in windows:
        _to_front(hwnd)
        log.info("окно панели: %s", _describe(hwnd))


def focus(pid: int) -> bool:
    """Поднять уже открытое окно панели. False — окна нет."""
    u32 = ctypes.windll.user32
    windows = _windows_of(pid)
    if not windows:
        return False
    hwnd = windows[0][0]
    u32.ShowWindow(hwnd, 9)               # SW_RESTORE — развернуть из свёрнутого
    _to_front(hwnd)
    return True


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB = None                       # держим хэндл живым, иначе job закроется сразу


def k32_current_thread() -> int:
    return ctypes.windll.kernel32.GetCurrentThreadId()


def _die_together() -> None:
    """Забрать движок с собой.

    WebView2 поднимает свои процессы `msedgewebview2.exe`, и они переживают
    закрытие окна: остаются сиротами и держат папку данных движка. Следующий
    запуск получает занятое хранилище и молча показывает пустоту — программа
    как будто уходит в трей без окна.

    Лечится job-объектом: все процессы, порождённые после привязки, погибают
    вместе с нами — хоть по закрытию окна, хоть по kill извне.
    """
    global _JOB
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        log.warning("не удалось создать job — движок может пережить окно")
        return
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                       ctypes.sizeof(info)):
        log.warning("job создан, но лимит не выставлен")
        return
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        # Windows отказывает, когда процесс уже состоит в чужом job без права
        # вложения. Тогда за уборку отвечает родитель — он гасит дерево.
        log.warning("процесс не привязался к job (ошибка %d) — движок уберёт "
                    "родительский процесс", ctypes.get_last_error())
        return
    _JOB = job


def _free_storage(base: Path) -> Path:
    """Папку данных WebView2 нельзя делить между окнами.

    Если её держит другой процесс (например, зависшее прошлое окно), движок
    молча отдаёт пустое окно с ошибкой 0x800700AA. Проверяем блокировку и в
    этом случае уходим в отдельную папку: панель важнее сохранённого состояния.
    """
    lock = base / "EBWebView" / "lockfile"
    if lock.exists():
        try:
            with open(lock, "r+b"):
                pass
        except OSError:
            alt = base.with_name(f"{base.name}-{os.getpid()}")
            log.warning("хранилище WebView2 занято — открываю панель в %s", alt.name)
            return alt
    _drop_stale(base)
    return base


def _drop_stale(base: Path) -> None:
    """Убрать запасные папки, оставшиеся от прошлых аварийных запусков."""
    parent = base.parent
    if not parent.is_dir():
        return
    for d in parent.glob(f"{base.name}-*"):
        try:
            shutil.rmtree(d)
        except OSError:
            pass                      # ещё занята — уберём в следующий раз


# ---------- окно без системной рамки ----------
#
# Системная полоса с заголовком и кнопками выглядела чужой рядом с шапкой
# программы. Окно создаётся без рамки, а заголовком служит шапка страницы:
# WebView2 считает области с CSS `app-region: drag` заголовком окна, и
# перетаскивание, разворот двойным щелчком делает сама Windows. Кнопки
# свернуть / развернуть / закрыть рисует страница (irc.js) и зовёт WindowApi.
# Размер меняется за края: по краю окна оставлена полоса EDGE px, и на
# WM_NCHITTEST окно отвечает Windows, какой это край.

FRAMELESS = True
EDGE = 5
WM_NCHITTEST = 0x0084
HT = {(1, 0): 10, (2, 0): 11, (0, 1): 12, (1, 1): 13, (2, 1): 14, (0, 2): 15, (1, 2): 16, (2, 2): 17}
GWL_STYLE = -16
WS_SYSMENU, WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00080000, 0x00020000, 0x00010000
_SUBCLASS = []                                  # держим ссылку на колбэк, иначе его соберёт GC


def _main_window():
    import webview
    return webview.windows[0] if webview.windows else None


def _hwnd(window) -> int:
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:                                        # noqa: BLE001
        return 0


def _on_ui(window, fn) -> None:
    """Выполнить в потоке окна: WinForms не терпит вызовов из чужих потоков."""
    from System import Action
    window.native.Invoke(Action(fn))


def _fit_work_area(window) -> None:
    """Развёрнутое окно без рамки иначе закрывает панель задач."""
    def ui():
        from System.Windows.Forms import Screen
        f = window.native
        f.MaximizedBounds = Screen.FromHandle(f.Handle).WorkingArea
    _on_ui(window, ui)


class WindowApi:
    """Кнопки окна в шапке страницы: window.pywebview.api.* из irc.js."""

    def minimize(self):
        w = _main_window()
        if w:
            w.minimize()

    def toggle_maximize(self):
        w = _main_window()
        if not w:
            return
        if ctypes.windll.user32.IsZoomed(_hwnd(w)):
            w.restore()
        else:
            _fit_work_area(w)
            w.maximize()

    def close(self):
        w = _main_window()
        if w:
            w.destroy()


def _install_frame(window) -> None:
    """Включить заголовок из страницы, края для размера, сворачивание с панели задач."""
    def ui():
        f = window.native
        from System.Windows.Forms import Padding
        f.Padding = Padding(EDGE)
        hwnd = int(f.Handle.ToInt64())
        u32 = ctypes.windll.user32
        get_l = getattr(u32, "GetWindowLongPtrW", u32.GetWindowLongW)
        set_l = getattr(u32, "SetWindowLongPtrW", u32.SetWindowLongW)
        set_l(hwnd, GWL_STYLE, get_l(hwnd, GWL_STYLE) | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
        _subclass_edges(hwnd)

    try:
        _on_ui(window, ui)
        _fit_work_area(window)
    except Exception:                                        # noqa: BLE001
        log.exception("окно без рамки не настроилось")


def _enable_title_region(window) -> None:
    """Шапка страницы как заголовок окна (CSS app-region). Движок готов только
    после первой загрузки; настройка действует со следующей — перезагружаем один раз."""
    def ui():
        core = window.native.webview.CoreWebView2
        if core is not None and not core.Settings.IsNonClientRegionSupportEnabled:
            core.Settings.IsNonClientRegionSupportEnabled = True
            core.Reload()
    try:
        _on_ui(window, ui)
    except Exception:                                        # noqa: BLE001
        log.warning("app-region не поддерживается этой версией WebView2", exc_info=True)


def _subclass_edges(hwnd: int) -> None:
    """Края окна — для изменения размера (отвечаем на WM_NCHITTEST)."""
    from ctypes import wintypes as wt
    LRESULT = ctypes.c_ssize_t
    PROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                              ctypes.c_size_t, ctypes.c_size_t)
    u32, cc = ctypes.windll.user32, ctypes.windll.comctl32
    cc.DefSubclassProc.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    cc.DefSubclassProc.restype = LRESULT

    def proc(h, msg, wp, lp, _uid, _ref):
        if msg == WM_NCHITTEST and not u32.IsZoomed(h):
            x = ctypes.c_short(lp & 0xFFFF).value
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value
            r = wt.RECT()
            u32.GetWindowRect(h, ctypes.byref(r))
            col = 0 if x < r.left + EDGE else 2 if x >= r.right - EDGE else 1
            row = 0 if y < r.top + EDGE else 2 if y >= r.bottom - EDGE else 1
            if (col, row) != (1, 1):
                return HT[(col, row)]
        return cc.DefSubclassProc(h, msg, wp, lp)

    cb = PROC(proc)
    _SUBCLASS.append(cb)
    cc.SetWindowSubclass.argtypes = [wt.HWND, PROC, ctypes.c_size_t, ctypes.c_size_t]
    cc.SetWindowSubclass(hwnd, cb, 1, 0)


def run(url: str, icon: Path, storage: Path, title: str = "iRC",
        size: tuple[int, int] = (1180, 780), on_top: bool = False) -> int:
    """Показать страницу родным окном. Возврат — код выхода процесса.

    Функция блокирующая: она и есть цикл сообщений окна.

    on_top — окно поверх всех, в том числе полноэкранного iRacing (режим
    borderless). Так живёт пульт эфира. Фокус окно при этом забирает: в
    редакторе макросов нужно печатать, а спектейту потеря фокуса не мешает —
    broadcast-команды идут без него.
    """
    import webview

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:                                        # noqa: BLE001
        pass                                  # не критично, только группировка

    _die_together()
    storage = _free_storage(storage)
    storage.mkdir(parents=True, exist_ok=True)
    # В 2.35.0 шапка в узком окне вылезала за край: крестик пропадал, а
    # свободного места, за которое тянуть окно, не оставалось. Шапка теперь
    # сжимает вкладки, а логотип и свободный промежуток — всегда ручка окна.
    frameless = FRAMELESS
    win = webview.create_window(title, url, width=size[0], height=size[1],
                                background_color="#0B0E14", on_top=on_top,
                                frameless=frameless, easy_drag=False,
                                js_api=WindowApi() if frameless else None)
    if frameless:
        win.events.shown += lambda: _install_frame(win)
        win.events.loaded += lambda: _enable_title_region(win)
    threading.Thread(target=_prepare_window, args=(icon,), daemon=True,
                     name="panel-window").start()
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=str(storage))
    return 0
