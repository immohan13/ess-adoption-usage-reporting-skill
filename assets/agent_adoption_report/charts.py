"""Chart generation for the adoption-report email.

Produces 4 PNGs in cfg.output_dir:
  - trend.png            messages (left) + DAU (right) over the medium window
  - tenants_users.png    daily distinct tenants + users, dual axis
  - schema.png           horizontal bar of tenants per BotSchemaName
  - regions.png          horizontal bar of message volume per Kusto region

All charts use matplotlib Agg backend (no display) and write PNGs at 100 dpi.
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .config import ReportConfig

# Region color helper (US dark blue, EU light blue, ROW gray) -- safe fallback for any region name.
def _region_color(name: str) -> str:
    if name == "US":
        return "#1f4e79"
    if name.startswith("EU"):
        return "#7eb6e8"
    return "#9aa0a6"


def _parse_day(s):
    """Parse a Day string to a date; return None for non-date values
    (e.g. a stray Kusto diagnostic/'Exceptions' row that leaked into the rows)."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


# ---- 1. Daily trend (messages + DAU) -------------------------------------

def plot_trend(cfg: ReportConfig) -> Path:
    out = cfg.output_dir / "trend.png"
    daily = json.loads((cfg.output_dir / "report-daily.json").read_text(encoding="utf-8"))
    cols = [c["ColumnName"] for c in daily["Tables"][0]["Columns"]]
    rows = [dict(zip(cols, r)) for r in daily["Tables"][0]["Rows"]]

    by_day: dict = {}
    for r in rows:
        d = _parse_day(r.get("Day"))
        if d is None:
            continue
        bucket = by_day.setdefault(d, {"messages": 0, "users": 0})
        bucket["messages"] += int(r.get("messages") or 0)
        bucket["users"]    += int(r.get("users")    or 0)
    days  = sorted(by_day.keys())
    msgs  = [by_day[d]["messages"] for d in days]
    users = [by_day[d]["users"]    for d in days]

    fig, ax_m = plt.subplots(figsize=(11, 4.5), dpi=100)
    ax_u = ax_m.twinx()
    ln_m, = ax_m.plot(days, msgs,  color="#1f4e79", linewidth=2.2, marker="o", markersize=4, label="Messages")
    ln_u, = ax_u.plot(days, users, color="#d97706", linewidth=2.2, marker="s", markersize=4, label="Daily active users")
    ax_m.set_title(f"Daily trend -- {cfg.agent_name}, last {cfg.windows.medium_days} days",
                   fontsize=12, fontweight="bold", loc="left")
    ax_m.set_ylabel("Messages",              color="#1f4e79")
    ax_u.set_ylabel("Daily active users",    color="#d97706")
    ax_m.tick_params(axis="y", labelcolor="#1f4e79")
    ax_u.tick_params(axis="y", labelcolor="#d97706")
    ax_m.grid(True, alpha=0.25, linestyle="--", linewidth=0.5)
    ax_m.set_xlim(days[0], days[-1])
    ax_m.set_ylim(bottom=0); ax_u.set_ylim(bottom=0)
    ax_m.spines["top"].set_visible(False)
    ax_u.spines["top"].set_visible(False)
    ax_m.legend(handles=[ln_m, ln_u], loc="upper left", frameon=False, fontsize=10)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, dpi=100, bbox_inches="tight", pil_kwargs={"optimize": True})
    plt.close(fig)
    return out


# ---- 2. Daily active tenants + users -------------------------------------

def plot_tenants_users(cfg: ReportConfig) -> Path:
    out = cfg.output_dir / "tenants_users.png"
    daily = json.loads((cfg.output_dir / "report-daily.json").read_text(encoding="utf-8"))
    cols = [c["ColumnName"] for c in daily["Tables"][0]["Columns"]]
    rows = [dict(zip(cols, r)) for r in daily["Tables"][0]["Rows"]]

    by_day: dict = {}
    for r in rows:
        d = _parse_day(r["Day"])
        bucket = by_day.setdefault(d, {"tenants": 0, "users": 0})
        bucket["tenants"] += int(r.get("tenants") or 0)
        bucket["users"]   += int(r.get("users")   or 0)
    days    = sorted(by_day.keys())
    tenants = [by_day[d]["tenants"] for d in days]
    users   = [by_day[d]["users"]   for d in days]

    fig, ax_t = plt.subplots(figsize=(11, 4.5), dpi=100)
    ax_u = ax_t.twinx()
    ln_t, = ax_t.plot(days, tenants, color="#1f4e79", linewidth=2.2, marker="o", markersize=4, label="Active tenants")
    ln_u, = ax_u.plot(days, users,   color="#d97706", linewidth=2.2, marker="s", markersize=4, label="Active users")
    if tenants:
        pt = tenants.index(max(tenants))
        ax_t.annotate(f"peak {tenants[pt]} tenants", xy=(days[pt], tenants[pt]),
                      xytext=(8, 10), textcoords="offset points", fontsize=9, color="#1f4e79",
                      bbox=dict(boxstyle="round,pad=0.3", facecolor="#eaf1f9", edgecolor="#1f4e79", linewidth=0.6))
    if users:
        pu = users.index(max(users))
        ax_u.annotate(f"peak {users[pu]:,} users", xy=(days[pu], users[pu]),
                      xytext=(8, -16), textcoords="offset points", fontsize=9, color="#d97706",
                      bbox=dict(boxstyle="round,pad=0.3", facecolor="#fef3c7", edgecolor="#d97706", linewidth=0.6))
    ax_t.set_title(f"Daily active tenants and users -- {cfg.agent_name}, last {cfg.windows.medium_days} days",
                   fontsize=12, fontweight="bold", loc="left")
    ax_t.set_ylabel("Distinct tenants active that day", color="#1f4e79")
    ax_u.set_ylabel("Distinct users active that day",   color="#d97706")
    ax_t.tick_params(axis="y", labelcolor="#1f4e79")
    ax_u.tick_params(axis="y", labelcolor="#d97706")
    ax_t.grid(True, alpha=0.25, linestyle="--", linewidth=0.5)
    ax_t.set_xlim(days[0], days[-1])
    ax_t.set_ylim(bottom=0); ax_u.set_ylim(bottom=0)
    ax_t.spines["top"].set_visible(False)
    ax_u.spines["top"].set_visible(False)
    ax_t.legend(handles=[ln_t, ln_u], loc="upper left", frameon=False, fontsize=10)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, dpi=100, bbox_inches="tight", pil_kwargs={"optimize": True})
    plt.close(fig)
    return out


# ---- 3. Tenants per BotSchemaName ----------------------------------------

def plot_schema(cfg: ReportConfig) -> Path:
    out = cfg.output_dir / "schema.png"
    tables = json.loads((cfg.output_dir / "report-tables.json").read_text(encoding="utf-8"))
    cols = [c["ColumnName"] for c in tables["Tables"][0]["Columns"]]
    rows = [dict(zip(cols, r)) for r in tables["Tables"][0]["Rows"]]
    schema_rows = [r for r in rows if r["Section"] == "Schema"]
    schema_rows.sort(key=lambda r: -(r.get("conversations") or 0))

    if not schema_rows:
        # Write an empty placeholder so callers can rely on the file existing.
        fig, ax = plt.subplots(figsize=(8, 2), dpi=100)
        ax.text(0.5, 0.5, "no schema data", ha="center", va="center", color="#666")
        ax.axis("off")
        fig.savefig(out, dpi=100, bbox_inches="tight")
        plt.close(fig)
        return out

    labels  = [r["Schema"] for r in schema_rows]
    tenants = [r["conversations"] for r in schema_rows]

    fig, ax = plt.subplots(figsize=(9, max(2.5, 0.5 * len(labels) + 1)), dpi=100)
    ypos = range(len(labels))
    ax.barh(list(ypos), tenants, color="#1f4e79", alpha=0.85)
    ax.set_yticks(list(ypos))
    ax.set_yticklabels(labels, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("Distinct tenants (cumulative over window)")
    ax.set_title(f"Tenants per schema -- {cfg.agent_name}",
                 fontsize=12, fontweight="bold", loc="left")
    for i, v in enumerate(tenants):
        ax.text(v, i, f" {v:,}", va="center", fontsize=9, color="#333")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, dpi=100, bbox_inches="tight", pil_kwargs={"optimize": True})
    plt.close(fig)
    return out


# ---- 4. Regional footprint -----------------------------------------------

def plot_regions(cfg: ReportConfig) -> Path:
    out = cfg.output_dir / "regions.png"
    rollup = json.loads((cfg.output_dir / "region-rollup.json").read_text(encoding="utf-8"))
    regions = [r for r in rollup.get("regions", []) if r.get("messages_20d", 0) > 0]
    if not regions:
        fig, ax = plt.subplots(figsize=(8, 2), dpi=100)
        ax.text(0.5, 0.5, "no regional traffic", ha="center", va="center", color="#666")
        ax.axis("off")
        fig.savefig(out, dpi=100, bbox_inches="tight")
        plt.close(fig)
        return out

    # Sort ascending so the biggest bar lands on top after invert_yaxis.
    regions.sort(key=lambda r: r["messages_20d"])
    labels = [r["region"] for r in regions]
    msgs   = [r["messages_20d"] for r in regions]
    tenants = [r["tenants_20d"] for r in regions]
    total = sum(msgs) or 1
    colors = [_region_color(n) for n in labels]

    fig, ax = plt.subplots(figsize=(11, max(3, 0.45 * len(labels) + 1.5)), dpi=100)
    bars = ax.barh(labels, msgs, color=colors, alpha=0.9)
    for i, b in enumerate(bars):
        share = msgs[i] / total * 100
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2,
                f"  {msgs[i]:,}  ({share:.1f}%)  - {tenants[i]} tenants",
                va="center", fontsize=10, color="#333")
    ax.set_xlabel(f"Messages (last {cfg.windows.medium_days} days)")
    ax.set_title(f"Regional footprint -- {cfg.agent_name}",
                 fontsize=12, fontweight="bold", loc="left")
    ax.set_xlim(0, max(msgs) * 1.4)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, dpi=100, bbox_inches="tight", pil_kwargs={"optimize": True})
    plt.close(fig)
    return out


def build_all(cfg: ReportConfig) -> dict[str, Path]:
    return {
        "trend":          plot_trend(cfg),
        "tenants_users":  plot_tenants_users(cfg),
        "schema":         plot_schema(cfg),
        "regions":        plot_regions(cfg),
    }
