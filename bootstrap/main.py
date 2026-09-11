"""Точка входа exe.

Задача ядра — минимальна и стабильна: выбрать версию бандла, подсунуть её в
sys.path, запустить приложение, проследить за успешностью старта. Всё, что
меняется часто, живёт в бандле и обновляется без переустановки клиента.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import threading
import traceback
from pathlib import Path

from . import paths
from .updater import Updater
from .version import CORE_VERSION


def trace(event: str) -> None:
    """Записать след старта в обход logging.

    Бывали запуски, после которых в irc.log не появлялось ни строки, хотя
    программа работала. Пока причина не найдена, ведём отдельный простой
    файл: открыли, дописали, закрыли — без обработчиков и буферов.
    """
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {os.getpid()} {event}\n"
        with open(paths.data_dir() / "startup.log", "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def _setup_logging() -> Path:
    log = paths.data_dir() / "irc.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log, encoding="utf-8"), logging.StreamHandler()],
    )
    return log


def _single_instance() -> bool:
    """Второй запуск не поднимает второй сервер — просто открывает панель."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 8778))       # порт-семафор, не для данных
        globals()["_lock_socket"] = s
        return True
    except OSError:
        return False


def _panel_mode(url: str, deck: bool = False) -> int:
    """Окно панели отдельным процессом (`iRC.exe --panel <url>`).

    Главный поток приложения занят циклом Tk — на нём живут оверлеи, — а
    WebView2 требует своего цикла сообщений. Поэтому окно панели выносим в
    отдельный процесс: заодно её падение не уносит рейс-контрол.
    """
    _setup_logging()
    log = logging.getLogger("panel")
    log.info("окно панели: открываю %s", url)
    version = paths.read_pointer().get("current") or CORE_VERSION
    bundle = paths.version_dir(version)
    sys.path.insert(0, str(bundle))
    try:
        from suite.core.ui.panel_native import run
    except Exception:
        log.error("панель не загрузилась: %s", traceback.format_exc())
        return 2
    icon = bundle / "suite" / "assets" / "icon.ico"
    try:
        if deck:
            # у пульта своя папка данных: одну папку WebView2 два окна
            # одновременно держать не могут
            code = run(url, icon=icon, storage=paths.data_dir() / "deck-webview",
                       title="iRC · пульт", size=(1120, 760), on_top=True)
        else:
            code = run(url, icon=icon, storage=paths.data_dir() / "panel-webview")
        log.info("окно панели закрыто")
        return code
    except Exception:
        log.error("окно панели упало: %s", traceback.format_exc())
        return 3


def _overlays_mode(url: str) -> int:
    """Окна эфирных оверлеев отдельным процессом (`iRC.exe --overlays <url>`)."""
    _setup_logging()
    log = logging.getLogger("overlays")
    log.info("окна оверлеев: %s", url)
    version = paths.read_pointer().get("current") or CORE_VERSION
    sys.path.insert(0, str(paths.version_dir(version)))
    try:
        from suite.core.ui.overlay_host import run
    except Exception:
        log.error("хост оверлеев не загрузился: %s", traceback.format_exc())
        return 2
    try:
        return run(url, storage=paths.data_dir() / "overlay-webview")
    except Exception:
        log.error("хост оверлеев упал: %s", traceback.format_exc())
        return 3


def _panel_port() -> int:
    """Порт панели из настроек: у второго экземпляра своего ядра ещё нет."""
    try:
        cfg = json.loads((paths.data_dir() / "config.json").read_text("utf-8"))
        return int(cfg.get("http_port") or 8777)
    except Exception:
        return 8777


def _show_running_panel(log) -> None:
    """Попросить уже запущенный экземпляр показать своё окно.

    Раньше здесь открывался браузер — и вместо родного окна пользователь
    получал вкладку. Работающий экземпляр умеет поднять своё окно сам.
    """
    url = f"http://127.0.0.1:{_panel_port()}/api/panel"
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"),
                               timeout=5).read()
        return
    except Exception:
        log.warning("не достучался до запущенного экземпляра — открываю браузером")
    import webbrowser
    webbrowser.open(url.replace("/api/panel", "/"))


def main() -> int:
    trace(f"старт argv={sys.argv[1:]} exe={sys.executable}")
    if "--panel" in sys.argv:
        i = sys.argv.index("--panel")
        if i + 1 >= len(sys.argv):
            return 2
        return _panel_mode(sys.argv[i + 1])
    if "--deck" in sys.argv:
        i = sys.argv.index("--deck")
        if i + 1 >= len(sys.argv):
            return 2
        return _panel_mode(sys.argv[i + 1], deck=True)
    if "--overlays" in sys.argv:
        i = sys.argv.index("--overlays")
        if i + 1 >= len(sys.argv):
            return 2
        return _overlays_mode(sys.argv[i + 1])

    log_path = _setup_logging()
    log = logging.getLogger("bootstrap")
    log.info("ядро %s, запуск", CORE_VERSION)
    trace(f"логи пишутся в {log_path}")

    if not _single_instance():
        log.info("экземпляр уже запущен — показываю его панель")
        _show_running_panel(log)
        return 0

    updater = Updater()

    rolled = updater.rollback_if_needed()
    if rolled:
        log.warning("предыдущая версия падала при старте — откат на %s", rolled)

    version = updater.ensure_seeded(fallback_version=CORE_VERSION)
    bundle = paths.version_dir(version)
    log.info("активная версия бандла: %s (%s)", version, bundle)

    sys.path.insert(0, str(bundle))
    updater.note_start()

    try:
        from suite.core import config as suite_config
        from suite.core.app import Application
        from suite.core.tray import Tray
        from suite.core.ui import host
        from suite.modules.broadcast import BroadcastModule
        from suite.modules.racecontrol import RaceControlModule
    except Exception:
        log.error("не удалось загрузить бандл:\n%s", traceback.format_exc())
        return 2

    try:
        moved = suite_config.migrate_from_racecontrol()
        if moved:
            log.info("перенесено из отдельного рейс-контрола: %s", moved)

        app = Application()
        updater.manifest_url = str(app.cfg.get("update_url") or "")

        app.register(RaceControlModule())
        app.register(BroadcastModule())
        # сюда же встанет rutp по мере готовности

        tray = Tray(app)
        app.on_mode_change = lambda old, new: tray.update_mode(new)
        app.on_incident_notify = lambda inc: tray.notify(
            f"#{inc.car_number} {inc.driver_name} +{inc.inc_delta}x"
            + (" (оценочно)" if inc.estimated else ""), "Инцидент")

        # Tk-хост поднимаем ДО модулей: их окна — Toplevel поверх него
        host.start()
        app.start()
        updater.note_ok()
        log.info("панель: %s", app.server.url)

        if app.cfg.get("update_check_on_start") and updater.manifest_url:
            threading.Thread(
                target=lambda: _bg_check(updater, tray, log), daemon=True).start()

        # Tk обязан жить в главном потоке, поэтому трей уходит в фон
        tray.run_detached()

        # Никаких окон при старте: на экране должно быть только главное окно
        # приложения. Оверлеи и пульт создаются при первом показе — их
        # включают из панели или из трея.

        host.run()                    # блокирует до выхода из трея
        return 0
    except Exception:
        log.error("падение приложения:\n%s", traceback.format_exc())
        return 3
    finally:
        log.info("завершение. лог: %s", log_path)


def _bg_check(updater: Updater, tray, log) -> None:
    try:
        res = updater.check()
        if res.get("update"):
            tray.notify(res["message"] + " — обновить из меню в трее")
        elif res.get("core_update_required"):
            tray.notify(res["message"])
    except Exception:
        log.exception("фоновая проверка обновлений не удалась")


if __name__ == "__main__":
    sys.exit(main())
