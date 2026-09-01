"""Хозяин Tk для всего приложения.

Tk требует главного потока и единственного корневого окна на процесс. Модули
же включаются и выключаются на лету, поэтому корень создаётся один раз при
старте и живёт до выхода, а окна модулей — это `Toplevel` поверх него.

Корень скрыт: видимых окон у Suite нет, пока модуль не откроет своё.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk

_root: tk.Tk | None = None
_calls: queue.Queue = queue.Queue()


def root() -> tk.Tk:
    global _root
    if _root is None:
        raise RuntimeError("Tk-хост не запущен: сначала вызови start()")
    return _root


def is_running() -> bool:
    return _root is not None


def start() -> tk.Tk:
    """Создать корень. Только из главного потока."""
    global _root
    if _root is not None:
        return _root
    _root = tk.Tk()
    _root.withdraw()
    _root.title("iRC")
    _pump()
    return _root


def call(fn) -> None:
    """Выполнить функцию в потоке Tk.

    Модули включаются из трея, а трей крутится в своём потоке; создавать и
    уничтожать виджеты оттуда нельзя — Tk не потокобезопасен. Поэтому вызов
    кладётся в очередь и исполняется насосом главного потока.
    """
    _calls.put(fn)


def _pump() -> None:
    try:
        while True:
            fn = _calls.get_nowait()
            try:
                fn()
            except Exception:
                import logging
                logging.getLogger("ui").exception("вызов в потоке Tk упал")
    except queue.Empty:
        pass
    if _root is not None:
        _root.after(50, _pump)


def run() -> None:
    """Блокирует главный поток до quit()."""
    if _root is not None:
        _root.mainloop()


def quit_() -> None:
    global _root
    if _root is not None:
        try:
            _root.quit()
        except Exception:
            pass
