"""Statistiques de productivité : données par jour + graphique PNG pour le téléphone."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta

from .llm import WEEKDAYS

# Palette de référence (thème clair) : une seule série par panneau -> un seul bleu.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES = "#2a78d6"


def collect(store, tasks, today: date, days: int = 7) -> list[dict]:
    """Une ligne par jour : minutes de focus, todos cochés, taux d'habitudes, tâches de l'agent."""
    rows = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        _, minutes = store.focus_minutes_on(day)
        agent = sum(
            1 for t in tasks.values()
            if t.status == "terminée" and t.finished and datetime.fromtimestamp(t.finished).date() == day
        )
        rows.append({
            "day": day,
            "focus": minutes,
            "todos": len(store.done_on(day)),
            "habits": store.habits_rate_on(day),
            "agent": agent,
        })
    return rows


def fmt_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d}" if h else f"{m} min"


def summary_text(rows: list[dict]) -> str:
    """Résumé chiffré (sert aussi de « vue tableau » du graphique)."""
    focus = sum(r["focus"] for r in rows)
    todos = sum(r["todos"] for r in rows)
    agent = sum(r["agent"] for r in rows)
    rates = [r["habits"] for r in rows if r["habits"] is not None]
    active = sum(1 for r in rows if r["focus"] or r["todos"])
    best = max(rows, key=lambda r: r["focus"])
    lines = [
        f"🎯 Concentration : {fmt_duration(focus)} (moyenne {fmt_duration(focus / len(rows))}/jour)",
        f"✅ Tâches cochées : {todos}",
        f"📅 Jours actifs : {active}/{len(rows)}",
    ]
    if rates:
        lines.append(f"🔁 Habitudes tenues : {round(100 * sum(rates) / len(rates))} %")
    if agent:
        lines.append(f"🛠 Tâches réalisées par l'agent : {agent}")
    if best["focus"]:
        lines.append(f"🏆 Meilleur jour : {WEEKDAYS[best['day'].weekday()]} {best['day']:%d/%m} "
                     f"({fmt_duration(best['focus'])})")
    return "\n".join(lines)


def _day_label(day: date, days: int, index: int) -> str:
    if days <= 10:
        return f"{WEEKDAYS[day.weekday()][:3]}\n{day:%d}"
    return f"{day:%d/%m}" if index % 5 == 0 or index == days - 1 else ""


def render_png(rows: list[dict], title: str) -> bytes | None:
    """Trois petits graphiques empilés (un par mesure, chacun sur son propre axe).
    Retourne None si matplotlib n'est pas installé."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch, Rectangle
    except ImportError:
        return None

    days = len(rows)
    panels = [
        ("Concentration", [r["focus"] for r in rows], fmt_duration, None),
        ("Tâches cochées", [r["todos"] for r in rows], lambda v: f"{int(v)}", None),
    ]
    if any(r["habits"] is not None for r in rows):
        panels.append(
            ("Habitudes tenues", [100 * (r["habits"] or 0) for r in rows], lambda v: f"{round(v)} %", 100)
        )

    dpi = 200
    fig_w, panel_h = 5.4, 1.75  # format téléphone (portrait)
    fig, axes = plt.subplots(len(panels), 1, figsize=(fig_w, 0.5 + panel_h * len(panels)), dpi=dpi, sharex=True)
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(title, x=0.04, y=0.998, ha="left", fontsize=12, fontweight="bold", color=INK)
    x = list(range(days))
    today_idx = days - 1

    for ax, (name, values, fmt, ymax) in zip(axes, panels):
        ax.set_facecolor(SURFACE)
        top = (ymax or max(max(values), 1)) * 1.25  # marge pour les étiquettes au-dessus des barres
        ax.set_ylim(0, top)
        ax.set_xlim(-0.6, days - 0.4)
        total = sum(values) if ymax is None else (sum(values) / days)
        headline = fmt(total) + (" en moyenne" if ymax else " au total")
        ax.set_title(f"{name} · {headline}", loc="left", fontsize=9, color=INK_2, pad=4)

        # Barres fines (<= 24 px), bout arrondi de 4 px, base carrée.
        fig.canvas.draw()
        bbox = ax.get_window_extent()
        px_per_x = bbox.width / (days - 0.4 + 0.6)
        px_per_y = bbox.height / top
        width = min(0.62, 24 / px_per_x)
        radius_x, radius_y = 4 / px_per_x, 4 / px_per_y
        for i, v in enumerate(values):
            if v <= 0:
                continue
            left = i - width / 2
            r_y = min(radius_y, v / 2)
            # haut arrondi (petit bloc aux coins ronds) + corps rectangulaire qui recouvre ses coins bas
            ax.add_patch(FancyBboxPatch(
                (left, v - 2 * r_y), width, 2 * r_y,
                boxstyle=f"round,pad=0,rounding_size={radius_x}",
                mutation_aspect=r_y / radius_x, linewidth=0, facecolor=SERIES,
            ))
            ax.add_patch(Rectangle((left, 0), width, v - r_y, linewidth=0, facecolor=SERIES))

        # Étiquettes sélectives : meilleur jour et aujourd'hui seulement.
        labelled = {today_idx}
        if max(values) > 0:
            labelled.add(values.index(max(values)))
        for i in labelled:
            if values[i] > 0:
                ax.text(i, values[i] + top * 0.03, fmt(values[i]), ha="center", va="bottom",
                        fontsize=7, color=INK)

        # Axes discrets : grille fine, ligne de base, pas de cadre.
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(BASELINE)
        ax.tick_params(axis="y", colors=MUTED, labelsize=7, length=0)
        ax.tick_params(axis="x", colors=MUTED, labelsize=7, length=0)
        if ymax:
            ax.yaxis.set_major_locator(plt.FixedLocator([0, ymax / 2, ymax]))
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v)} %"))
        else:
            ax.yaxis.set_major_locator(plt.MaxNLocator(3, integer=True))
        if name == "Concentration":
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v)}"))
            ax.set_ylabel("min", color=MUTED, fontsize=7, rotation=0, labelpad=10, va="center")

    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels([_day_label(r["day"], days, i) for i, r in enumerate(rows)])
    fig.tight_layout(rect=(0, 0, 1, 0.985), h_pad=1.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
