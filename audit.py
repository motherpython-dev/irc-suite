"""Аудит полноты детекта: сравнение нашей ленты с маркерами инцидентов iRacing.

Идея: сим сам расставляет маркеры в ленте повтора на каждый контакт —
независимо от нашего детектора и от того, смотрел ли оператор. Пройдя по ним
через `replay_search(next_incident)`, получаем ЭТАЛОННЫЙ список моментов, с
которым можно сравнить то, что поймали мы.

ЗАПУСКАТЬ ПОСЛЕ ГОНКИ: скрипт перематывает повтор и мешает смотреть live.

    python audit.py                 полный аудит текущей сессии
    python audit.py --markers-only  только собрать маркеры, без сравнения
    python audit.py --offline       разбор ленты без сима (сессия уже закрыта)

ВАЖНО про ленту повтора: она живёт, только пока сессия открыта. Вышел из
сессии, не сохранив реплей — маркеры потеряны, эталона больше нет. Чтобы аудит
был возможен, при выходе надо согласиться сохранить реплей (askToSaveOnQuit),
а потом открыть .rpy в симе и запустить audit.py по нему.

Что считаем:
  покрытие (recall)  — доля маркеров iRacing, попавших в наши карточки
  лишние             — наши карточки без маркера рядом (не обязательно ошибка:
                       выезд в одиночку сим маркером не отмечает)

Ограничение эталона: маркеры ставятся на КОНТАКТЫ. Одиночный вылет без касания
в них может не попасть, поэтому "лишние" карточки — не всегда ложные
срабатывания. Для полноты картины скрипт дополнительно сверяет суммы инцидентов
по пилотам из итоговых результатов сессии.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import irsdk

MATCH_WINDOW = 8.0        # сек: маркер и карточка считаются одним эпизодом
MAX_MARKERS = 800         # предохранитель от бесконечного обхода
SETTLE = 0.35             # пауза, чтобы сим успел перемотать


def db_path() -> str:
    return os.path.expandvars(r"%LOCALAPPDATA%\ichamps\racecontrol\racecontrol.sqlite3")


def connect_sim() -> irsdk.IRSDK:
    ir = irsdk.IRSDK()
    if not ir.startup() or not ir.is_connected:
        print("iRacing не запущен или сессия не активна.")
        sys.exit(1)
    return ir


def read_pos(ir: irsdk.IRSDK) -> tuple[int, float, int]:
    ir.freeze_var_buffer_latest()
    try:
        return (int(ir["SessionNum"] or 0),
                float(ir["SessionTime"] or 0.0),
                int(ir["ReplayFrameNum"] or 0))
    finally:
        ir.unfreeze_var_buffer_latest()


def collect_markers(ir: irsdk.IRSDK) -> list[tuple[int, float]]:
    """Обойти ленту по собственным маркерам инцидентов iRacing."""
    print("перематываю в начало ленты...")
    ir.replay_set_play_speed(0)
    ir.replay_search(irsdk.RpySrchMode.to_start)
    time.sleep(1.2)

    markers: list[tuple[int, float]] = []
    last_frame = -1
    for i in range(MAX_MARKERS):
        ir.replay_search(irsdk.RpySrchMode.next_incident)
        time.sleep(SETTLE)
        num, t, frame = read_pos(ir)
        if frame == last_frame:
            break                      # дальше маркеров нет
        last_frame = frame
        markers.append((num, t))
        print(f"  маркер {len(markers):3d}: сессия {num}, время {t:8.1f}", flush=True)
    return markers


def load_cards(subsession_id: int, track: str) -> list[dict]:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM incidents WHERE subsession_id=? AND track=? ORDER BY session_time",
        (subsession_id, track)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def results_incidents(ir: irsdk.IRSDK) -> dict[str, int]:
    """Итоговые инциденты по пилотам из результатов сессии."""
    si = ir["SessionInfo"] or {}
    di = ir["DriverInfo"] or {}
    names = {d.get("CarIdx"): (d.get("CarNumber"), d.get("UserName"))
             for d in (di.get("Drivers") or [])}
    out: dict[str, int] = {}
    for s in (si.get("Sessions") or []):
        for p in (s.get("ResultsPositions") or []):
            idx, inc = p.get("CarIdx"), p.get("Incidents")
            if idx is None or not inc:
                continue
            num, name = names.get(idx, ("?", "?"))
            out[f"#{num} {name}"] = max(out.get(f"#{num} {name}", 0), int(inc))
    return out


def compare(markers: list[tuple[int, float]], cards: list[dict]) -> None:
    matched_markers, matched_cards = set(), set()
    for mi, (mnum, mt) in enumerate(markers):
        for ci, c in enumerate(cards):
            if c["session_num"] != mnum:
                continue
            if abs(c["session_time"] - mt) <= MATCH_WINDOW:
                matched_markers.add(mi)
                matched_cards.add(ci)

    print("\n" + "=" * 64)
    print("РЕЗУЛЬТАТ АУДИТА")
    print("=" * 64)
    print(f"маркеров iRacing (контакты) : {len(markers)}")
    print(f"наших карточек              : {len(cards)}")
    if markers:
        rec = 100.0 * len(matched_markers) / len(markers)
        print(f"покрытие маркеров           : {len(matched_markers)}/{len(markers)}  ({rec:.0f}%)")
    print(f"карточек с маркером рядом   : {len(matched_cards)}/{len(cards)}")

    missed = [m for i, m in enumerate(markers) if i not in matched_markers]
    if missed:
        print(f"\nПРОПУЩЕНО эпизодов: {len(missed)}")
        for num, t in missed[:25]:
            print(f"   сессия {num}, время {t:8.1f}  — маркер есть, карточки нет")
        if len(missed) > 25:
            print(f"   ... и ещё {len(missed) - 25}")

    extra = [c for i, c in enumerate(cards) if i not in matched_cards]
    if extra:
        print(f"\nБЕЗ МАРКЕРА: {len(extra)} карточек "
              f"(одиночные вылеты сим маркером не отмечает — это ожидаемо)")
        for c in extra[:15]:
            print(f"   [{c['kind']:8s}] #{c['car_number']:<5} время {c['session_time']:8.1f}")


def offline_report() -> None:
    """Что поймали, без сравнения с эталоном: сим не нужен."""
    import collections, json
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM incidents ORDER BY id")]
    con.close()
    if not rows:
        print("лента пуста")
        return
    groups = collections.OrderedDict()
    for r in rows:
        groups.setdefault((r["subsession_id"], r["track"], r["session_num"]), []).append(r)
    for (sub, track, num), items in groups.items():
        kinds = collections.Counter(i["kind"] for i in items)
        ctx = sum(1 for i in items if i["nearby"] not in ("[]", "", None))
        weight = sum(i["inc_delta"] for i in items)
        print(f"\n=== {track[:40]} | сессия {num} | sub={sub} ===")
        print(f"карточек {len(items)} | источники {dict(kinds)} | "
              f"контекст {ctx}/{len(items)} | вес {weight}x")
        for i in items:
            near = json.loads(i["nearby"]) if i["nearby"] else []
            extra = (f"вне трассы {i['duration']:.0f}с" if i["kind"] == "offtrack"
                     else f"+{i['inc_delta']}x")
            print(f"  t={i['session_time']:7.1f} [{i['kind']:8s}] #{i['car_number']:<4} "
                  f"{i['driver_name'][:22]:22s} {extra:15s} "
                  f"рядом={','.join('#' + n for n in near) or '—'}")


def main() -> None:
    if "--offline" in sys.argv:
        offline_report()
        return
    ir = connect_sim()
    w = ir["WeekendInfo"] or {}
    sub = int(w.get("SubSessionID") or 0)
    track = str(w.get("TrackDisplayName") or "")
    print(f"сессия: {track}  SubSessionID={sub}\n")

    markers = collect_markers(ir)
    if "--markers-only" in sys.argv:
        return

    cards = load_cards(sub, track)
    compare(markers, cards)

    res = results_incidents(ir)
    if res:
        print("\nитоговые инциденты по пилотам (из результатов сессии):")
        for who, inc in sorted(res.items(), key=lambda kv: -kv[1])[:15]:
            mine = sum(c["inc_delta"] for c in cards
                       if f"#{c['car_number']} " in who)
            print(f"   {who:<32} итог {inc:3d}x   у нас {mine:3d}x")

    print("\nвозврат к live")
    ir.replay_search(irsdk.RpySrchMode.to_end)
    ir.replay_set_play_speed(1)


if __name__ == "__main__":
    main()
