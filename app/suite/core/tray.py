"""Единый значок в трее для всего Suite.

Меню строится из реестра модулей: галочка у каждого включает и выключает его
на лету. Цвет точки на значке — состояние сима (LIVE / повтор / нет связи).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from .config import APP_TITLE, data_dir
from .simclient import MODE_LIVE, MODE_REPLAY
from .version import VERSION

DOT = {MODE_LIVE: (34, 197, 94), MODE_REPLAY: (245, 158, 11)}
OFF = (107, 118, 137)


def _asset(name: str) -> Path | None:
    for base in (Path(__file__).resolve().parent.parent / "assets",
                 Path(getattr(sys, "_MEIPASS", ".")) / "assets",
                 Path(sys.executable).parent / "assets",
                 Path(sys.executable).parent / "_internal" / "assets"):
        p = base / name
        if p.exists():
            return p
    return None


def _base_image() -> Image.Image:
    p = _asset("icon.ico") or _asset("icon.png")
    if p:
        try:
            return Image.open(p).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (11, 14, 20, 255))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), outline=(255, 90, 31, 255), width=5)
    return img


def _icon_for(mode: str) -> Image.Image:
    img = _base_image().copy()
    d = ImageDraw.Draw(img)
    d.ellipse((40, 40, 62, 62), fill=DOT.get(mode, OFF) + (255,),
              outline=(11, 14, 20, 255), width=2)
    return img


class Tray:
    def __init__(self, app):
        self.app = app
        self._mode = None
        self._stopped = threading.Event()
        self.icon = pystray.Icon("irc", _icon_for("OFFLINE"),
                                 APP_TITLE, self._menu())
        app.on_module_change = self.refresh

    # ---------- меню ----------

    def _menu(self) -> pystray.Menu:
        items = [pystray.MenuItem("Открыть панель", lambda: self.app.open_panel(),
                                  default=True)]
        if self._is_on("broadcast"):
            # оверлеи трансляции живут в OBS, но посмотреть их удобно и так
            items.append(pystray.MenuItem("Оверлеи трансляции", pystray.Menu(
                *[pystray.MenuItem(title, (lambda u: lambda: self.app.open_panel(u))(url))
                  for title, url in (("Башня позиций", "/ov/tower"),
                                     ("Стартовая решётка", "/ov/grid"),
                                     ("Комментаторы", "/ov/commentators"),
                                     ("Дельта пилотов", "/ov/delta"),
                                     ("Пит-стопы", "/ov/pits"))])))
        windows = []
        rc = self.app.module("racecontrol")
        if rc and getattr(rc, "overlay", None):
            windows.append(pystray.MenuItem(
                "Оверлей рейс-контроля",
                lambda: rc.overlay.request_toggle(),
                checked=lambda _: rc.overlay_visible()))
        bc = self.app.module("broadcast")
        if bc and getattr(bc, "deck", None):
            windows.append(pystray.MenuItem(
                "Пульт эфира",
                lambda: bc.deck.toggle(),
                checked=lambda _: bool(bc.deck and bc.deck.visible)))
        if windows:
            items.append(pystray.Menu.SEPARATOR)
            items.extend(windows)
        items.append(pystray.Menu.SEPARATOR)
        for st in self.app.registry.statuses():
            mid = st["id"]
            items.append(pystray.MenuItem(
                st["title"],
                (lambda m: lambda: self._toggle(m))(mid),
                checked=(lambda m: lambda _: self._is_on(m))(mid)))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Папка с данными", lambda: os.startfile(str(data_dir()))),
            pystray.MenuItem("Выход", self._quit),
        ]
        return pystray.Menu(*items)

    def _is_on(self, module_id: str) -> bool:
        for st in self.app.registry.statuses():
            if st["id"] == module_id:
                return bool(st["started"])
        return False

    def _toggle(self, module_id: str) -> None:
        res = self.app.set_module(module_id, not self._is_on(module_id))
        if not res.get("ok"):
            self.notify(res.get("message", "Не удалось переключить модуль"))

    def _status_text(self) -> str:
        st = self.app.state()
        if not st["connected"]:
            return f"v{VERSION} · сим не найден"
        return f"v{VERSION} · {st['mode']} · {st['track'] or 'сессия'}"

    def refresh(self) -> None:
        """Перестроить меню — состав галочек изменился."""
        try:
            self.icon.menu = self._menu()
            self.icon.update_menu()
        except Exception:
            pass

    # ---------- значок ----------

    def update_mode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            try:
                self.icon.icon = _icon_for(mode)
            except Exception:
                pass

    def notify(self, text: str, title: str = APP_TITLE) -> None:
        try:
            self.icon.notify(text, title)
        except Exception:
            pass

    # ---------- жизненный цикл ----------

    def _quit(self) -> None:
        from .ui import host
        self.app.stop()
        host.quit_()
        self.icon.stop()
        self._stopped.set()

    def run_detached(self) -> None:
        """Трей в фоне: главный поток нужен Tk."""
        self.icon.run_detached()

    def wait(self) -> None:
        self._stopped.wait()
