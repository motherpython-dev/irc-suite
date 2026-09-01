"""Слой хранения. Изолирован намеренно: пилот пишет в SQLite, боевая версия
будет писать в ichamps через HTTP — меняется только реализация Storage.

Ничего выше по стеку не должно знать, что внутри SQLite.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class Incident:
    """Один зафиксированный инцидент. session_time — якорь для перемотки."""
    session_num: int
    session_time: float
    car_idx: int
    car_number: str
    driver_name: str
    lap: int
    inc_delta: int
    inc_total: int
    lap_dist_pct: float = 0.0
    nearby: list[str] = field(default_factory=list)
    estimated: bool = False          # якорь оценочный (пойман в режиме REPLAY)
    kind: str = "incident"           # incident | offtrack
    duration: float = 0.0            # для offtrack: сколько секунд был вне трассы
    subsession_id: int = 0
    track: str = ""
    id: int | None = None
    reviewed: int = 0
    note: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Storage(Protocol):
    """Контракт слоя хранения. Реализации: SQLiteStorage (пилот), позже IChampsStorage."""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def add_incident(self, inc: Incident) -> Incident: ...
    def list_incidents(self, session_num: int | None = None, limit: int = 200,
                       subsession_id: int | None = None,
                       track: str | None = None) -> list[Incident]: ...
    def last_session_key(self) -> tuple[int, str] | None: ...
    def get_incident(self, incident_id: int) -> Incident | None: ...
    def find_recent(self, subsession_id: int, track: str, car_idx: int,
                    session_time: float, window: float) -> Incident | None: ...
    def merge_into(self, incident_id: int, inc_delta: int, inc_total: int) -> None: ...
    def find_recent(self, subsession_id: int, track: str, car_idx: int,
                    session_time: float, window: float) -> Incident | None:
        """Событие той же машины рядом по времени — кандидат на слияние."""
        with self._lock:
            r = self._c().execute(
                """SELECT * FROM incidents
                   WHERE subsession_id=? AND track=? AND car_idx=?
                     AND ABS(session_time - ?) <= ?
                   ORDER BY id DESC LIMIT 1""",
                (subsession_id, track, car_idx, session_time, window)).fetchone()
            return self._row_to_inc(r) if r else None

    def merge_into(self, incident_id: int, inc_delta: int, inc_total: int) -> None:
        """Досыпать вес в существующую карточку вместо создания дубля."""
        with self._lock:
            self._c().execute(
                "UPDATE incidents SET inc_delta=inc_delta+?, inc_total=?, "
                "kind='incident' WHERE id=?",
                (inc_delta, inc_total, incident_id))
            self._c().commit()

    def set_reviewed(self, incident_id: int, reviewed: bool, note: str = "") -> None: ...
    def known_incident_counts(self, subsession_id: int, track: str = "") -> dict[int, int]: ...
    def log_event(self, kind: str, payload: dict[str, Any]) -> None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subsession_id INTEGER NOT NULL DEFAULT 0,
    session_num   INTEGER NOT NULL,
    session_time  REAL    NOT NULL,
    car_idx       INTEGER NOT NULL,
    car_number    TEXT    NOT NULL,
    driver_name   TEXT    NOT NULL,
    lap           INTEGER NOT NULL,
    inc_delta     INTEGER NOT NULL,
    inc_total     INTEGER NOT NULL,
    lap_dist_pct  REAL    NOT NULL DEFAULT 0,
    nearby        TEXT    NOT NULL DEFAULT '[]',
    estimated     INTEGER NOT NULL DEFAULT 0,
    kind          TEXT    NOT NULL DEFAULT 'incident',
    duration      REAL    NOT NULL DEFAULT 0,
    track         TEXT    NOT NULL DEFAULT '',
    reviewed      INTEGER NOT NULL DEFAULT 0,
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inc_session ON incidents(subsession_id, session_num);

-- append-only лог: всё, что произошло, включая наши команды в сим
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- последний известный счётчик инцидентов по машине: переживает рестарт клиента,
-- чтобы после перезапуска не залить ленту "новыми" инцидентами за всю гонку.
-- Трек в ключе обязателен: в offline testing SubSessionID всегда 0, и без него
-- базлайн от прошлого тест-драйва (напр. 7x) утекал бы в новый, где счётчик
-- стартует с нуля — детект молчал бы, пока пилот не наберёт больше 7.
CREATE TABLE IF NOT EXISTS inc_counters (
    subsession_id INTEGER NOT NULL,
    track         TEXT    NOT NULL DEFAULT '',
    car_idx       INTEGER NOT NULL,
    inc_total     INTEGER NOT NULL,
    PRIMARY KEY (subsession_id, track, car_idx)
);
"""

# Схема inc_counters сменила первичный ключ. Базлайн — величина восстановимая
# (снимется заново при первом тике), поэтому миграция сводится к пересозданию.
MIGRATIONS = [
    ("inc_counters_track",
     ["DROP TABLE IF EXISTS inc_counters",
      """CREATE TABLE inc_counters (
             subsession_id INTEGER NOT NULL,
             track         TEXT    NOT NULL DEFAULT '',
             car_idx       INTEGER NOT NULL,
             inc_total     INTEGER NOT NULL,
             PRIMARY KEY (subsession_id, track, car_idx)
         )"""]),
    ("incident_kind",
     ["ALTER TABLE incidents ADD COLUMN kind TEXT NOT NULL DEFAULT 'incident'",
      "ALTER TABLE incidents ADD COLUMN duration REAL NOT NULL DEFAULT 0"]),
]


class SQLiteStorage:
    """Пилотная реализация. Один файл, WAL, потокобезопасна через один лок."""

    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self._c().execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Догнать схему до актуальной.

        На ЧИСТОЙ базе таблицы уже создаются полными по SCHEMA, поэтому
        миграции, добавляющие колонки, выполнять нельзя — SQLite ответит
        `duplicate column name` и приложение не стартует. Поэтому каждая
        миграция сначала проверяется на применимость и, если её работа уже
        сделана, просто помечается выполненной.
        """
        c = self._c()
        c.execute("CREATE TABLE IF NOT EXISTS migrations ("
                  "name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))")
        done = {r["name"] for r in c.execute("SELECT name FROM migrations")}

        # что должно существовать, чтобы миграцию можно было считать выполненной
        already = {
            "incident_kind": lambda: "kind" in self._columns("incidents"),
            "inc_counters_track": lambda: "track" in self._columns("inc_counters"),
        }

        for name, statements in MIGRATIONS:
            if name in done:
                continue
            check = already.get(name)
            if check and check():
                c.execute("INSERT INTO migrations (name) VALUES (?)", (name,))
                continue
            for sql in statements:
                c.execute(sql)
            c.execute("INSERT INTO migrations (name) VALUES (?)", (name,))
        c.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("storage is not open")
        return self._conn

    def add_incident(self, inc: Incident) -> Incident:
        with self._lock:
            cur = self._c().execute(
                """INSERT INTO incidents (subsession_id, session_num, session_time, car_idx,
                       car_number, driver_name, lap, inc_delta, inc_total, lap_dist_pct,
                       nearby, estimated, track, kind, duration)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (inc.subsession_id, inc.session_num, inc.session_time, inc.car_idx,
                 inc.car_number, inc.driver_name, inc.lap, inc.inc_delta, inc.inc_total,
                 inc.lap_dist_pct, json.dumps(inc.nearby, ensure_ascii=False),
                 1 if inc.estimated else 0, inc.track, inc.kind, inc.duration),
            )
            self._c().execute(
                """INSERT INTO inc_counters (subsession_id, track, car_idx, inc_total)
                   VALUES (?,?,?,?)
                   ON CONFLICT(subsession_id, track, car_idx)
                   DO UPDATE SET inc_total=excluded.inc_total""",
                (inc.subsession_id, inc.track, inc.car_idx, inc.inc_total),
            )
            self._c().commit()
            inc.id = cur.lastrowid
            return inc

    def _row_to_inc(self, r: sqlite3.Row) -> Incident:
        return Incident(
            id=r["id"], subsession_id=r["subsession_id"], session_num=r["session_num"],
            session_time=r["session_time"], car_idx=r["car_idx"], car_number=r["car_number"],
            driver_name=r["driver_name"], lap=r["lap"], inc_delta=r["inc_delta"],
            inc_total=r["inc_total"], lap_dist_pct=r["lap_dist_pct"],
            nearby=json.loads(r["nearby"]), estimated=bool(r["estimated"]),
            track=r["track"], reviewed=r["reviewed"], note=r["note"],
            kind=(r["kind"] if "kind" in r.keys() else "incident"),
            duration=(r["duration"] if "duration" in r.keys() else 0.0),
            created_at=r["created_at"],
        )

    def list_incidents(self, session_num: int | None = None, limit: int = 200,
                       subsession_id: int | None = None,
                       track: str | None = None) -> list[Incident]:
        where, params = [], []
        if subsession_id is not None:
            where.append("subsession_id=?")
            params.append(subsession_id)
        if track is not None:
            where.append("track=?")
            params.append(track)
        if session_num is not None:
            where.append("session_num=?")
            params.append(session_num)
        sql = "SELECT * FROM incidents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._c().execute(sql, tuple(params)).fetchall()
            return [self._row_to_inc(r) for r in rows]

    def last_session_key(self) -> tuple[int, str] | None:
        """(subsession_id, track) последнего записанного инцидента.
        Нужен, чтобы после закрытия сима лента показывала прошлую сессию,
        а не всё подряд."""
        with self._lock:
            r = self._c().execute(
                "SELECT subsession_id, track FROM incidents ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return (r["subsession_id"], r["track"]) if r else None

    def get_incident(self, incident_id: int) -> Incident | None:
        with self._lock:
            r = self._c().execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            return self._row_to_inc(r) if r else None

    def find_recent(self, subsession_id: int, track: str, car_idx: int,
                    session_time: float, window: float) -> Incident | None:
        """Событие той же машины рядом по времени — кандидат на слияние."""
        with self._lock:
            r = self._c().execute(
                """SELECT * FROM incidents
                   WHERE subsession_id=? AND track=? AND car_idx=?
                     AND ABS(session_time - ?) <= ?
                   ORDER BY id DESC LIMIT 1""",
                (subsession_id, track, car_idx, session_time, window)).fetchone()
            return self._row_to_inc(r) if r else None

    def merge_into(self, incident_id: int, inc_delta: int, inc_total: int) -> None:
        """Досыпать вес в существующую карточку вместо создания дубля."""
        with self._lock:
            self._c().execute(
                "UPDATE incidents SET inc_delta=inc_delta+?, inc_total=?, "
                "kind='incident' WHERE id=?",
                (inc_delta, inc_total, incident_id))
            self._c().commit()

    def set_reviewed(self, incident_id: int, reviewed: bool, note: str = "") -> None:
        with self._lock:
            self._c().execute("UPDATE incidents SET reviewed=?, note=? WHERE id=?",
                              (1 if reviewed else 0, note, incident_id))
            self._c().commit()

    def known_incident_counts(self, subsession_id: int,
                              track: str = "") -> dict[int, int]:
        """Счётчики на момент прошлого запуска — база для дельт после рестарта."""
        with self._lock:
            rows = self._c().execute(
                "SELECT car_idx, inc_total FROM inc_counters "
                "WHERE subsession_id=? AND track=?",
                (subsession_id, track)).fetchall()
            return {r["car_idx"]: r["inc_total"] for r in rows}

    def log_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._c().execute("INSERT INTO events (kind, payload) VALUES (?,?)",
                              (kind, json.dumps(payload, ensure_ascii=False)))
            self._c().commit()
