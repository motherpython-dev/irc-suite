"""Панель как окно приложения, а не вкладка браузера.

Панель написана на HTML — это даёт одну кодовую базу и доступ с планшета или
машины стюарда. Но открывать её вкладкой среди прочих неудобно: это рабочий
инструмент, у него должно быть своё окно.

Решение — режим приложения браузера (`--app=`): Edge и Chrome открывают
страницу в окне без адресной строки, вкладок и меню. Отдельного движка ставить
не нужно, зависимостей ноль, а окно выглядит и ведёт себя как программа.

Если ни одного подходящего браузера нет — открываем обычным способом, чтобы
панель осталась доступной.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

log = logging.getLogger("ui.panel")

# Chrome первым: на свежем профиле Edge встречает экраном про синхронизацию,
# и вместо панели пользователь видит рекламу входа в учётную запись.
# Edge остаётся запасным — он есть на любой Windows 10/11.
BROWSERS = (
    ("chrome", [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]),
    ("msedge", [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]),
)

# гасим всё, что браузер норовит показать вместо страницы
QUIET = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--no-service-autorun",
    "--disable-background-networking",
    "--disable-features=msEdgeIdentityDefaultPrompt,msImplicitSignin,"
    "EdgeIdentityWelcome,ChromeWhatsNewUI,PrivacySandboxSettings4",
]


def _find_browser() -> str | None:
    for name, paths in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
        for p in paths:
            if Path(p).exists():
                return p
    return None


def open_panel(url: str, profile_dir: Path | None = None,
               size: tuple[int, int] = (1180, 780)) -> bool:
    """Открыть панель окном приложения. True — получилось.

    Отдельный профиль нужен, чтобы окно не наследовало вкладки и расширения
    рабочего браузера и открывалось само по себе.
    """
    exe = _find_browser()
    if not exe:
        webbrowser.open(url)
        return False

    args = [exe, f"--app={url}", f"--window-size={size[0]},{size[1]}", *QUIET]
    if profile_dir:
        profile_dir.mkdir(parents=True, exist_ok=True)
        args.append(f"--user-data-dir={profile_dir}")

    try:
        # DETACHED_PROCESS: окно живёт своей жизнью и не тянет за собой iRC
        subprocess.Popen(args, close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return True
    except Exception:
        log.exception("не удалось открыть окно панели, падаю в браузер")
        webbrowser.open(url)
        return False
