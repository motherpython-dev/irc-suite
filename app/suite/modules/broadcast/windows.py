"""Оверлеи трансляции как окна поверх сима.

Схема с одним монитором: OBS захватывает экран целиком, поэтому в эфир идёт
ровно то, что видит оператор. Браузерные страницы (`/ov/*`) остаются для
конфигурации с двумя машинами, где OBS берёт их источником.

Каждое окно перетаскивается и запоминает своё место; показом управляет пульт.
"""
from __future__ import annotations

import tkinter as tk

from ...core.ui.overlay import OverlayWindow
from ...modules.racecontrol.overlay import (
    ACCENT, BG, DIM, FG, LIGHT, LINE, LIVE_C, OFF_TRACK_C, SURFACE, SURFACE_2,
    SURFACE_3,
)

PIT = "#38BDF8"
CLOSING = "#22C55E"
GROWING = "#EF4444"


def fmt_lap(t: float) -> str:
    if not t or t <= 0:
        return "—"
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


class TowerWindow(OverlayWindow):
    """Башня позиций."""

    WIDTH = 330
    TITLE = "iRC Tower"
    CONFIG_PREFIX = "tower"

    def build(self) -> None:
        self.head(self.cfg.get("tower_title") or "позиции")
        self.body = tk.Frame(self.panel, bg=BG)
        self.body.pack(fill="both", expand=True)
        self._rows: list[dict] = []
        self._sig = None

    def _row(self) -> dict:
        r = tk.Frame(self.body, bg=SURFACE)
        r.pack(fill="x")
        pos = tk.Label(r, width=3, bg=SURFACE, fg=FG,
                       font=(self.f_display, 9, "bold"))
        pos.pack(side="left", padx=(8, 2), pady=3)
        num = tk.Label(r, width=4, bg=SURFACE_3, fg=FG,
                       font=(self.f_display, 8, "bold"))
        num.pack(side="left", padx=2, pady=3)
        name = tk.Label(r, bg=SURFACE, fg=FG, anchor="w", font=(self.f_ui, 9))
        name.pack(side="left", fill="x", expand=True, padx=(6, 0))
        gap = tk.Label(r, bg=SURFACE, fg=DIM, anchor="e", width=8,
                       font=(self.f_ui, 9))
        gap.pack(side="right", padx=8)
        return {"frame": r, "pos": pos, "num": num, "name": name, "gap": gap}

    def refresh(self) -> None:
        data = self.module.tower()
        rows = data["rows"]
        # виджеты создаём один раз и переиспользуем: пересборка на каждом кадре
        # заставляла бы строки мигать прямо в эфире
        while len(self._rows) < len(rows):
            self._rows.append(self._row())
        for i, w in enumerate(self._rows):
            if i >= len(rows):
                w["frame"].pack_forget()
                continue
            r = rows[i]
            w["frame"].pack(fill="x")
            in_pit = r["on_pit"] or r["in_pit_stall"]
            bg = "#12263A" if in_pit else (SURFACE if i % 2 else SURFACE_2)
            for key in ("frame", "pos", "name", "gap"):
                w[key].configure(bg=bg)
            w["pos"].configure(text=str(r["position"]))
            w["num"].configure(text=r["car_number"])
            w["name"].configure(text=r["name"][:20])
            if r["position"] == 1:
                w["gap"].configure(text="лидер", fg=LIVE_C)
            elif data["gap_mode"] == "ahead":
                prev = next((x for x in rows if x["position"] == r["position"] - 1), None)
                d = max(0.0, r["gap"] - prev["gap"]) if prev else r["gap"]
                w["gap"].configure(text=f"+{d:.1f}", fg=PIT if in_pit else DIM)
            else:
                w["gap"].configure(text=f"+{r['gap']:.1f}", fg=PIT if in_pit else DIM)

        sig = (len(rows), data.get("track"))
        if sig != self._sig:
            self._sig = sig
            self.meta.configure(text=data.get("track", "")[:22])
            self.fit()


class GridWindow(OverlayWindow):
    """Стартовая решётка: нечётные слева, чётные справа."""

    WIDTH = 560
    TITLE = "iRC Grid"
    CONFIG_PREFIX = "grid"

    def build(self) -> None:
        self.head(self.cfg.get("grid_title") or "стартовая решётка")
        self.body = tk.Frame(self.panel, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.left = tk.Frame(self.body, bg=BG)
        self.left.pack(side="left", fill="both", expand=True)
        self.right = tk.Frame(self.body, bg=BG)
        self.right.pack(side="left", fill="both", expand=True)
        self.foot = tk.Label(self.panel, bg=SURFACE, fg=LIGHT, anchor="w",
                             font=(self.f_ui, 7), padx=10, pady=3)
        self.foot.pack(fill="x")
        self._cells: dict[int, dict] = {}
        self._count = -1

    def _cell(self, parent) -> dict:
        f = tk.Frame(parent, bg=SURFACE)
        f.pack(fill="x", pady=1)
        pos = tk.Label(f, width=3, bg=SURFACE, fg=FG, font=(self.f_display, 9, "bold"))
        pos.pack(side="left", padx=(8, 2), pady=3)
        num = tk.Label(f, width=4, bg=SURFACE_3, fg=FG, font=(self.f_display, 8, "bold"))
        num.pack(side="left", padx=2)
        name = tk.Label(f, bg=SURFACE, fg=FG, anchor="w", font=(self.f_ui, 9))
        name.pack(side="left", fill="x", expand=True, padx=(6, 0))
        t = tk.Label(f, bg=SURFACE, fg=DIM, anchor="e", width=9, font=(self.f_ui, 8))
        t.pack(side="right", padx=8)
        return {"frame": f, "pos": pos, "num": num, "name": name, "time": t}

    def refresh(self) -> None:
        data = self.module.grid()
        rows = data["rows"]
        for i, r in enumerate(rows):
            if i not in self._cells:
                self._cells[i] = self._cell(self.left if i % 2 == 0 else self.right)
            c = self._cells[i]
            c["frame"].pack(fill="x", pady=1)
            c["pos"].configure(text=str(r["position"]))
            c["num"].configure(text=r["car_number"])
            c["name"].configure(text=r["name"][:18])
            if i == 0:
                c["time"].configure(text=fmt_lap(r["time"]), fg=LIVE_C)
            else:
                c["time"].configure(
                    text=(f"+{r['delta']:.3f}" if r.get("delta") else fmt_lap(r["time"])),
                    fg=DIM)
        for i, c in self._cells.items():
            if i >= len(rows):
                c["frame"].pack_forget()

        if len(rows) != self._count:
            self._count = len(rows)
            self.foot.configure(
                text="по результатам квалификации" if data["source"] == "qualifying"
                else "квалификации нет — показан текущий порядок")
            self.meta.configure(text=data.get("track", "")[:22])
            self.fit()


class CommentatorsWindow(OverlayWindow):
    """Плашка комментаторов: до двух имён."""

    WIDTH = 470
    TITLE = "iRC Commentators"
    CONFIG_PREFIX = "commentators"

    def build(self) -> None:
        self.panel.configure(highlightthickness=0)
        self.wrap = tk.Frame(self.panel, bg=BG)
        self.wrap.pack(fill="both", expand=True)
        self.cards: list[dict] = []
        self.caption = tk.Label(self.wrap, text="", bg=ACCENT, fg="#fff",
                                font=(self.f_display, 8, "bold"), padx=10, pady=6)
        self._sig = None
        self.make_draggable(self.panel, self.wrap)

    def _card(self) -> dict:
        f = tk.Frame(self.wrap, bg=SURFACE)
        bar = tk.Frame(f, bg=ACCENT, height=3)
        bar.pack(fill="x")
        name = tk.Label(f, bg=SURFACE, fg=FG, anchor="w",
                        font=(self.f_display, 11, "bold"), padx=12)
        name.pack(fill="x", pady=(6, 0))
        role = tk.Label(f, bg=SURFACE, fg=DIM, anchor="w",
                        font=(self.f_ui, 8), padx=12)
        role.pack(fill="x", pady=(0, 7))
        self.make_draggable(f, name, role)
        return {"frame": f, "name": name, "role": role}

    def refresh(self) -> None:
        data = self.module.commentators()
        people = [p for p in data.get("people", []) if p.get("name")]
        sig = (tuple((p["name"], p.get("role", "")) for p in people),
               data.get("caption", ""))
        if sig == self._sig:
            return
        self._sig = sig

        while len(self.cards) < len(people):
            self.cards.append(self._card())
        for i, c in enumerate(self.cards):
            if i >= len(people):
                c["frame"].pack_forget()
                continue
            c["frame"].pack(side="left", padx=(0, 8))
            c["name"].configure(text=people[i]["name"])
            c["role"].configure(text=(people[i].get("role") or "").upper())

        cap = data.get("caption", "")
        self.caption.configure(text=cap.upper())
        self.caption.pack_forget()
        if cap:
            self.caption.pack(side="left", pady=(0, 2))
        self.fit()


class DeltaWindow(OverlayWindow):
    """Разрыв между парой пилотов."""

    WIDTH = 300
    TITLE = "iRC Delta"
    CONFIG_PREFIX = "delta"

    def build(self) -> None:
        self.head("разрыв")
        self.lead = self._car(SURFACE)
        mid = tk.Frame(self.panel, bg=SURFACE)
        mid.pack(fill="x")
        self.value = tk.Label(mid, text="—", bg=SURFACE, fg=FG,
                              font=(self.f_display, 18, "bold"))
        self.value.pack(side="left", padx=(14, 8), pady=4)
        self.trend = tk.Label(mid, text="", bg=SURFACE, fg=DIM,
                              font=(self.f_ui, 9, "bold"))
        self.trend.pack(side="left")
        self.chase = self._car(SURFACE_2)
        self.hint = tk.Label(self.panel, text="", bg=SURFACE, fg=LIGHT,
                             font=(self.f_ui, 8), pady=4)

    def _car(self, bg) -> dict:
        f = tk.Frame(self.panel, bg=bg)
        f.pack(fill="x")
        pos = tk.Label(f, width=3, bg=bg, fg=DIM, font=(self.f_display, 9, "bold"))
        pos.pack(side="left", padx=(8, 2), pady=4)
        num = tk.Label(f, width=4, bg=SURFACE_3, fg=FG, font=(self.f_display, 8, "bold"))
        num.pack(side="left", padx=2)
        name = tk.Label(f, bg=bg, fg=FG, anchor="w", font=(self.f_ui, 9))
        name.pack(side="left", fill="x", expand=True, padx=(6, 8))
        return {"frame": f, "pos": pos, "num": num, "name": name}

    def refresh(self) -> None:
        d = self.module.delta()
        if not d.get("ready"):
            for part in (self.lead, self.chase):
                part["frame"].pack_forget()
            self.value.configure(text="—")
            self.trend.configure(text="")
            self.hint.configure(text="пара не выбрана" if not d.get("pair")
                                else "ждём машины на трассе")
            self.hint.pack(fill="x")
            self.fit()
            return

        self.hint.pack_forget()
        for part, src in ((self.lead, d["lead"]), (self.chase, d["chase"])):
            part["frame"].pack(fill="x")
            part["pos"].configure(text=str(src["position"]))
            part["num"].configure(text=src["car_number"])
            part["name"].configure(text=src["name"][:20])
        self.lead["frame"].pack(before=self.value.master, fill="x")
        self.value.configure(text=f"{d['gap']:.1f}")
        trend = d.get("trend", 0)
        self.trend.configure(
            text="▼ сокращается" if trend < 0 else ("▲ растёт" if trend > 0 else "— держится"),
            fg=CLOSING if trend < 0 else (GROWING if trend > 0 else DIM))
        self.fit()


class PitsWindow(OverlayWindow):
    """Пит-стопы: сколько раз и как долго."""

    WIDTH = 360
    TITLE = "iRC Pits"
    CONFIG_PREFIX = "pits"

    def build(self) -> None:
        self.head(self.cfg.get("pits_title") or "пит-стопы")
        self.body = tk.Frame(self.panel, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.empty = tk.Label(self.panel, text="пит-стопов пока не было",
                              bg=BG, fg=DIM, font=(self.f_ui, 9), pady=10)
        self._rows: list[dict] = []
        self._count = -1

    def _row(self) -> dict:
        f = tk.Frame(self.body, bg=SURFACE)
        f.pack(fill="x")
        pos = tk.Label(f, width=3, bg=SURFACE, fg=DIM, font=(self.f_display, 8, "bold"))
        pos.pack(side="left", padx=(8, 2), pady=3)
        num = tk.Label(f, width=4, bg=SURFACE_3, fg=FG, font=(self.f_display, 8, "bold"))
        num.pack(side="left", padx=2)
        name = tk.Label(f, bg=SURFACE, fg=FG, anchor="w", font=(self.f_ui, 9))
        name.pack(side="left", fill="x", expand=True, padx=(6, 0))
        cnt = tk.Label(f, width=3, bg=SURFACE, fg=FG, font=(self.f_display, 9, "bold"))
        cnt.pack(side="right", padx=(0, 8))
        last = tk.Label(f, width=7, bg=SURFACE, fg=DIM, anchor="e", font=(self.f_ui, 8))
        last.pack(side="right")
        return {"frame": f, "pos": pos, "num": num, "name": name,
                "cnt": cnt, "last": last}

    def refresh(self) -> None:
        data = self.module.pits()
        rows = data["rows"]
        if not rows:
            for w in self._rows:
                w["frame"].pack_forget()
            self.empty.pack(fill="x")
            if self._count != 0:
                self._count = 0
                self.fit()
            return
        self.empty.pack_forget()

        while len(self._rows) < len(rows):
            self._rows.append(self._row())
        for i, w in enumerate(self._rows):
            if i >= len(rows):
                w["frame"].pack_forget()
                continue
            r = rows[i]
            w["frame"].pack(fill="x")
            bg = "#12263A" if r["in_pit"] else (SURFACE if i % 2 else SURFACE_2)
            for key in ("frame", "pos", "name", "cnt", "last"):
                w[key].configure(bg=bg)
            w["pos"].configure(text=str(r["position"]))
            w["num"].configure(text=r["car_number"])
            w["name"].configure(text=r["name"][:18])
            w["cnt"].configure(text=str(r["count"]))
            w["last"].configure(text=f"{r['last']['duration']:.1f}с" if r["last"] else "—")

        if len(rows) != self._count:
            self._count = len(rows)
            self.fit()


WINDOWS = {
    "tower": TowerWindow,
    "grid": GridWindow,
    "commentators": CommentatorsWindow,
    "delta": DeltaWindow,
    "pits": PitsWindow,
}
