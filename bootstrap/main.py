"""Точка входа exe.

Задача ядра — минимальна и стабильна: выбрать версию бандла, подсунуть её в
sys.path, запустить приложение, проследить за успешностью старта. Всё, что
меняется часто, живёт в бандле и обновляется без переустановки клиента.
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from pathlib import Path

from . import paths
from .updater import Updater
from .version import CORE_VERSION


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


def main() -> int:
    log_path = _setup_logging()
    log = logging.getLogger("bootstrap")
    log.info("ядро %s, запуск", CORE_VERSION)

    if not _single_instance():
        log.info("экземпляр уже запущен — открываю панель")
        import webbrowser
        webbrowser.open("http://127.0.0.1:8777/")
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
        # сюда же встанут broadcast и rutp по мере готовности

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

        rc = app.module("racecontrol")
        if rc and getattr(rc, "ctx", None) and rc.wants_main_thread():
            overlay = rc.build_overlay()
            tray.overlay = overlay
            log.info("оверлей рейс-контрола запущен")

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
