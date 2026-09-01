"""Пульт трансляции — окно поверх iRacing.

Зачем оно вообще. Оператор стримит в полноэкранном симе: OBS свёрнут, панель
iRC — обычное окно, которого тоже не видно. Значит узнать, что сейчас в эфире,
и убрать графику можно только свернув сим прямо во время гонки.

Поэтому статус эфира и переключатели живут поверх сима — как оверлей
рейс-контроля. Зелёная точка = оверлей в эфире.

Окно наследует те же три свойства, без которых оверлей поверх сима бесполезен:
topmost, WS_EX_NOACTIVATE (не забирает фокус у сима) и цвет-ключ для
кликопрозрачного фона. Подробности — в core/ui.
"""
from __future__ import annotations

import tkinter as tk

from ...modules.racecontrol.overlay import (
    ACCENT, BG, DIM, FG, LIGHT, LINE, LIVE_C, OFF_C, SURFACE, SURFACE_2,
    SURFACE_3, TRANSPARENT, RADIUS_CARD, _apply_no_activate, apply_round_corners,
    load_logo, pick_font, register_bundled_fonts, rounded_png,
)


class BroadcastDeck:
    WIDTH = 300

    def __init__(self, module, host_root: tk.Misc | None = None):
        self.module = module
        self.visible = True
        self._rows: dict[str, dict] = {}
        self._drag = (0, 0)
        self._keep: list = []

        register_bundled_fonts()
        self.f_display = pick_font("Rubik", "Inter")
        self.f_ui = pick_font("Inter", "Rubik")

        cfg = module.ctx.config
        self.root = tk.Toplevel(host_root) if host_root is not None else tk.Tk()
        self.root.withdraw()
        self.root.title("iRC Broadcast Deck")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(cfg.get("deck_alpha", 0.96)))
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass

        x = int(cfg.get("deck_x", 470))
        y = int(cfg.get("deck_y", 24))
        self.root.geometry(f"{self.WIDTH}x300+{x}+{y}")

        self._build()
        self.root.deiconify()
        self.root.update_idletasks()
        self._fit()
        _apply_no_activate(self.root)
        apply_round_corners(self.root)
        self._tick()

    # ---------- построение ----------

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=TRANSPARENT)
        outer.pack(fill="both", expand=True)
        self.panel = tk.Frame(outer, bg=BG, highlightthickness=1,
                              highlightbackground=LINE)
        self.panel.pack(fill="both", expand=True)

        head = tk.Frame(self.panel, bg=SURFACE, cursor="fleur")
        head.pack(fill="x")
        for w in (head,):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self._logo = load_logo(20)
        logo = (tk.Label(head, image=self._logo, bg=SURFACE, bd=0) if self._logo
                else tk.Label(head, text="iRC", bg=ACCENT, fg="#fff",
                              font=(self.f_display, 8, "bold")))
        logo.pack(side="left", padx=(9, 8), pady=8)
        logo.bind("<Button-1>", self._drag_start)
        logo.bind("<B1-Motion>", self._drag_move)

        self.lbl_title = tk.Label(head, text="Э Ф И Р", bg=SURFACE, fg=DIM,
                                  font=(self.f_display, 8, "bold"))
        self.lbl_title.pack(side="left")
        self.lbl_title.bind("<Button-1>", self._drag_start)
        self.lbl_title.bind("<B1-Motion>", self._drag_move)

        self.lbl_meta = tk.Label(head, text="", bg=SURFACE, fg=LIGHT,
                                 font=(self.f_ui, 8))
        self.lbl_meta.pack(side="left", padx=8)

        tk.Button(head, text="✕", command=self.hide, bg=SURFACE, fg=DIM, bd=0,
                  activebackground=SURFACE_3, activeforeground=FG,
                  font=(self.f_ui, 9), cursor="hand2").pack(side="right", padx=6)

        self.list = tk.Frame(self.panel, bg=BG)
        self.list.pack(fill="both", expand=True, padx=6, pady=6)

        foot = tk.Frame(self.panel, bg=SURFACE)
        foot.pack(fill="x", side="bottom")
        tk.Label(foot, text="зелёная точка — в эфире", bg=SURFACE, fg=LIGHT,
                 font=(self.f_ui, 7)).pack(side="left", padx=10, pady=6)
        self.lbl_hint = tk.Label(foot, text="", bg=SURFACE, fg=LIGHT,
                                 font=(self.f_ui, 7))
        self.lbl_hint.pack(side="right", padx=10)

    def _row(self, parent, oid: str, title: str) -> dict:
        row = tk.Frame(parent, bg=SURFACE_2, cursor="hand2")
        row.pack(fill="x", pady=2)

        dot = tk.Canvas(row, width=12, height=12, bg=SURFACE_2,
                        highlightthickness=0)
        dot.pack(side="left", padx=(10, 8), pady=8)
        did = dot.create_oval(1, 1, 11, 11, fill=OFF_C, outline="")

        lbl = tk.Label(row, text=title, bg=SURFACE_2, fg=FG, anchor="w",
                       font=(self.f_ui, 9))
        lbl.pack(side="left", fill="x", expand=True)

        state = tk.Label(row, text="скрыт", bg=SURFACE_2, fg=LIGHT,
                         font=(self.f_ui, 8))
        state.pack(side="right", padx=10)

        widgets = (row, dot, lbl, state)
        for w in widgets:
            w.bind("<Button-1>", lambda e, i=oid: self._toggle(i))
            w.bind("<Enter>", lambda e, ws=widgets: self._hover(ws, True))
            w.bind("<Leave>", lambda e, ws=widgets: self._hover(ws, False))
        return {"row": row, "dot": dot, "dot_id": did, "state": state,
                "widgets": widgets}

    @staticmethod
    def _hover(widgets, on: bool) -> None:
        bg = SURFACE_3 if on else SURFACE_2
        for w in widgets:
            try:
                w.configure(bg=bg)
            except Exception:
                pass

    # ---------- действия ----------

    def _toggle(self, overlay_id: str) -> None:
        now = self.module.is_visible(overlay_id)
        self.module.set_visible(overlay_id, not now)
        self._refresh()

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")
        cfg = self.module.ctx.config
        cfg["deck_x"], cfg["deck_y"] = x, y

    # ---------- показ ----------

    def show(self) -> None:
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        _apply_no_activate(self.root)
        apply_round_corners(self.root)

    def hide(self) -> None:
        self.visible = False
        self.root.withdraw()

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def stop(self) -> None:
        try:
            self.module.ctx.save_config()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- обновление ----------

    def _fit(self) -> None:
        try:
            self.root.update_idletasks()
            h = self.panel.winfo_reqheight()
            self.root.geometry(f"{self.WIDTH}x{h}")
        except Exception:
            pass

    def _tick(self) -> None:
        if self.visible:
            try:
                self._refresh()
            except Exception:
                pass
        self.root.after(700, self._tick)

    def _refresh(self) -> None:
        from . import OVERLAYS
        if not self._rows:
            for oid, title in OVERLAYS:
                self._rows[oid] = self._row(self.list, oid, title)
            self._fit()

        live = 0
        for oid, _ in OVERLAYS:
            r = self._rows[oid]
            on = self.module.is_visible(oid)
            live += 1 if on else 0
            r["dot"].itemconfigure(r["dot_id"], fill=LIVE_C if on else OFF_C)
            r["state"].configure(text="в эфире" if on else "скрыт",
                                 fg=LIVE_C if on else LIGHT)

        st = self.module.ctx.app.state()
        self.lbl_title.configure(text="Э Ф И Р", fg=LIVE_C if live else DIM)
        self.lbl_meta.configure(
            text=f"{live} из {len(OVERLAYS)}" if st["connected"]
            else "сим не найден")
        self.lbl_hint.configure(text=f"машин: {len(self.module._rows)}")
