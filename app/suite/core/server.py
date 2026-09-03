"""HTTP + SSE сервер — общий для всех модулей iRC.

Только stdlib: чем меньше зависимостей, тем меньше поводов сборке сломаться.

По умолчанию слушает 127.0.0.1. Режиссёру часто нужен пульт с телефона —
тогда сервер поднимается на всех интерфейсах (`bind_all`), и адрес в локальной
сети показывается в панели. Наружу за роутер это не выходит, но в своей сети
управление доступно любому, кто знает адрес: пароля здесь нет.

Роуты бывают двух видов:
  * ядра — состояние, модули, обновления;
  * модулей — регистрируются динамически при включении модуля
    (`/api/racecontrol/...`, `/ov/tower` и т.п.).
Оверлеи трансляции отдаются отсюда же и подключаются в OBS как browser source.
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


class Hub:
    """Раздаёт события всем открытым SSE-соединениям."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event: str, data: dict) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            targets = list(self._clients)
        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


class Api:
    """Что панель просит у ЯДРА. Всё остальное дают роуты модулей."""

    def state(self) -> dict: ...
    def drivers(self) -> list[dict]: ...
    def find_drivers(self, query: str, limit: int = 8) -> list[dict]: ...
    def set_module(self, module_id: str, enabled: bool) -> dict: ...
    def set_lan(self, allow: bool) -> dict: ...


def make_handler(api: Api, hub: Hub, dynamic=None):
    """`dynamic()` возвращает {путь: обработчик} от запущенных модулей.

    Вызывается на каждый запрос: набор роутов меняется, когда модуль включают
    или выключают на лету.
    """
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):      # тишина в консоли
            pass

        # ---------- helpers ----------
        def _json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str):
            try:
                body = path.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        # ---------- routes ----------
        def _dynamic(self, path: str, method: str):
            if not dynamic:
                return False
            handler = dynamic().get(f"{method} {path}") or dynamic().get(path)
            if not handler:
                return False
            try:
                result = handler(self)
            except Exception as exc:            # noqa: BLE001
                self._json({"ok": False, "error": "handler_failed",
                            "message": str(exc)}, 500)
                return True
            if isinstance(result, tuple):       # (тело, content-type)
                body, ctype = result
                data = body if isinstance(body, bytes) else str(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif result is not None:
                self._json(result)
            return True

        def do_GET(self):
            p = self.path.split("?")[0]
            if self._dynamic(p, "GET"):
                return
            if p in ("/", "/index.html"):
                return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            if p == "/assets/logo.png":
                return self._file(ASSETS_DIR / "logo-irc.png", "image/png")
            # Окно панели — это Chrome в режиме --app, и значок в панели задач
            # он берёт из фавиконки страницы. Без неё висит серый глобус.
            if p == "/favicon.ico":
                return self._file(ASSETS_DIR / "icon.ico", "image/x-icon")
            if p == "/assets/icon.png":
                return self._file(ASSETS_DIR / "icon-256.png", "image/png")
            if p == "/deck":
                return self._file(WEB_DIR / "deck.html", "text/html; charset=utf-8")
            if p == "/racecontrol":
                return self._file(WEB_DIR / "racecontrol.html", "text/html; charset=utf-8")
            if p == "/api/state":
                return self._json(api.state())

            if p == "/api/drivers":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                return self._json(api.find_drivers(q))

            if p == "/api/stream":
                return self._stream()
            self.send_error(404)

        def do_POST(self):
            p = self.path.split("?")[0]
            if self._dynamic(p, "POST"):
                return
            body = self._body()
            if p == "/api/panel":
                # повторный запуск программы просит показать окно панели
                api.open_panel()
                return self._json({"ok": True})
            if p == "/api/lan":
                return self._json(api.set_lan(bool(body.get("allow"))))
            if p == "/api/modules/set":
                return self._json(api.set_module(str(body.get("id", "")),
                                                 bool(body.get("enabled"))))
            self.send_error(404)

        def _stream(self):
            q = hub.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = ": ping\n\n"          # держим соединение живым
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def lan_ip() -> str:
    """Адрес машины в локальной сети — для телефона.

    Определяем через UDP-сокет к внешнему адресу: пакет никуда не уходит,
    но система выбирает интерфейс, через который пошла бы отправка.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class PanelServer:
    def __init__(self, api: Api, hub: Hub, port: int, dynamic=None,
                 bind_all: bool = False):
        self.hub = hub
        self.port = port
        self.bind_all = bind_all
        host = "0.0.0.0" if bind_all else "127.0.0.1"
        self._httpd = ThreadingHTTPServer((host, port),
                                          make_handler(api, hub, dynamic))
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def lan_url(self) -> str:
        """Адрес для телефона. Пусто, если сервер слушает только локально."""
        return f"http://{lan_ip()}:{self.port}/" if self.bind_all else ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="http")
        self._thread.start()

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
