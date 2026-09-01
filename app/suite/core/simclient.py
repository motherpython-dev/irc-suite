"""Обёртка над irSDK: режимы LIVE / REPLAY / RECONCILE, якорь времени, кольцевой буфер.

Главная идея — см. skills/iracing-irsdk/references/broadcast-race-control.md:
в реплее per-frame каналы (CarIdx*, SessionTime) отдают кадр повтора, а не live,
поэтому им нельзя верить. Живое время сессии в этот момент считается арифметикой
от якоря, снятого в последний достоверный LIVE-тик.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:
    import irsdk
except ImportError:  # чтобы модуль импортировался на машине без SDK (тесты/CI)
    irsdk = None

MODE_OFFLINE = "OFFLINE"
MODE_LIVE = "LIVE"
MODE_REPLAY = "REPLAY"


@dataclass
class Driver:
    car_idx: int
    car_number: str
    name: str
    user_id: int = 0
    team: str = ""


@dataclass
class Snapshot:
    """То, что отдаётся в UI. Только безопасные для текущего режима поля."""
    mode: str = MODE_OFFLINE
    connected: bool = False
    session_num: int = 0
    session_time: float = 0.0        # в REPLAY — оценочное живое время
    session_time_estimated: bool = False
    replay_frames_to_end: int = 0
    session_tick: int = 0
    player_car_idx: int = -1
    player_incidents: int = 0
    results_available: bool = False   # есть ли ResultsPositions (в тест-драйве нет)
    spectator: bool = False
    session_state: int = 0
    session_flags: int = 0
    track: str = ""
    subsession_id: int = 0
    drivers: list[Driver] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)


class SimClient:
    """Опрос сима в фоновом потоке. Наружу — потокобезопасный snapshot()."""

    def __init__(self, poll_hz: float = 5.0, live_frame_threshold: int = 60,
                 live_time_tolerance: float = 3.0):
        self.poll_interval = 1.0 / poll_hz
        # Оставлено для совместимости с конфигом; в определении режима НЕ участвует.
        self.live_frame_threshold = live_frame_threshold
        # Допуск в секундах для определения "мы смотрим настоящее".
        self.live_time_tolerance = live_time_tolerance
        self._max_session_time = 0.0
        self._stale_ticks = 0          # тиков подряд с мёртвой телеметрией
        self._last_tick = None

        self.ir = irsdk.IRSDK() if irsdk else None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._snap = Snapshot()
        self._anchor: tuple[float, float] | None = None   # (session_time, monotonic)
        self._ring: deque[tuple[float, list[float], list[int]]] = deque(maxlen=1800)
        self._drivers: dict[int, Driver] = {}
        self._session_info: dict[str, Any] | None = None
        self._session_info_driver: dict[str, Any] | None = None
        self._session_info_ts = 0.0
        self.on_mode_change = None      # callable(old, new)

    # ---------- жизненный цикл ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="simclient")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.ir:
            try:
                self.ir.shutdown()
            except Exception:
                pass

    # ---------- публичное ----------

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def live_session_time(self) -> tuple[float, bool]:
        """(время сессии, оценочное ли). В REPLAY считается от якоря."""
        with self._lock:
            if self._snap.mode == MODE_LIVE:
                return self._snap.session_time, False
            if self._anchor:
                t0, w0 = self._anchor
                return t0 + (time.monotonic() - w0), True
            return 0.0, True

    def track_length_m(self) -> float:
        """Длина круга в метрах. В YAML приходит строкой вида '25.1764 km'."""
        w = getattr(self, "_weekend", None) or {}
        raw = str(w.get("TrackLength") or "")
        try:
            value = float(raw.split()[0])
        except (ValueError, IndexError):
            return 0.0
        return value * 1000.0 if "km" in raw.lower() else value * 1609.34

    def nearby_cars(self, car_idx: int, window: float | None = None,
                    meters: float = 120.0) -> list[str]:
        """Кто был рядом по дистанции круга в последнем достоверном LIVE-кадре.

        Радиус задаётся в МЕТРАХ и переводится в долю круга по длине трассы.
        Фиксированная доля не годится: 0.6% круга — это 151 м на Нордшляйфе
        (25.2 км) и всего 10 м на Phoenix (1.6 км), то есть на коротких трассах
        соседей не нашлось бы почти никогда.

        В REPLAY кольцевой буфер не пополняется — вернётся пусто, это нормально.
        """
        if window is None:
            length = self.track_length_m()
            window = (meters / length) if length > 0 else 0.006
        with self._lock:
            if not self._ring:
                return []
            _, dist, _ = self._ring[-1]
            if car_idx >= len(dist):
                return []
            me = dist[car_idx]
            if me is None or me < 0:
                return []
            out = []
            for idx, d in enumerate(dist):
                if idx == car_idx or d is None or d < 0:
                    continue
                if idx not in self._drivers:
                    continue
                gap = abs(d - me)
                gap = min(gap, 1.0 - gap)          # через линию старт/финиш
                if gap <= window:
                    out.append(self._drivers[idx].car_number)
            return out

    def session_results_incidents(self) -> dict[int, int]:
        """{car_idx: incident_count} из session YAML. Работает в ОБОИХ режимах."""
        info = self._session_info
        if not info:
            return {}
        try:
            sessions = info.get("Sessions") or []
            num = self._snap.session_num
            cur = None
            for s in sessions:
                if s.get("SessionNum") == num:
                    cur = s
                    break
            if cur is None and sessions:
                cur = sessions[-1]
            if not cur:
                return {}
            out = {}
            for p in (cur.get("ResultsPositions") or []):
                idx = p.get("CarIdx")
                inc = p.get("Incidents")
                if idx is not None and inc is not None:
                    out[int(idx)] = int(inc)
            return out
        except Exception:
            return {}

    def laps_completed(self, car_idx: int) -> int:
        info = self._session_info
        try:
            for s in ((info or {}).get("Sessions") or []):
                if s.get("SessionNum") != self._snap.session_num:
                    continue
                for p in (s.get("ResultsPositions") or []):
                    if p.get("CarIdx") == car_idx:
                        return int(p.get("LapsComplete") or 0)
        except Exception:
            pass
        # ResultsPositions нет (тест-драйв) — берём круг из телеметрии,
        # иначе в карточке инцидента вечно стоял бы "круг 0"
        try:
            if car_idx == self._snap.player_car_idx:
                return int(self.ir["Lap"] or 0)
            laps = self.ir["CarIdxLap"] or []
            if 0 <= car_idx < len(laps):
                return max(0, int(laps[car_idx]))
        except Exception:
            pass
        return 0

    def is_spectator(self) -> bool:
        """У спектатора нет своей машины: PlayerCarIdx указывает на фиктивный
        слот, а PlayerCarMyIncidentCount всегда 0. Значит источник «свои
        инциденты» для него бессмысленен."""
        try:
            di = self._session_info_driver or {}
            idx = di.get("DriverCarIdx")
            for d in (di.get("Drivers") or []):
                if d.get("CarIdx") == idx:
                    return str(d.get("IsSpectator", 0)) == "1"
        except Exception:
            pass
        return False

    def track_surfaces(self) -> dict[int, int]:
        """{car_idx: TrkLoc} для машин, которые есть в списке пилотов.

        Единственный источник, который видит ВСЁ поле независимо от того,
        отдаёт ли сессия ResultsPositions (в квалификации — не отдаёт).
        """
        try:
            surf = self.ir["CarIdxTrackSurface"] or []
        except Exception:
            return {}
        out = {}
        for idx in self._drivers:
            if 0 <= idx < len(surf):
                out[idx] = int(surf[idx])
        return out

    def camera_groups(self) -> dict[str, int]:
        """{'TV1': 2, 'Nose': 1, ...} из session YAML CameraInfo."""
        try:
            ci = self.ir["CameraInfo"] or {}
            return {str(g.get("GroupName")): int(g.get("GroupNum"))
                    for g in (ci.get("Groups") or [])
                    if g.get("GroupName") and g.get("GroupNum") is not None}
        except Exception:
            return {}

    def resolve_camera_group(self, name: str) -> int | None:
        """Имя группы -> номер. Регистр не важен; None, если не нашли."""
        if not name:
            return None
        if str(name).isdigit():
            return int(name)
        groups = self.camera_groups()
        low = {k.lower(): v for k, v in groups.items()}
        return low.get(str(name).strip().lower())

    def driver(self, car_idx: int) -> Driver:
        return self._drivers.get(car_idx, Driver(car_idx, "?", f"CarIdx {car_idx}"))

    # ---------- внутреннее ----------

    def _telemetry_stale(self, seconds: float = 3.0) -> bool:
        """Буфер телеметрии не обновляется, хотя подключение считается живым.

        Признак — застывший SessionTick: он инкрементируется каждый кадр, пока
        сим реально пишет телеметрию.
        """
        try:
            tick = self.ir["SessionTick"]
        except Exception:
            tick = None
        if tick is None:
            self._stale_ticks += 1
        elif tick == self._last_tick:
            self._stale_ticks += 1
        else:
            self._last_tick = tick
            self._stale_ticks = 0
        return self._stale_ticks * self.poll_interval > seconds

    def _reconnect(self) -> None:
        try:
            self.ir.shutdown()
        except Exception:
            pass
        self._stale_ticks = 0
        self._last_tick = None
        try:
            if not self.ir.startup():
                self._set_mode(MODE_OFFLINE, connected=False)
        except Exception:
            self._set_mode(MODE_OFFLINE, connected=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # сим нестабилен на стыке сессий — переживаем молча
                pass
            time.sleep(self.poll_interval)

    def _tick(self) -> None:
        if not self.ir:
            return
        if not (self.ir.is_initialized and self.ir.is_connected):
            if not self.ir.startup():
                self._set_mode(MODE_OFFLINE, connected=False)
                self._stale_ticks = 0
                return

        if self._telemetry_stale():
            # Клиент, поднятый ДО запуска сима (автозапуск с Windows —
            # обычное дело), цепляется к shared memory, но остаётся на мёртвом
            # буфере телеметрии: session YAML при этом читается свежий, а
            # SessionTime/SessionNum стоят на нуле. Лечится переподключением.
            self._reconnect()
            return

        self.ir.freeze_var_buffer_latest()
        try:
            frames_to_end = self.ir["ReplayFrameNumEnd"]
            frames_to_end = 0 if frames_to_end is None else int(frames_to_end)

            session_num = int(self.ir["SessionNum"] or 0)
            session_time = float(self.ir["SessionTime"] or 0.0)
            session_state = int(self.ir["SessionState"] or 0)
            session_flags = int(self.ir["SessionFlags"] or 0)
            player_idx = int(self.ir["PlayerCarIdx"] if self.ir["PlayerCarIdx"]
                             is not None else -1)
            player_inc = int(self.ir["PlayerCarMyIncidentCount"] or 0)

            # Режим — по остатку ленты до конца.
            #
            # ВАЖНО (проверено на живой спектейт-сессии): SessionTime НЕ
            # замирает при перемотке повтора — он продолжает идти вперёд как
            # живое время сессии, пока кадр ленты уходит назад:
            #     live:              time=529.7  frame=54792  toEnd=1
            #     после перемотки:   time=534.3  frame=28182  toEnd=26884
            # Поэтому определять режим по отставанию времени нельзя — оно
            # никогда не отстаёт, и режим всегда показывался бы LIVE.
            # Рабочий признак — ReplayFrameNumEnd: на конце ленты он ~0-1,
            # при уходе назад растёт.
            if session_num != getattr(self, "_last_session_num", session_num):
                self._max_session_time = 0.0          # новая сессия — счёт заново
            self._last_session_num = session_num

            is_live = frames_to_end <= self.live_frame_threshold
            self._max_session_time = max(self._max_session_time, session_time)

            if is_live:
                # достоверный кадр: обновляем якорь и кольцевой буфер
                self._anchor = (session_time, time.monotonic())
                dist = list(self.ir["CarIdxLapDistPct"] or [])
                surf = list(self.ir["CarIdxTrackSurface"] or [])
                with self._lock:
                    self._ring.append((session_time, dist, surf))
                est_time, estimated = session_time, False
            else:
                # per-frame каналы врут — время считаем от якоря
                est_time, estimated = self.live_session_time()

            self._refresh_session_info()

            with self._lock:
                self._snap = Snapshot(
                    mode=MODE_LIVE if is_live else MODE_REPLAY,
                    connected=True,
                    session_num=session_num,
                    session_time=est_time,
                    session_time_estimated=estimated,
                    replay_frames_to_end=frames_to_end,
                    session_tick=int(self.ir["SessionTick"] or 0),
                    player_car_idx=player_idx,
                    player_incidents=player_inc,
                    results_available=bool(self.session_results_incidents()),
                    spectator=self.is_spectator(),
                    session_state=session_state,
                    session_flags=session_flags,
                    track=self._track_name(),
                    subsession_id=self._subsession_id(),
                    drivers=list(self._drivers.values()),
                    positions=[],
                )
            self._notify_mode(self._snap.mode)
        finally:
            self.ir.unfreeze_var_buffer_latest()

    def _refresh_session_info(self, period: float = 1.0) -> None:
        now = time.monotonic()
        if now - self._session_info_ts < period:
            return
        self._session_info_ts = now
        try:
            self._session_info = self.ir["SessionInfo"]
            di = self.ir["DriverInfo"] or {}
            self._session_info_driver = di
            drivers = {}
            for d in (di.get("Drivers") or []):
                idx = d.get("CarIdx")
                if idx is None:
                    continue
                # пейс-кар и пустые слоты пропускаем
                if str(d.get("CarIsPaceCar", 0)) == "1":
                    continue
                drivers[int(idx)] = Driver(
                    car_idx=int(idx),
                    car_number=str(d.get("CarNumber") or "?"),
                    name=str(d.get("UserName") or "?"),
                    user_id=int(d.get("UserID") or 0),
                    team=str(d.get("TeamName") or ""),
                )
            if drivers:
                self._drivers = drivers
            self._weekend = self.ir["WeekendInfo"] or {}
        except Exception:
            pass

    def _track_name(self) -> str:
        w = getattr(self, "_weekend", None) or {}
        return str(w.get("TrackDisplayName") or w.get("TrackName") or "")

    def _subsession_id(self) -> int:
        w = getattr(self, "_weekend", None) or {}
        try:
            return int(w.get("SubSessionID") or 0)
        except Exception:
            return 0

    def _set_mode(self, mode: str, connected: bool) -> None:
        with self._lock:
            self._snap = Snapshot(mode=mode, connected=connected)
        self._notify_mode(mode)

    _last_mode = MODE_OFFLINE

    def _notify_mode(self, mode: str) -> None:
        if mode != self._last_mode:
            old, self._last_mode = self._last_mode, mode
            if self.on_mode_change:
                try:
                    self.on_mode_change(old, mode)
                except Exception:
                    pass
