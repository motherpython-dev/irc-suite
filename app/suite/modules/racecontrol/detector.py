"""Детект инцидентов по всему полю.

Источник — кумулятивный счётчик Incidents из session YAML (ResultsPositions).
Он приходит от сервера, а не из кадра, поэтому работает и в режиме REPLAY —
именно это позволяет не слепнуть, пока оператор разбирает эпизод.

Контекст "кто был рядом" берётся из кольцевого буфера SimClient и доступен
только в LIVE. В REPLAY карточка помечается estimated=True.
"""
from __future__ import annotations

import threading
import time

from ...core.simclient import MODE_LIVE, MODE_OFFLINE, SimClient
from ...core.storage import Incident, Storage

# irsdk.TrkLoc
TRK_NOT_IN_WORLD, TRK_OFF_TRACK, TRK_ON_TRACK = -1, 0, 3

KIND_INCIDENT = "incident"      # по счётчику инцидентов сессии
KIND_OFFTRACK = "offtrack"      # по геометрии: выезд за пределы трассы


class IncidentDetector:
    def __init__(self, sim: SimClient, storage: Storage, poll_period: float = 1.0,
                 offtrack_enabled: bool = True, min_offtrack_seconds: float = 1.0,
                 offtrack_cooldown: float = 8.0, merge_window: float = 12.0):
        self.sim = sim
        self.storage = storage
        self.poll_period = poll_period
        self.offtrack_enabled = offtrack_enabled
        self.min_offtrack_seconds = min_offtrack_seconds
        self.offtrack_cooldown = offtrack_cooldown
        self.merge_window = merge_window
        self._off_since: dict[int, tuple[float, list[str]]] = {}
        self._off_last: dict[int, float] = {}
        self._counts: dict[int, int] = {}
        self._player_count: int | None = None
        self._subsession = 0
        self._session_key = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_incident = None          # callable(Incident)
        self.missed_in_replay = 0        # счётчик для плашки "пока вы в повторе"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="detector")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.poll_period)

    def _tick(self) -> None:
        snap = self.sim.snapshot()
        if not snap.connected or snap.mode == MODE_OFFLINE:
            return

        # Смена сессии — перечитать базовые счётчики, иначе зальём ленту
        # "инцидентами" за всю уже прошедшую гонку.
        # Ключ — не только SubSessionID: в offline testing он равен 0, и без
        # трека в ключе смена трассы не сбрасывала бы счётчики.
        key = (snap.subsession_id, snap.track, snap.session_num)
        if key != self._session_key:
            self._session_key = key
            self._subsession = snap.subsession_id
            self._counts = dict(self.storage.known_incident_counts(
                self._subsession, snap.track))
            self._player_count = None
            self._off_since.clear()
            self._off_last.clear()

        if self.offtrack_enabled:
            self._tick_offtrack(snap)

        current = self.sim.session_results_incidents()
        if not current:
            # ResultsPositions отсутствует (квалификация, тест-драйв). Свои
            # инциденты — только если мы за рулём: у спектатора своей машины нет
            # и PlayerCarMyIncidentCount всегда 0.
            if not snap.spectator:
                self._tick_player(snap)
            return

        first_seen = not self._counts
        for car_idx, total in current.items():
            prev = self._counts.get(car_idx)
            if prev is None:
                # первое наблюдение машины: не считаем инцидентом, только базлайн
                self._counts[car_idx] = total
                continue
            if total <= prev:
                continue

            delta = total - prev
            self._counts[car_idx] = total
            if first_seen:
                continue

            self._emit(car_idx, delta, total, snap)

    def _tick_offtrack(self, snap) -> None:
        """Выезды за пределы трассы по CarIdxTrackSurface — для ВСЕГО поля.

        Зачем отдельный источник: ResultsPositions есть не всегда (в
        квалификации iRacing их не отдаёт, в тест-драйве тоже), а спектатору
        источник «свои инциденты» вообще недоступен — своей машины нет.
        Геометрия же доступна всегда и по каждой машине.

        Фиксируем момент СЪЕЗДА (это и есть якорь для перемотки), а карточку
        выпускаем при возвращении на трассу — когда известна длительность и
        отсеяны касания травы на доли секунды.
        """
        if snap.mode != MODE_LIVE:
            return                       # в повторе TrackSurface показывает прошлое
        surfaces = self.sim.track_surfaces()
        if not surfaces:
            return
        now, _ = self.sim.live_session_time()

        for car_idx, loc in surfaces.items():
            if loc == TRK_NOT_IN_WORLD:
                self._off_since.pop(car_idx, None)
                continue
            if loc == TRK_OFF_TRACK:
                self._off_since.setdefault(car_idx, (now, self.sim.nearby_cars(car_idx)))
                continue

            started = self._off_since.pop(car_idx, None)
            if started is None or loc != TRK_ON_TRACK:
                continue                 # пит-лейн выездом не считаем

            t0, nearby = started
            duration = max(0.0, now - t0)
            if duration < self.min_offtrack_seconds:
                continue                 # клипнул поребрик — не событие
            if now - self._off_last.get(car_idx, -1e9) < self.offtrack_cooldown:
                continue                 # серия выездов подряд -> одна карточка
            self._off_last[car_idx] = now
            self._emit(car_idx, 0, 0, snap, kind=KIND_OFFTRACK,
                       session_time=t0, nearby=nearby, duration=duration)

    def _tick_player(self, snap) -> None:
        """Запасной источник: PlayerCarMyIncidentCount.

        Единственный рабочий вариант там, где нет результатов сессии — в
        тест-драйве и на ранней практике. Видит только свою машину, зато
        обновляется мгновенно, без задержки YAML.
        """
        total = snap.player_incidents
        idx = snap.player_car_idx
        if idx < 0:
            return
        if self._player_count is None:
            self._player_count = total          # базлайн, не инцидент
            return
        if total <= self._player_count:
            return
        delta = total - self._player_count
        self._player_count = total
        self._emit(idx, delta, total, snap)

    def _emit(self, car_idx: int, delta: int, total: int, snap,
              kind: str = KIND_INCIDENT, session_time: float | None = None,
              nearby: list[str] | None = None, duration: float = 0.0) -> None:
        st, estimated = self.sim.live_session_time()
        session_time = st if session_time is None else session_time
        drv = self.sim.driver(car_idx)
        if nearby is None:
            nearby = self.sim.nearby_cars(car_idx) if snap.mode == MODE_LIVE else []

        inc = Incident(
            session_num=snap.session_num,
            session_time=round(session_time, 3),
            car_idx=car_idx,
            car_number=drv.car_number,
            driver_name=drv.name,
            lap=self.sim.laps_completed(car_idx),
            inc_delta=delta,
            inc_total=total,
            nearby=nearby,
            estimated=estimated,
            subsession_id=snap.subsession_id,
            track=snap.track,
            kind=kind,
            duration=round(duration, 2),
        )
        # Один и тот же эпизод видят оба источника: геометрия ловит выезд
        # сразу, счётчик инцидентов приходит из YAML на секунду-две позже.
        # Без слияния в гонке на каждый вылет было бы по две карточки.
        prev = self.storage.find_recent(inc.subsession_id, inc.track, car_idx,
                                        inc.session_time, self.merge_window)
        if prev is not None and prev.id is not None:
            if kind == KIND_INCIDENT and prev.kind == KIND_OFFTRACK:
                # счётчик точнее: досыпаем вес в карточку выезда
                self.storage.merge_into(prev.id, delta, total)
                self.storage.log_event("incident_merged",
                                       {"into": prev.id, "delta": delta})
                if self.on_incident:
                    try:
                        self.on_incident(prev)
                    except Exception:
                        pass
                return
            if kind == KIND_OFFTRACK:
                return                   # эпизод уже зафиксирован

        inc = self.storage.add_incident(inc)
        self.storage.log_event("incident_detected", inc.to_dict())

        if estimated:
            self.missed_in_replay += 1

        if self.on_incident:
            try:
                self.on_incident(inc)
            except Exception:
                pass

    def reset_missed(self) -> None:
        self.missed_in_replay = 0
