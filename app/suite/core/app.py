"""Ядро Suite: общие ресурсы и жизненный цикл модулей.

Здесь живёт всё, что делится между модулями — клиент сима, broadcast-команды,
хранилище, HTTP-сервер, шина SSE. Логика конкретных функций (инциденты, башня
позиций, ливреи) вынесена в модули.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import config
from .commands import RaceControl
from .license import CHECK_EVERY_S, License
from .modules import Context, Registry
from .server import Hub, PanelServer
from .simclient import MODE_LIVE, MODE_REPLAY, SimClient
from .storage import SQLiteStorage
from .version import VERSION

log = logging.getLogger("irc")


def _update_not_published(res: dict) -> bool:
    """Проверка не нашла файла версии на сервере (404): выпусков ещё не было.

    Механизм проверки живёт в ядре (exe) и отдаёт только текст ошибки, поэтому
    различаем по нему: так правка работает и на уже установленных клиентах.
    """
    if res.get("error") == "not_found":
        return True
    return "HTTP Error 404" in str(res.get("message") or "")


def run_later(line: str) -> None:
    """Команда cmd, которая переживёт закрытие программы, — без окна.

    Строкой, как есть: список аргументов Python экранирует кавычки внутри
    команды обратными слэшами, и cmd получал вместо пути «\"» («Windows не
    удаётся найти "\\"»). cmd /c "…" снимает только внешние кавычки.
    Без окна: CREATE_NO_WINDOW и скрытое окно — иначе ping вылезал консолью.
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (0x01000000, 0):               # CREATE_BREAKAWAY_FROM_JOB, если можно
        try:
            subprocess.Popen(f'cmd.exe /d /c "{line}"', close_fds=True, startupinfo=si,
                             creationflags=flags | extra)
            return
        except OSError:
            continue
    raise OSError("не удалось запустить команду")


def _ensure_log_file() -> None:
    """Журнал в irc.log, даже если ядро его не подключило.

    Ядро (bootstrap) зовёт logging.basicConfig, а он ничего не делает, когда у
    корневого логгера уже есть обработчик (его ставит кто-то из библиотек раньше).
    Так irc.log с 21.10 не дописывался, хотя startup.log говорил «логи пишутся».
    Ядро без нового установщика не поменять — подключаем файл здесь.
    """
    root = logging.getLogger()
    path = str(config.log_path())
    import os
    if any(isinstance(h, logging.FileHandler) and os.path.abspath(getattr(h, "baseFilename", "")) == os.path.abspath(path)
           for h in root.handlers):
        return
    try:
        h = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


class Application:
    def __init__(self):
        _ensure_log_file()
        # запасной путь в сеть (системный прокси / напрямую) — для всех запросов
        from . import net
        net.install()
        self.cfg = config.load()
        self.storage = SQLiteStorage(str(config.db_path()))
        self.storage.open()

        self.sim = SimClient(poll_hz=float(self.cfg.get("poll_hz", 5.0)))
        self.rc = RaceControl(self.sim.ir, self.storage)

        self.hub = Hub()
        self.registry = Registry(log)
        self.server = PanelServer(self, self.hub, int(self.cfg.get("http_port", 8777)),
                                  dynamic=self.registry.routes,
                                  bind_all=bool(self.cfg.get("allow_lan", False)))

        self._stop = threading.Event()
        self._panel = None               # процесс окна панели, см. open_panel
        self._deck = None                # процесс окна пульта, см. open_deck
        self._overlays = None            # процесс окон оверлеев, см. ensure_overlays
        self.on_module_change = None     # трей перерисовывает меню
        self.on_mode_change = None       # трей меняет цвет значка
        self.tray_notify = None          # всплывающее уведомление из трея
        self.request_quit = None         # выход из программы (ставит ядро)
        # что сейчас с обновлением: idle, checking, uptodate, bundle_ready,
        # core_ready, error
        self.update = {"state": "idle", "message": "", "new_version": "",
                       "installer": "", "checked_at": 0.0}

        # Допуск: программа работает только у своих (см. license.py). Панель
        # поднимается всегда — иначе человеку негде подключить программу.
        self.license = License(config.data_dir(), VERSION, self.sim)
        self._gated = None               # что было применено в прошлый раз

        self.sim.on_mode_change = self._mode_changed

    # ---------- модули ----------

    def register(self, module) -> None:
        mid = module.id
        enabled = bool(config.module_config(self.cfg, mid).get("enabled", False))
        self.registry.add(module, enabled=enabled)

    def _make_ctx(self, module_id: str) -> Context:
        return Context(
            sim=self.sim,
            rc=self.rc,
            storage=self.storage,
            publish=self.hub.publish,
            config=config.module_config(self.cfg, module_id),
            save_config=lambda: config.save(self.cfg),
            log=logging.getLogger(f"module.{module_id}"),
            app=self,
        )

    def set_lan(self, allow: bool) -> dict:
        """Доступ с телефона. Требует перезапуска: порт занимается при старте."""
        self.cfg["allow_lan"] = bool(allow)
        config.save(self.cfg)
        return {"ok": True, "allow_lan": bool(allow), "restart_required": True}

    def set_module(self, module_id: str, enabled: bool) -> dict:
        """Включить или выключить модуль на лету."""
        if enabled and not self.license.allowed():
            return {"ok": False, "error": "locked",
                    "message": self.license.status()["message"]}
        res = (self.registry.start(module_id, self._make_ctx) if enabled
               else self.registry.stop(module_id))
        if res.get("ok"):
            config.module_config(self.cfg, module_id)["enabled"] = enabled
            config.save(self.cfg)
            self.storage.log_event("module_toggled",
                                   {"module": module_id, "enabled": enabled})
            if self.on_module_change:
                try:
                    self.on_module_change()
                except Exception:
                    pass
            self._push_state()
        return res

    def module(self, module_id: str):
        return self.registry.get(module_id)

    # ---------- API ядра ----------

    def state(self) -> dict:
        s = self.sim.snapshot()
        return {
            "version": VERSION,
            "mode": s.mode,
            "connected": s.connected,
            "session_num": s.session_num,
            "session_time": s.session_time,
            "session_time_estimated": s.session_time_estimated,
            "replay_frames_to_end": s.replay_frames_to_end,
            "session_tick": s.session_tick,
            "player_car_idx": s.player_car_idx,
            "player_incidents": s.player_incidents,
            "results_available": s.results_available,
            "spectator": s.spectator,
            "session_flags": s.session_flags,
            "session_state": s.session_state,
            "track": s.track,
            "subsession_id": s.subsession_id,
            "drivers": len(s.drivers),
            "modules": self.registry.statuses(),
            "lan_url": self.server.lan_url,
            "allow_lan": bool(self.cfg.get("allow_lan", False)),
            "demo": bool(self.sim.demo),
            "license": self.license.status(),
            "update": {k: self.update[k] for k in ("state", "message", "new_version")},
        }

    def drivers(self) -> list[dict]:
        s = self.sim.snapshot()
        return [{"car_idx": d.car_idx, "car_number": d.car_number,
                 "name": d.name, "team": d.team}
                for d in sorted(s.drivers, key=lambda d: d.name.lower())]

    def plates(self) -> dict:
        """Цвета плашек номеров для страниц программы: {номер: [цвет1, цвет2]}.

        Два цвета ливреи Trading Paints, если RUTP её скачал (как в Standings),
        иначе основной цвет из конструктора iRacing; второго тогда нет.
        """
        if getattr(self, "_livery", None) is None:
            from .livery_colors import LiveryColors
            self._livery = LiveryColors()
        folder = ""
        try:
            ru = self.registry.get("rutp")
            folder = str(((getattr(ru, "cfg", None) or {}).get("paint_folder")) or "")
            if not folder:
                from ..modules.rutp.client import suggest_paint_folder
                folder = suggest_paint_folder() or ""
        except Exception:                    # noqa: BLE001
            folder = ""
        out = {}
        for d in self.sim.snapshot().drivers:
            liv = self._livery.get(folder, getattr(d, "car_path", "") or "", getattr(d, "user_id", 0) or 0,
                                   getattr(d, "team_id", 0) or 0) if folder else None
            c = [liv[0], liv[1]] if liv else ([d.color] if getattr(d, "color", "") else [])
            if c and d.car_number:
                out[str(d.car_number)] = c
        return out

    def events(self, limit: int = 50) -> list[dict]:
        """Журнал на вкладке «Обзор»."""
        return self.storage.recent_events(max(1, min(int(limit), 200)))

    def find_drivers(self, query: str, limit: int = 8) -> list[dict]:
        """Поиск по номеру и по имени/фамилии.

        Номер ищем с начала строки (ввод «2» должен давать #2 раньше #23),
        имя — по любому слову, чтобы фамилию можно было набирать без имени.
        """
        q = query.strip().lstrip("#").lower()
        if not q:
            return self.drivers()[:limit]
        out = []
        for d in self.drivers():
            num, name = d["car_number"].lower(), d["name"].lower()
            if num == q:
                rank = 0
            elif num.startswith(q):
                rank = 1
            elif any(w.startswith(q) for w in name.split()):
                rank = 2
            elif q in name:
                rank = 3
            else:
                continue
            out.append((rank, d))
        out.sort(key=lambda r: (r[0], r[1]["name"]))
        return [d for _, d in out[:limit]]

    def sim_ready(self) -> dict | None:
        """broadcast через HWND_BROADCAST успешен всегда, даже когда сима нет.
        Поэтому связь проверяем явно, иначе панель врёт оператору."""
        if not self.sim.snapshot().connected:
            return {"ok": False, "error": "sim_offline",
                    "message": "iRacing не запущен или сессия не активна"}
        return None

    # ---------- события ----------

    def _mode_changed(self, old: str, new: str) -> None:
        self.storage.log_event("mode_change", {"from": old, "to": new})
        if self.on_mode_change:
            try:
                self.on_mode_change(old, new)
            except Exception:
                pass
        rc = self.registry.get("racecontrol")
        if rc and hasattr(rc, "on_mode_change"):
            try:
                rc.on_mode_change(old, new)
            except Exception:
                log.exception("racecontrol не обработал смену режима")
        self._push_state()

    def _push_state(self) -> None:
        self.hub.publish("state", self.state())

    # ---------- жизненный цикл ----------

    def set_demo(self, on: bool, session: str | None = None) -> dict:
        """Включить синтетическую гонку для проверки оверлеев.

        В конфиг НЕ сохраняем: демо, забытое включённым, во время реальной
        гонки увело бы рейс-контрол на выдуманные данные. После перезапуска
        программа всегда читает сим.
        """
        self.sim.set_demo(bool(on))
        if session:
            # проверка оверлеев вне гонки: практика, квалификация, тест
            self.sim.demo_session = {"practice": "Practice", "qualify": "Open Qualify",
                                     "testing": "Offline Testing"}.get(session, "Race")
        self.storage.log_event("demo", {"on": bool(on)})
        self._push_state()
        return {"ok": True, "demo": bool(self.sim.demo), "session": self.sim.demo_session}

    def open_panel(self, path: str = "") -> None:
        """Показать панель родным окном на WebView2.

        Окно поднимает отдельный процесс (`--panel`): главный поток занят
        циклом Tk, на котором держатся оверлеи. Если панель уже открыта —
        не плодим вторую, а поднимаем существующую.
        """
        url = self.server.url.rstrip("/") + (path or "/")
        from .ui import panel_native

        if self._panel and self._panel.poll() is None:
            if panel_native.focus(self._panel.pid):
                return
            # окно закрыли, а процесс ещё жив — не ждём, откроем заново

        try:
            self._panel = self._spawn_panel(url)
            code = self._panel.poll()
            if code is None:
                time.sleep(2.0)          # успел бы упасть — упал бы сразу
                code = self._panel.poll()
            if code is None:
                return
            log.error("процесс окна панели умер сразу, код %s — открываю браузером",
                      code)
            self._trace(f"панель умерла, код {code}")
        except Exception:
            log.exception("родное окно панели не запустилось — открываю браузером")
            self._trace("панель не запустилась: исключение")

        from .ui.panel_window import open_panel as open_window
        try:
            open_window(url, profile_dir=config.data_dir() / "panel-profile")
        except Exception:
            # без панели программа остаётся живой в трее, но пользователь
            # видит только иконку и не понимает, почему — поэтому в лог
            log.exception("панель не открылась: %s", url)

    @staticmethod
    def _trace(event: str) -> None:
        """След в startup.log — тот же файл, что ведёт точка входа."""
        try:
            import os
            with open(config.data_dir() / "startup.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {os.getpid()} {event}\n")
        except Exception:
            pass

    @staticmethod
    def _close_panel_tree(pid: int) -> None:
        """Закрыть окно панели вместе с процессами движка.

        WebView2 поднимает свои `msedgewebview2.exe`, и просто убить окно мало:
        они остаются сиротами, держат папку данных движка, и следующий запуск
        показывает пустоту вместо панели. Поэтому гасим дерево целиком.
        """
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _spawn_panel(self, url: str) -> subprocess.Popen:
        return self._spawn_window("--panel", url, "panel-window.log")

    def _spawn_window(self, flag: str, url: str, log_name: str) -> subprocess.Popen:
        """Процесс родного окна: сам exe с флагом, а из исходников — run.py."""
        if getattr(sys, "frozen", False):
            args = [sys.executable, flag, url]
        else:
            # из исходников точка входа — тот же run.py, которым запустили
            # приложение; вычислять её от __file__ нельзя: модуль лежит в
            # распакованном бандле, а не в репозитории
            args = [sys.executable, str(Path(sys.argv[0]).resolve()), flag, url]
        log.info("окно %s: %s", flag.lstrip("-"), url)
        # Вывод процесса уводим в файл: у отсоединённого окна нет консоли, и
        # без этого падение движка не оставляет никаких следов.
        out = open(config.data_dir() / log_name, "a", encoding="utf-8")
        proc = subprocess.Popen(args, close_fds=True, stdout=out, stderr=out,
                                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        # Разрешаем окну выйти вперёд: без этого Windows не даёт фоновому
        # процессу передний план, и окно поднимается за чужими окнами.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(proc.pid)
        except Exception:
            pass
        return proc

    # ---------- пульт эфира ----------

    def open_deck(self) -> dict:
        """Пульт родным окном поверх iRacing. Второй раз — поднять открытый."""
        from .ui import panel_native
        if self._deck and self._deck.poll() is None:
            panel_native.focus(self._deck.pid)
            return {"ok": True, "visible": True}
        url = self.server.url.rstrip("/") + "/deck"
        try:
            self._deck = self._spawn_window("--deck", url, "deck-window.log")
        except Exception:
            log.exception("окно пульта не запустилось")
            return {"ok": False, "error": "spawn_failed"}
        return {"ok": True, "visible": True}

    def close_deck(self) -> dict:
        if self._deck and self._deck.poll() is None:
            self._close_panel_tree(self._deck.pid)
        self._deck = None
        return {"ok": True, "visible": False}

    def deck_open(self) -> bool:
        return bool(self._deck and self._deck.poll() is None)

    # ---------- окна оверлеев ----------

    def ensure_overlays(self) -> None:
        """Поднять процесс окон оверлеев при первом выходе в эфир.

        Все оверлеи живут в одном процессе (ui/overlay_host.py): прозрачные
        слоистые окна, картинку рисует PIL. При старте программы его нет: на
        экране должно быть только главное окно.
        """
        if self._overlays and self._overlays.poll() is None:
            return
        try:
            self._overlays = self._spawn_window(
                "--overlays", self.server.url.rstrip("/"), "overlays-window.log")
        except Exception:
            log.exception("окна оверлеев не запустились")

    # ---------- допуск ----------

    def quit(self) -> dict:
        """Штатный выход по команде: модули останавливаются, база закрывается.

        Нужен сборке и проверкам — убитый посреди записи процесс однажды уже
        испортил базу инцидентов.
        """
        if not callable(self.request_quit):
            return {"ok": False, "error": "no_quit"}
        threading.Timer(0.5, self.request_quit).start()
        return {"ok": True}

    def license_status(self) -> dict:
        return {"ok": True, **self.license.status()}

    def license_login_start(self) -> dict:
        """Вход через сайт: адрес открываем системным браузером — там у
        человека уже есть вход на сайт, а окно панели его не знает."""
        res = self.license.start_login(int(self.cfg.get("http_port", 8777)))
        if res.get("ok"):
            webbrowser.open(res["url"])
        return res

    def license_callback(self, code: str, state: str) -> dict:
        """Сайт вернул человека в программу после входа."""
        res = self.license.finish_login(code, state)
        self._apply_gate()
        if res.get("ok"):
            # вернуть человека из браузера в программу
            threading.Thread(target=self.open_panel, daemon=True).start()
        return res

    def license_open(self) -> dict:
        webbrowser.open(self.license.browser_url())
        return {"ok": True}

    def license_pair_start(self) -> dict:
        res = self.license.start_pair()
        if res.get("ok"):
            webbrowser.open(res["url"])
        return res

    def license_pair_poll(self) -> dict:
        res = self.license.poll_pair()
        if res.get("status") == "approved":
            self._apply_gate()
        return res

    def license_check(self) -> dict:
        res = self.license.check()
        self._apply_gate()
        return res

    def license_unlink(self) -> dict:
        res = self.license.unlink()
        self._apply_gate()
        return res

    def _start_modules(self) -> None:
        """Поднять модули, включённые в настройках."""
        for mid in self.registry.ids():
            if config.module_config(self.cfg, mid).get("enabled"):
                self.registry.start(mid, self._make_ctx)

    def _apply_gate(self) -> None:
        """Пустить программу к работе или погасить её.

        Гасим полностью: модули останавливаются, окна оверлеев и пульта
        закрываются. Панель остаётся — через неё программу и подключают.
        """
        allowed = self.license.allowed()
        if allowed == self._gated:
            return
        self._gated = allowed
        if allowed:
            self._start_modules()
            log.info("допуск получен: модули запущены")
            return
        log.warning("допуск закрыт (%s): модули остановлены",
                    self.license.status()["reason"])
        self.registry.stop_all()
        try:
            self.close_deck()
        except Exception:                          # noqa: BLE001
            log.debug("пульт не закрылся", exc_info=True)
        if self._overlays and self._overlays.poll() is None:
            try:
                self._close_panel_tree(self._overlays.pid)
            except Exception:                      # noqa: BLE001
                log.debug("окна оверлеев не закрылись", exc_info=True)
        self._push_state()

    def _license_loop(self) -> None:
        """Раз в час спрашиваем сайт; между этим следим за симом и запасом."""
        from .license import ENFORCE
        if not ENFORCE:
            # допуск выключен: никого не блокируем, но привязанный аккаунт
            # сверяем при запуске — имя и привязка iRacing в панели актуальны
            if self.license.state.get("key"):
                self.license.check()
                self._push_state()
            return
        self.license.check()
        self._apply_gate()
        last_check = time.time()
        while not self._stop.is_set():
            if self._stop.wait(20):
                return
            if time.time() - last_check >= CHECK_EVERY_S:
                last_check = time.time()
                self.license.check()
            # решение меняется и без сети: сменился аккаунт в симе, истёк запас
            self._apply_gate()

    # ---------- обновления ----------

    def _updater(self):
        """Механизм обновления живёт в ядре (exe), а решение о нём — здесь, в
        бандле: так сам порядок обновления можно чинить обновлением."""
        from bootstrap.updater import Updater
        return Updater(str(self.cfg.get("update_url") or ""))

    def _notify(self, text: str) -> None:
        if callable(self.tray_notify):
            try:
                self.tray_notify(text)
            except Exception:
                log.debug("уведомление не показалось", exc_info=True)

    def update_status(self) -> dict:
        return {"ok": True, "version": VERSION,
                "configured": bool(self.cfg.get("update_url")), **self.update}

    def update_check(self) -> dict:
        """Проверка по кнопке в панели."""
        return self._update_once(manual=True)

    def _update_once(self, manual: bool = False) -> dict:
        # уже скачано и ждёт перезапуска — повторно не качаем
        if self.update["state"] in ("bundle_ready", "core_ready") and not manual:
            return self.update_status()
        u = self._updater()
        if not u.manifest_url:
            self.update.update(state="idle", message="Адрес обновлений не задан")
            return self.update_status()
        self.update.update(state="checking", message="Проверяю обновления…")
        res = u.check()
        self.update["checked_at"] = time.time()
        try:
            if res.get("update"):
                man = res["manifest"]
                got = u.apply(man)
                if got.get("ok"):
                    self.update.update(state="bundle_ready", new_version=str(man.get("version")),
                                       message=f"Обновлено до {man.get('version')}. "
                                               f"Изменения появятся после перезапуска.")
                    self._notify(self.update["message"])
                else:
                    self.update.update(state="error", message=got.get("message") or "Обновление не удалось")
            elif res.get("core_update_required"):
                man = res.get("manifest") or {}
                got = u.fetch_installer(man)
                if got.get("ok"):
                    self.update.update(state="core_ready", new_version=str(man.get("version")),
                                       installer=got["path"],
                                       message=f"Версия {man.get('version')} требует установки. "
                                               f"Установщик скачан — нажмите «Установить».")
                    if not got.get("cached"):
                        self._notify(self.update["message"])
                else:
                    self.update.update(state="error", message=got.get("message") or res.get("message", ""))
            elif res.get("ok"):
                self.update.update(state="uptodate", new_version="",
                                   message=f"Обновлений нет · установлена версия {VERSION}")
            elif _update_not_published(res):
                # файла версии на сервере нет — выпусков ещё не было, значит и
                # обновлений нет. Это не ошибка, пугать человека незачем.
                self.update.update(state="uptodate", new_version="",
                                   message=f"Обновлений нет · установлена версия {VERSION}")
            else:
                # нет связи: обновления могут быть, просто проверить не вышло.
                # Следующая попытка — по расписанию, без тревожного текста.
                log.info("проверка обновлений не удалась: %s", res.get("message"))
                self.update.update(state="offline", new_version="",
                                   message=f"Не удалось проверить обновления — нет связи с сервером, "
                                           f"повторим позже. Установлена версия {VERSION}")
        except Exception as exc:                       # noqa: BLE001
            log.exception("обновление не удалось")
            self.update.update(state="error", message=f"Обновление не удалось: {exc}")
        return self.update_status()

    def restart(self) -> dict:
        """Перезапуск, чтобы встало скачанное обновление.

        Новая копия стартует через несколько секунд отдельной командой: пока
        старая не закрылась, она заняла бы порт-семафор и просто показала бы
        окно старой.
        """
        if not getattr(sys, "frozen", False):
            return {"ok": False, "message": "Из исходников перезапуск не делается — запустите заново"}
        if not callable(self.request_quit):
            return {"ok": False, "error": "no_quit"}
        # новая копия — только когда эта процесс действительно завершился (не дольше
        # минуты). Раньше ждали 7 с вслепую: закрытие затянулось (разбор .ibt 9 с) —
        # новая копия видела «уже запущен», открывала одно окно, а ядро уходило:
        # «Нет связи с программой» (2026-11-11 16:05)
        exe = sys.executable.replace("'", "''")
        run_later(f"powershell -NoProfile -WindowStyle Hidden -Command \"Wait-Process -Id {os.getpid()} -Timeout 60 "
                  f"-ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500; Start-Process -FilePath '{exe}'\"")
        log.info("перезапуск ради обновления")
        threading.Timer(0.8, self.request_quit).start()
        return {"ok": True, "message": "Программа перезапустится через несколько секунд"}

    def update_install(self) -> dict:
        """Запустить скачанный установщик и выйти.

        Установщик не может заменить работающий exe, поэтому сначала закрываем
        программу, а запуск и обратный старт делает отдельная команда.
        """
        path = Path(self.update.get("installer") or "")
        if not path.is_file():
            return {"ok": False, "message": "Установщик не скачан"}
        exe = sys.executable if getattr(sys, "frozen", False) else ""
        back = f' & start "" "{exe}"' if exe else ""
        run_later(f'ping -n 5 127.0.0.1 >nul & "{path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART{back}')
        log.info("установка обновления: %s", path)
        if callable(self.request_quit):
            threading.Timer(1.0, self.request_quit).start()
        return {"ok": True, "message": "Программа закроется и обновится, потом откроется снова."}

    def _update_loop(self) -> None:
        if self.cfg.get("update_check_on_start"):
            if self._stop.wait(20):            # дать программе подняться
                return
            self._update_once()
        while not self._stop.is_set():
            mins = max(10, int(self.cfg.get("update_check_interval_min", 60) or 60))
            if self._stop.wait(mins * 60):
                return
            self._update_once()

    def _register_bundled(self) -> None:
        """Модули, которых не знает точка входа в exe.

        Список модулей задаёт bootstrap (он внутри exe и меняется только
        установщиком). Новые модули приходят с бандлом — их регистрируем
        здесь, чтобы они появлялись обычным обновлением.
        """
        known = set(self.registry.ids())
        if "telemetry" not in known:
            try:
                from ..modules.telemetry import TelemetryModule
                self.register(TelemetryModule())
            except Exception:
                log.exception("модуль телеметрии не загрузился")
        if "pilot" not in known:
            try:
                from ..modules.pilot import PilotModule
                self.register(PilotModule())
            except Exception:
                log.exception("модуль оверлеев пилота не загрузился")

    def start(self) -> None:
        self._register_bundled()
        self.server.start()
        self.sim.start()
        # модули поднимаются только с допуском; решение примет _license_loop
        if self.license.allowed():
            self._gated = True
            self.registry.start_enabled(self._make_ctx)
        threading.Thread(target=self._license_loop, daemon=True, name="license").start()
        threading.Thread(target=self._broadcast_loop, daemon=True, name="push").start()
        if self.cfg.get("update_url"):
            threading.Thread(target=self._update_loop, daemon=True, name="update").start()
        self.storage.log_event("app_start", {"version": VERSION})
        # Окно открывается при каждом обычном запуске: человек кликнул ярлык и
        # ждёт программу, а не значок в трее. Запуски с ярлыка почему-то видели
        # настройку open_panel_on_start выключенной, хотя в файле она включена;
        # причину ищем по следу ниже, а окно больше от неё не зависит. Только
        # в трее программа стартует лишь с явным ключом --background.
        background = "--background" in sys.argv
        self._trace(f"старт ядра, окно панели при запуске: {not background} "
                    f"(настройка: {self.cfg.get('open_panel_on_start')!r}, "
                    f"конфиг: {config.config_path()}, argv: {sys.argv[1:]})")
        if not background:
            self.open_panel()

    def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._push_state()
            except Exception:
                pass
            time.sleep(1.0)

    def stop(self) -> None:
        self._stop.set()
        if self._deck and self._deck.poll() is None:
            try:
                self._close_panel_tree(self._deck.pid)
            except Exception:
                log.debug("окно пульта не закрылось", exc_info=True)
        if self._overlays and self._overlays.poll() is None:
            try:
                self._close_panel_tree(self._overlays.pid)
            except Exception:
                log.debug("окна оверлеев не закрылись", exc_info=True)
        # Окно панели — отдельный процесс, и само оно о выходе не узнает:
        # закрываем вместе с приложением, иначе останется висеть без сервера.
        if self._panel and self._panel.poll() is None:
            try:
                self._close_panel_tree(self._panel.pid)
            except Exception:
                log.debug("окно панели не закрылось", exc_info=True)
        self.storage.log_event("app_stop", {"version": VERSION})
        self.registry.stop_all()
        self.sim.stop()
        self.server.stop()
        self.storage.close()
