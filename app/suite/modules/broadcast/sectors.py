"""Секторные времена всего поля — считаем сами.

iRacing не отдаёт секторов по чужим машинам. Зато есть разметка трассы
(`SplitTimeInfo.Sectors[].SectorStartPct` в session YAML) и положение каждой
машины на круге (`CarIdxLapDistPct`). Время сектора = момент пересечения
следующей границы минус момент пересечения предыдущей.

Опрос идёт несколько раз в секунду, машина за это время проезжает десятки
метров. Поэтому момент пересечения не берём «как есть» с тика, а
интерполируем между двумя соседними замерами — иначе погрешность была бы
в треть секунды на каждом секторе.

Считается только в LIVE: в повторе CarIdx* показывают кадр повтора, и
пересечения оттуда записались бы как новые.
"""
from __future__ import annotations


class SectorTimer:
    """Текущий круг, прошлый круг и лучшие сектора по каждой машине."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._starts: list[float] = []
        self._cars: dict[int, dict] = {}

    # ---------- приём замеров ----------

    def update(self, starts: list[float], rows: list[dict], t: float) -> None:
        if len(starts) < 2:
            return
        if starts != self._starts:                  # другая трасса или разметка
            self._starts = list(starts)
            self._cars = {}
        n = len(starts)

        for r in rows:
            idx = r["car_idx"]
            p = float(r.get("lap_dist") or 0.0)
            if r.get("not_in_world") or p < 0:
                self._cars.pop(idx, None)
                continue
            s = self._sector_of(p)
            c = self._cars.get(idx)
            if c is None:
                self._cars[idx] = {"p": p, "t": t, "sec": s, "enter": None,
                                   "cur": [None] * n, "last": [None] * n,
                                   "best": [None] * n}
                continue
            if t <= c["t"]:
                continue

            if s != c["sec"]:
                if s == (c["sec"] + 1) % n:
                    tc = self._cross_time(c["p"], c["t"], p, t, self._starts[s])
                    done = c["sec"]
                    if c["enter"] is not None:
                        dur = tc - c["enter"]
                        # отсекаем мусор: телепорт, буксир, выход из боксов
                        if 1.0 < dur < 600.0:
                            dur = round(dur, 3)
                            c["cur"][done] = dur
                            if c["best"][done] is None or dur < c["best"][done]:
                                c["best"][done] = dur
                    c["enter"] = tc
                    if s == 0:                      # закрыт круг
                        c["last"] = c["cur"]
                        c["cur"] = [None] * n
                else:
                    # перескок через сектор — сброс в боксы или телепорт;
                    # время текущего сектора уже не восстановить
                    c["enter"] = None
                c["sec"] = s
            c["p"], c["t"] = p, t

    def _sector_of(self, p: float) -> int:
        s = 0
        for i, start in enumerate(self._starts):
            if p >= start:
                s = i
        return s

    @staticmethod
    def _cross_time(p0: float, t0: float, p1: float, t1: float, b: float) -> float:
        """Момент пересечения границы b между двумя замерами.

        Граница нулевого сектора — это линия старта, и там положение
        проворачивается через 1.0 -> 0.0: путь считаем с учётом оборота.
        """
        if p1 < p0:                                  # проехали линию старта
            total = (1.0 - p0) + p1
            part = (1.0 - p0) + (b if b > 0 else 0.0)
        else:
            total = p1 - p0
            part = b - p0
        frac = part / total if total > 0 else 1.0
        frac = min(1.0, max(0.0, frac))
        return t0 + frac * (t1 - t0)

    # ---------- выдача ----------

    def get(self, car_idx: int, n: int) -> dict:
        c = self._cars.get(car_idx)
        if not c or len(c["cur"]) != n:
            empty = [None] * n
            return {"current": empty, "last": empty, "best": empty}
        return {"current": list(c["cur"]), "last": list(c["last"]),
                "best": list(c["best"])}

    def best_overall(self, n: int) -> list:
        """Лучший сектор по полю — для фиолетовой подсветки."""
        out: list = [None] * n
        for c in self._cars.values():
            for i, v in enumerate(c["best"][:n]):
                if v is not None and (out[i] is None or v < out[i]):
                    out[i] = v
        return out
