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


def _set_icon(icon: Path) -> None:
    """Повесить иконку iRC на окно панели.

    pywebview не отдаёт наружу форму окна (`window.native` пуст к моменту
    старта), поэтому идём напрямую: находим окно процесса и шлём WM_SETICON.
    Способ не зависит от версии библиотеки и бэкенда.
    """
    if not icon.exists():
        return
    u32 = ctypes.windll.user32
    for _ in range(60):                       # окно появляется не мгновенно
        windows = _windows_of(os.getpid())
        if windows:
            break
        time.sleep(0.25)
    else:
        log.warning("окно панели не найдено — иконка осталась стандартной")
        return

    big = u32.LoadImageW(None, str(icon), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
    small = u32.LoadImageW(None, str(icon), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    for hwnd, _title in windows:
        u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)


def focus(pid: int) -> bool:
    """Поднять уже открытое окно панели. False — окна нет."""
    u32 = ctypes.windll.user32
    windows = _windows_of(pid)
    if not windows:
        return False
    hwnd = windows[0][0]
    u32.ShowWindow(hwnd, 9)               # SW_RESTORE — развернуть из свёрнутого
    u32.SetForegroundWindow(hwnd)
    return True


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
    return base


def run(url: str, icon: Path, storage: Path, title: str = "iRC",
        size: tuple[int, int] = (1180, 780)) -> int:
    """Показать панель родным окном. Возврат — код выхода процесса.

    Функция блокирующая: она и есть цикл сообщений окна.
    """
    import webview

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:                                        # noqa: BLE001
        pass                                  # не критично, только группировка

    storage = _free_storage(storage)
    storage.mkdir(parents=True, exist_ok=True)
    webview.create_window(title, url, width=size[0], height=size[1],
                          background_color="#0B0E14")
    threading.Thread(target=_set_icon, args=(icon,), daemon=True,
                     name="panel-icon").start()
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=str(storage))
    return 0
