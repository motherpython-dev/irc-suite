"""Сеть с запасным путём: через системный прокси и напрямую.

Программы вроде GearUp ставят в Windows системный прокси (у пользователя —
`127.0.0.1:10809` и в настройках, и в HTTP_PROXY/HTTPS_PROXY). Python берёт
его оттуда сам, и все запросы iRC — вход, решения маршала, телеметрия,
обновления — шли через него. Когда прокси не отвечал или рвал соединение,
программа считала, что связи нет, хотя напрямую сайт был доступен.

Теперь запрос, который не смог соединиться одним путём, сразу повторяется
другим, а сработавший путь запоминается для этого сервера. Ответ сервера
с ошибкой (401, 404…) — это ответ, а не обрыв: его не повторяем.

`install()` подменяет общий механизм urllib — так запасной путь получает и
код внутри exe (проверка и скачивание обновлений), который бандлом не
меняется.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

log = logging.getLogger("net")

LOCAL = {"localhost", "127.0.0.1", "::1"}


class FallbackOpener:
    """Совместим с urllib.request.urlopen: у него вызывается только open()."""

    def __init__(self):
        self.system = urllib.request.build_opener()            # прокси как в системе
        self.direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.good: dict[str, str] = {}                         # сервер -> путь, что сработал
        self._lock = threading.Lock()
        self._logged: dict[str, float] = {}

    @staticmethod
    def _proxied(url: str) -> bool:
        """Пойдёт ли этот адрес через прокси по настройкам системы."""
        p = urlparse(url)
        proxies = urllib.request.getproxies()
        if not proxies.get(p.scheme):
            return False
        try:
            return not urllib.request.proxy_bypass(p.hostname or "")
        except Exception:                                      # noqa: BLE001
            return True

    def open(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, prefer: str | None = None):
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
        host = (urlparse(url).hostname or "").lower()
        if host in LOCAL or not self._proxied(url):
            return self.direct.open(fullurl, data, timeout)
        with self._lock:
            first = self.good.get(host) or prefer or "system"
        order = [first, "direct" if first == "system" else "system"]
        err = None
        for way in order:
            try:
                r = (self.system if way == "system" else self.direct).open(_copy(fullurl, data), None, timeout)
            except urllib.error.HTTPError:
                with self._lock:
                    self.good[host] = way        # сервер ответил — путь рабочий
                raise
            except (urllib.error.URLError, OSError) as e:      # не соединились: другой путь
                err = e
                continue
            with self._lock:
                changed = self.good.get(host) != way
                self.good[host] = way
            if changed and way != first:
                self._note(host, way, err)
            return r
        raise err

    def _note(self, host: str, way: str, err) -> None:
        now = time.monotonic()
        if now - self._logged.get(host, 0) > 60:
            self._logged[host] = now
            log.warning("%s: %s не прошёл (%s) — работаю %s", host,
                        "через прокси" if way == "direct" else "напрямую", err,
                        "напрямую" if way == "direct" else "через системный прокси")


def _copy(req, data=None):
    """Чистая копия запроса на каждую попытку: обработчик прокси переписывает
    в объекте запроса адрес на прокси, и повтор «напрямую» с тем же объектом
    снова уходил в прокси."""
    if not isinstance(req, urllib.request.Request):
        return urllib.request.Request(str(req), data=data)
    headers = dict(req.headers)
    headers.update(req.unredirected_hdrs)
    return urllib.request.Request(req.full_url, data=data if data is not None else req.data,
                                  headers=headers, method=req.get_method())


_opener: FallbackOpener | None = None


def opener() -> FallbackOpener:
    global _opener
    if _opener is None:
        _opener = FallbackOpener()
    return _opener


def install() -> None:
    """Подменить общий механизм urllib: urlopen() во всей программе, в том
    числе в коде exe, пойдёт через запасной путь."""
    urllib.request.install_opener(opener())


def urlopen(req, timeout=30, prefer: str | None = None):
    """Как urllib.request.urlopen, но можно попросить сначала путь
    ('direct' или 'system')."""
    return opener().open(req, None, timeout, prefer=prefer)
