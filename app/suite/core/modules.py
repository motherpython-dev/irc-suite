"""Реестр модулей Suite.

Модуль — включаемая часть приложения (рейс-контрол, трансляционные оверлеи,
RUTP, телеметрия). Все они живут в ОДНОМ процессе и делят общие ресурсы:
один клиент irSDK, один HTTP-сервер, один трей, одно хранилище.

Почему один процесс: irSDK читает shared memory сима. Несколько процессов
означали бы кратный опрос, свои копии session YAML и — главное — дублирование
логики переподключения к залипшему буферу (клиент, поднятый до сима,
без этого остаётся на мёртвых данных).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class Context:
    """Всё, что модуль получает от ядра. Своего SDK и сервера он не создаёт."""
    sim: Any                       # SimClient — снимки состояния сима
    rc: Any                        # RaceControl — broadcast-команды (камеры, реплей)
    storage: Any                   # слой хранения
    publish: Callable[[str, dict], None]   # отправить событие в SSE
    config: dict                   # раздел конфига этого модуля
    save_config: Callable[[], None]
    log: logging.Logger
    app: Any = None                # ссылка на Application (для общих действий)


class Module(Protocol):
    id: str
    title: str

    def start(self, ctx: Context) -> None: ...
    def stop(self) -> None: ...
    def status(self) -> dict: ...
    def routes(self) -> dict: ...


@dataclass
class Entry:
    module: Any
    enabled: bool = False
    started: bool = False
    error: str = ""
    ctx: Context | None = None


class Registry:
    """Хранит модули, включает и выключает их на лету."""

    def __init__(self, log: logging.Logger | None = None):
        self._items: dict[str, Entry] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self.log = log or logging.getLogger("modules")

    def add(self, module: Any, enabled: bool = False) -> None:
        with self._lock:
            self._items[module.id] = Entry(module=module, enabled=enabled)
            self._order.append(module.id)

    def get(self, module_id: str) -> Any | None:
        e = self._items.get(module_id)
        return e.module if e else None

    def ids(self) -> list[str]:
        return list(self._order)

    def start_enabled(self, make_ctx: Callable[[str], Context]) -> None:
        for mid in self._order:
            if self._items[mid].enabled:
                self.start(mid, make_ctx)

    def start(self, module_id: str, make_ctx: Callable[[str], Context]) -> dict:
        with self._lock:
            e = self._items.get(module_id)
            if not e:
                return {"ok": False, "error": "unknown_module"}
            if e.started:
                return {"ok": True, "already": True}
            try:
                e.ctx = make_ctx(module_id)
                e.module.start(e.ctx)
                e.started, e.enabled, e.error = True, True, ""
                self.log.info("модуль %s запущен", module_id)
                return {"ok": True}
            except Exception as exc:            # noqa: BLE001 — показываем в UI
                e.error = str(exc)
                e.started = False
                self.log.exception("модуль %s не стартовал", module_id)
                return {"ok": False, "error": "start_failed", "message": str(exc)}

    def stop(self, module_id: str) -> dict:
        with self._lock:
            e = self._items.get(module_id)
            if not e:
                return {"ok": False, "error": "unknown_module"}
            if not e.started:
                e.enabled = False
                return {"ok": True, "already": True}
            try:
                e.module.stop()
            except Exception:
                self.log.exception("модуль %s упал при остановке", module_id)
            e.started, e.enabled = False, False
            self.log.info("модуль %s остановлен", module_id)
            return {"ok": True}

    def stop_all(self) -> None:
        for mid in reversed(self._order):
            if self._items[mid].started:
                self.stop(mid)

    def statuses(self) -> list[dict]:
        out = []
        for mid in self._order:
            e = self._items[mid]
            row = {"id": mid, "title": e.module.title,
                   "enabled": e.enabled, "started": e.started, "error": e.error}
            if e.started:
                try:
                    row.update(e.module.status() or {})
                except Exception as exc:        # noqa: BLE001
                    row["error"] = str(exc)
            out.append(row)
        return out

    def routes(self) -> dict:
        """Собрать HTTP-роуты всех ЗАПУЩЕННЫХ модулей.

        Ключ — путь целиком, чтобы модуль сам решал, где ему жить
        (`/api/racecontrol/...`, `/ov/tower` и т.п.).
        """
        out: dict = {}
        for mid in self._order:
            e = self._items[mid]
            if not e.started:
                continue
            try:
                out.update(e.module.routes() or {})
            except Exception:
                self.log.exception("модуль %s не отдал роуты", mid)
        return out
