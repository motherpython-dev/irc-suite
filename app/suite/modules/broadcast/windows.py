"""Оверлеи трансляции как окна поверх сима.

Схема с одним монитором: OBS захватывает экран целиком, поэтому в эфир идёт
ровно то, что видит оператор. Браузерные страницы (`/ov/*`) остаются для
конфигурации с двумя машинами, где OBS берёт их источником.

Каждое окно перетаскивается и запоминает своё место; показом управляет пульт.
"""
from __future__ import annotations

import tkinter as tk

from . import style as st
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


class BroadcastHead:
    """Шапка эфирного оверлея: наклонная акцентная плашка.

    Отдельно от `OverlayWindow.head()` намеренно: та же шапка стоит у
    рейс-контроля, а он рабочий инструмент — ему эфирная геометрия не нужна.
    """

    HEAD_H = 28

    def bcast_head(self, title: str, width: int) -> None:
        self.hcanvas = tk.Canvas(self.panel, bg=st.BG, highlightthickness=0,
                                 height=self.HEAD_H, width=width)
        self.hcanvas.pack(fill="x")
        self._hbar = self.hcanvas.create_image(0, 0, anchor="nw")
        self.hcanvas.itemconfigure(self._hbar,
                                   image=st.bar_png(width, self.HEAD_H, st.ACCENT))
        self._hkeep = st.bar_png(width, self.HEAD_H, st.ACCENT)
        self._htitle = self.hcanvas.create_text(
            12, self.HEAD_H // 2, anchor="w", fill="#FFFFFF", text=title.upper(),
            font=("Bahnschrift SemiBold Condensed", 11))
        self.meta = _CanvasText(self.hcanvas, self.hcanvas.create_text(
            width - 12, self.HEAD_H // 2, anchor="e", fill="#FFE8DC", text="",
            font=("Bahnschrift Condensed", 10)))
        self.make_draggable(self.hcanvas)


class _CanvasText:
    """Обёртка, чтобы к тексту на Canvas обращались как к Label."""

    def __init__(self, canvas, item):
        self.canvas, self.item = canvas, item

    def configure(self, **kw):
        if "text" in kw:
            self.canvas.itemconfigure(self.item, text=str(kw["text"]))
        if "fg" in kw:
            self.canvas.itemconfigure(self.item, fill=kw["fg"])

    config = configure


class TowerWindow(OverlayWindow):
    """Пилон позиций.

    Строится на Canvas, а не из Frame с Label: нужен точный контроль над
    наклонными чипами и полосой флага, а Tk-раскладка их не даёт. Элементы
    создаются один раз и потом только переписываются — пересборка на каждом
    кадре мигала бы в эфире.
    """

    WIDTH = 340
    TITLE = "iRC Tower"
    CONFIG_PREFIX = "tower"
    ENTER = "left"        # пилон выезжает от левого края, как на трансляции

    HEAD_H = 30
    ROW_H = 27
    FLAG_W = 5
    CHIP_W = 34

    def build(self) -> None:
        self.canvas = tk.Canvas(self.panel, bg=st.BG, highlightthickness=0,
                                height=self.HEAD_H, width=self.WIDTH)
        self.canvas.pack(fill="both", expand=True)
        self.make_draggable(self.canvas)

        self.f_row = ("Bahnschrift SemiBold Condensed", 11)
        self.f_gap = ("Bahnschrift Condensed", 11)
        self.f_head = ("Bahnschrift SemiBold Condensed", 11)

        self._head_bar = self.canvas.create_image(0, 0, anchor="nw")
        self._head_lap = self.canvas.create_text(
            12, self.HEAD_H // 2, anchor="w", fill="#FFFFFF", font=self.f_head, text="")
        self._head_track = self.canvas.create_text(
            self.WIDTH - 12, self.HEAD_H // 2, anchor="e", fill="#FFE8DC",
            font=("Bahnschrift Condensed", 10), text="")
        self._flag = self.canvas.create_rectangle(
            0, self.HEAD_H, self.FLAG_W, self.HEAD_H, width=0, fill=st.FLAG_COLORS["none"])

        self._rows: list[dict] = []
        self._sig = None
        self._keep: list = []          # ссылки на PhotoImage, иначе их соберёт GC

    # ---------- строка ----------

    def _make_row(self, i: int) -> dict:
        y = self.HEAD_H + i * self.ROW_H
        c = self.canvas
        row = {
            "bg": c.create_rectangle(self.FLAG_W, y, self.WIDTH, y + self.ROW_H,
                                     width=0, fill=st.SURFACE),
            "mark": c.create_rectangle(self.FLAG_W, y, self.FLAG_W, y + self.ROW_H,
                                       width=0, fill=st.ACCENT),
            "pos": c.create_image(self.FLAG_W + 8, y + self.ROW_H // 2, anchor="w"),
            "chip": c.create_image(36, y + self.ROW_H // 2, anchor="w"),
            # номер кладём по центру чипа, иначе двузначные вылезают за край
            "num": c.create_image(36 + self.CHIP_W // 2, y + self.ROW_H // 2,
                                  anchor="center"),
            "name": c.create_text(36 + self.CHIP_W + 14, y + self.ROW_H // 2,
                                  anchor="w",
                                  fill=st.FG, font=self.f_row, text=""),
            "gap": c.create_text(self.WIDTH - 10, y + self.ROW_H // 2, anchor="e",
                                 fill=st.DIM, font=self.f_gap, text=""),
            "y": y,
            "key": None,
        }
        return row

    def _set_image(self, item: int, img) -> None:
        self.canvas.itemconfigure(item, image=img)
        self._keep.append(img)
        if len(self._keep) > 400:          # кэш в style всё равно держит свои
            del self._keep[:200]

    # ---------- обновление ----------

    def refresh(self) -> None:
        data = self.module.tower()
        rows = data["rows"]
        c = self.canvas

        while len(self._rows) < len(rows):
            self._rows.append(self._make_row(len(self._rows)))

        for i, w in enumerate(self._rows):
            items = (w["bg"], w["mark"], w["pos"], w["chip"], w["num"],
                     w["name"], w["gap"])
            if i >= len(rows):
                for it in items:
                    c.itemconfigure(it, state="hidden")
                continue
            for it in items:
                c.itemconfigure(it, state="normal")

            r = rows[i]
            in_pit = r["on_pit"] or r["in_pit_stall"]
            leader = r["position"] == 1
            bg = "#1B2A3A" if in_pit else (st.SURFACE if i % 2 else st.SURFACE_2)
            c.itemconfigure(w["bg"], fill=bg)
            c.coords(w["mark"], self.FLAG_W, w["y"],
                     self.FLAG_W + (3 if leader else 0), w["y"] + self.ROW_H)

            key = (r["position"], r["car_number"], r.get("class_name"), leader)
            if key != w["key"]:
                w["key"] = key
                self._set_image(w["pos"], st.text_png(
                    str(r["position"]), 15, "#FFFFFF" if leader else st.FG, "num"))
                col = st.class_color(r.get("class_name") or "")
                self._set_image(w["chip"], st.chip_png(self.CHIP_W, self.ROW_H - 9, col))
                self._set_image(w["num"], st.text_png(
                    str(r["car_number"]), 12, "#0B0E14", "num"))

            c.itemconfigure(w["name"], text=st.surname(r["name"])[:16],
                            fill="#FFFFFF" if leader else st.FG)
            if leader:
                c.itemconfigure(w["gap"], text="ЛИДЕР", fill=st.ACCENT_2)
            else:
                if data["gap_mode"] == "ahead":
                    prev = next((x for x in rows
                                 if x["position"] == r["position"] - 1), None)
                    d = max(0.0, r["gap"] - prev["gap"]) if prev else r["gap"]
                else:
                    d = r["gap"]
                c.itemconfigure(w["gap"], text=f"+{d:.1f}",
                                fill=st.PIT if in_pit else st.DIM)

        body_h = self.HEAD_H + len(rows) * self.ROW_H + 2
        c.coords(self._flag, 0, self.HEAD_H, self.FLAG_W, body_h)
        c.itemconfigure(self._flag, fill=st.FLAG_COLORS.get(data.get("flag", "none"),
                                                            st.FLAG_COLORS["none"]))

        lap = data.get("lap") or 0
        total = data.get("laps_total") or 0
        c.itemconfigure(self._head_lap,
                        text=f"КРУГ {lap}/{total}" if total else f"КРУГ {lap}")
        c.itemconfigure(self._head_track, text=(data.get("track") or "").upper()[:22])

        sig = (len(rows), data.get("track"))
        if sig != self._sig:
            self._sig = sig
            self._set_image(self._head_bar,
                            st.bar_png(self.WIDTH, self.HEAD_H, st.ACCENT))
            c.configure(height=body_h)
            self.root.geometry(f"{self.WIDTH}x{body_h + 2}")


class GridWindow(BroadcastHead, OverlayWindow):
    """Стартовая решётка: нечётные слева, чётные справа."""

    WIDTH = 560
    TITLE = "iRC Grid"
    CONFIG_PREFIX = "grid"
    ENTER = "up"        # решётка поднимается снизу

    def build(self) -> None:
        self.bcast_head(self.cfg.get("grid_title") or "стартовая решётка", self.WIDTH)
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
        pos = tk.Label(f, width=2, bg=SURFACE, fg=st.DIM, anchor="e",
                       font=("Bahnschrift SemiBold Condensed", 11))
        pos.pack(side="left", padx=(8, 4), pady=3)
        num = tk.Label(f, bg=SURFACE, borderwidth=0)
        num.pack(side="left", padx=2)
        name = tk.Label(f, bg=SURFACE, fg=st.FG, anchor="w",
                        font=("Bahnschrift SemiBold Condensed", 11))
        name.pack(side="left", fill="x", expand=True, padx=(8, 0))
        t = tk.Label(f, bg=SURFACE, fg=st.DIM, anchor="e", width=9,
                     font=("Bahnschrift Condensed", 10))
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
            img = st.num_chip_png(r["car_number"], r.get("class_name") or "", 30, 18)
            c["num"].configure(image=img)
            c["num"].image = img            # иначе картинку соберёт GC
            c["name"].configure(text=st.surname(r["name"])[:16])
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
            self.meta.configure(text=(data.get("track") or "").upper()[:22])
            self.fit()


class CommentatorsWindow(OverlayWindow):
    """Плашка комментаторов: до двух имён."""

    WIDTH = 470
    TITLE = "iRC Commentators"
    CONFIG_PREFIX = "commentators"
    ENTER = "up"        # нижняя треть всегда приходит снизу

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
        """Нижняя плашка: акцентный скос слева, фамилия крупно, роль под ней."""
        f = tk.Frame(self.wrap, bg=st.SURFACE)
        edge = tk.Label(f, bg=st.SURFACE, borderwidth=0)
        img = st.bar_png(10, 46, st.ACCENT)
        edge.configure(image=img)
        edge.image = img
        edge.pack(side="left", fill="y")
        box = tk.Frame(f, bg=st.SURFACE)
        box.pack(side="left", fill="both", expand=True)
        name = tk.Label(box, bg=st.SURFACE, fg="#FFFFFF", anchor="w",
                        font=("Bahnschrift SemiBold Condensed", 15), padx=10)
        name.pack(fill="x", pady=(5, 0))
        role = tk.Label(box, bg=st.SURFACE, fg=st.ACCENT_2, anchor="w",
                        font=("Bahnschrift Condensed", 9), padx=11)
        role.pack(fill="x", pady=(0, 6))
        self.make_draggable(f, box, name, role, edge)
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
            c["name"].configure(text=people[i]["name"].upper())
            c["role"].configure(text=(people[i].get("role") or "").upper())

        cap = data.get("caption", "")
        self.caption.configure(text=cap.upper())
        self.caption.pack_forget()
        if cap:
            self.caption.pack(side="left", pady=(0, 2))
        # плашка нижней трети должна кончаться там же, где текст: пустой
        # хвост окна в эфире выглядит как обрезанная графика
        self.root.update_idletasks()
        w = max(120, self.wrap.winfo_reqwidth())
        self.root.geometry(f"{w}x{max(40, self.wrap.winfo_reqheight())}")


class DeltaWindow(BroadcastHead, OverlayWindow):
    """Разрыв между парой пилотов."""

    WIDTH = 300
    TITLE = "iRC Delta"
    CONFIG_PREFIX = "delta"
    ENTER = "right"        # дельта живёт справа — оттуда и выезжает

    def build(self) -> None:
        self.bcast_head("разрыв", self.WIDTH)
        self.lead = self._car(SURFACE)
        mid = tk.Frame(self.panel, bg=SURFACE)
        mid.pack(fill="x")
        self.value = tk.Label(mid, text="—", bg=SURFACE, fg="#FFFFFF",
                              font=("Bahnschrift SemiBold Condensed", 26))
        self.value.pack(side="left", padx=(14, 8), pady=4)
        self.trend = tk.Label(mid, text="", bg=SURFACE, fg=DIM,
                              font=("Bahnschrift SemiBold Condensed", 10))
        self.trend.pack(side="left")
        self.chase = self._car(SURFACE_2)
        self.hint = tk.Label(self.panel, text="", bg=SURFACE, fg=LIGHT,
                             font=(self.f_ui, 8), pady=4)

    def _car(self, bg) -> dict:
        f = tk.Frame(self.panel, bg=bg)
        f.pack(fill="x")
        pos = tk.Label(f, width=2, bg=bg, fg=st.DIM, anchor="e",
                       font=("Bahnschrift SemiBold Condensed", 11))
        pos.pack(side="left", padx=(8, 4), pady=4)
        num = tk.Label(f, bg=bg, borderwidth=0)
        num.pack(side="left", padx=2)
        name = tk.Label(f, bg=bg, fg=st.FG, anchor="w",
                        font=("Bahnschrift SemiBold Condensed", 12))
        name.pack(side="left", fill="x", expand=True, padx=(8, 8))
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
            img = st.num_chip_png(src["car_number"], src.get("class_name") or "", 30, 19)
            part["num"].configure(image=img)
            part["num"].image = img
            part["name"].configure(text=st.surname(src["name"])[:16])
        self.lead["frame"].pack(before=self.value.master, fill="x")
        self.value.configure(text=f"{d['gap']:.1f}")
        trend = d.get("trend", 0)
        self.trend.configure(
            text="▼ СОКРАЩАЕТСЯ" if trend < 0 else ("▲ РАСТЁТ" if trend > 0
                                                   else "— ДЕРЖИТСЯ"),
            fg=CLOSING if trend < 0 else (GROWING if trend > 0 else DIM))
        self.fit()


class PitsWindow(BroadcastHead, OverlayWindow):
    """Пит-стопы: сколько раз и как долго."""

    WIDTH = 360
    TITLE = "iRC Pits"
    CONFIG_PREFIX = "pits"
    ENTER = "right"        # пит-репорт приходит с той же стороны, что дельта

    def build(self) -> None:
        self.bcast_head(self.cfg.get("pits_title") or "пит-стопы", self.WIDTH)
        self.body = tk.Frame(self.panel, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.empty = tk.Label(self.panel, text="пит-стопов пока не было",
                              bg=BG, fg=DIM, font=(self.f_ui, 9), pady=10)
        self._rows: list[dict] = []
        self._count = -1

    def _row(self) -> dict:
        f = tk.Frame(self.body, bg=SURFACE)
        f.pack(fill="x")
        pos = tk.Label(f, width=2, bg=SURFACE, fg=st.DIM, anchor="e",
                       font=("Bahnschrift SemiBold Condensed", 10))
        pos.pack(side="left", padx=(8, 4), pady=3)
        num = tk.Label(f, bg=SURFACE, borderwidth=0)
        num.pack(side="left", padx=2)
        name = tk.Label(f, bg=SURFACE, fg=st.FG, anchor="w",
                        font=("Bahnschrift SemiBold Condensed", 11))
        name.pack(side="left", fill="x", expand=True, padx=(8, 0))
        cnt = tk.Label(f, width=3, bg=SURFACE, fg="#FFFFFF",
                       font=("Bahnschrift SemiBold Condensed", 12))
        cnt.pack(side="right", padx=(0, 8))
        last = tk.Label(f, width=7, bg=SURFACE, fg=st.DIM, anchor="e",
                        font=("Bahnschrift Condensed", 10))
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
            img = st.num_chip_png(r["car_number"], r.get("class_name") or "", 30, 18)
            w["num"].configure(image=img)
            w["num"].image = img
            w["name"].configure(text=st.surname(r["name"])[:16])
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
