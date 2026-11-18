"""Два цвета ливреи Trading Paints — для плашки номера в оверлеях пилота.

RUTP кладёт ливреи в папку paint: `<paint>/<CarPath>/car_<UserID>.tga` (в
командных гонках — `car_team_<TeamID>.tga`). Сим о них не знает: в данных
сессии только цвета из конструктора iRacing. Здесь по текстуре берутся два
цвета: основной (больше всего площади) и второй — самый заметный из тех, что
отличаются оттенком хотя бы на ACCENT_HUE°. Соседние тона одного градиента
считаются одним цветом.

Что не краска: пустые места развёртки (фон шаблона — самый частый цвет по краю
текстуры), чёрное, белое и серое (шины, стёкла, надписи).

Считается в фоне один раз на файл (путь + время изменения) — текстура 2048×2048
читается ~0,2 с, кадр окна ждать не должен.
"""
from __future__ import annotations

import colorsys
import logging
import os
import queue
import threading
import time
from collections import Counter
from pathlib import Path

log = logging.getLogger("irc.livery")

ACCENT_HUE = 60          # второй цвет — не ближе по оттенку, °
SAME_HUE = 20            # тона в ±20° — один цвет (градиент)
STAT_EVERY = 10.0        # как часто перепроверять, появился ли файл, с


def extract(path: str | Path) -> tuple[str, str] | None:
    """(основной, второй) — #RRGGBB; None, если краски не нашлось."""
    from PIL import Image
    im = Image.open(path).convert("RGB").resize((256, 256), Image.BOX)
    W = 256
    px = im.load()
    edge = [px[i, 0] for i in range(W)] + [px[i, W - 1] for i in range(W)] + \
           [px[0, i] for i in range(W)] + [px[W - 1, i] for i in range(W)]
    bg = Counter((r // 8, g // 8, b // 8) for r, g, b in edge).most_common(1)[0][0]
    bg = tuple(x * 8 + 4 for x in bg)
    bins: dict[int, list] = {}
    for y in range(W):
        for x in range(W):
            r, g, b = px[x, y]
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) < 40:
                continue                                   # фон шаблона
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if v < 0.22 or s < 0.30:
                continue                                   # чёрное, белое, серое
            bins.setdefault(int(h * 36) % 36, []).append((r, g, b))
    if not bins:
        return None

    def dist(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)
    groups = []
    for k in sorted(bins, key=lambda k: -len(bins[k])):
        hue = k * 10
        if any(dist(hue, h0) < ACCENT_HUE for h0, _ in groups):
            continue
        grp = [c for kk, cs in bins.items() if dist(kk * 10, hue) <= SAME_HUE for c in cs]
        groups.append((hue, grp))
        if len(groups) == 2:
            break
    groups.sort(key=lambda g: -len(g[1]))                  # основной — больше площади
    cols = []
    for _, grp in groups:
        grp.sort(key=sum)
        cols.append("#%02X%02X%02X" % grp[len(grp) // 2])  # медиана: среднее градиента «грязное»
    return (cols[0], cols[1] if len(cols) > 1 else cols[0])


class LiveryColors:
    """Кэш цветов ливрей. get() отвечает сразу: готово — цвета, нет — None,
    а файл ставится в очередь фонового потока."""

    def __init__(self) -> None:
        self._done: dict[tuple, tuple | None] = {}       # (путь, mtime) -> цвета
        self._stat: dict[str, tuple[float, float | None]] = {}   # путь -> (когда смотрели, mtime)
        self._q: queue.Queue = queue.Queue()
        self._queued: set = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._work, daemon=True, name="livery-colors").start()

    def _mtime(self, path: str) -> float | None:
        now = time.monotonic()
        hit = self._stat.get(path)
        if hit and now - hit[0] < STAT_EVERY:
            return hit[1]
        try:
            m = os.path.getmtime(path)
        except OSError:
            m = None
        self._stat[path] = (now, m)
        return m

    def get(self, folder: str, car_path: str, user_id: int = 0, team_id: int = 0) -> tuple | None:
        if not folder or not car_path:
            return None
        base = Path(folder) / car_path
        names = ([f"car_team_{team_id}.tga"] if team_id else []) + ([f"car_{user_id}.tga"] if user_id else [])
        for name in names:
            p = str(base / name)
            m = self._mtime(p)
            if m is None:
                continue
            key = (p, m)
            with self._lock:
                if key in self._done:
                    return self._done[key]
                if key not in self._queued:
                    self._queued.add(key)
                    self._q.put(key)
            return None
        return None

    def _work(self) -> None:
        while True:
            key = self._q.get()
            try:
                res = extract(key[0])
            except Exception:                              # noqa: BLE001 — битый файл, чужой формат
                log.warning("цвета ливреи не считаны: %s", key[0])
                res = None
            with self._lock:
                self._done[key] = res
                self._queued.discard(key)
