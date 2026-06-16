"""Multi-cluster fan-out + cross-region merge for the adoption-report pipeline.

Public entry point: `run(cfg, from_cache=False)` which produces the merged JSONs
and the region rollup under `cfg.output_dir`.
"""
from __future__ import annotations
import datetime as dt
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import ReportConfig

ASSETS_DIR = Path(__file__).resolve().parent.parent
KQL_MAIN_TMPL  = (ASSETS_DIR / "kql" / "main.kql.tmpl").read_text(encoding="utf-8")
KQL_DAILY_TMPL = (ASSETS_DIR / "kql" / "daily.kql.tmpl").read_text(encoding="utf-8")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def render_kql(template: str, cfg: ReportConfig) -> str:
    schema_list_json = json.dumps(cfg.schemas)
    schema_has_any = ", ".join(json.dumps(s) for s in cfg.schemas)
    repl = {
        "{{SCHEMA_LIST_JSON}}": schema_list_json,
        "{{SCHEMA_HAS_ANY}}":   schema_has_any,
        "{{APPLICATION_NAME}}": cfg.kusto.application_name,
        "{{SERVICE_NAME}}":     cfg.kusto.service_name,
        "{{ACTIVITY_NAME}}":    cfg.kusto.activity_name,
        "{{SHORT_DAYS}}":       str(cfg.windows.short_days),
        "{{MEDIUM_DAYS}}":      str(cfg.windows.medium_days),
        "{{LONG_DAYS}}":        str(cfg.windows.long_days),
        "{{QUARTER_DAYS}}":     str(cfg.windows.quarter_days),
        "{{PREV_END_DAYS}}":    str(cfg.windows.prev_end_days),
        "{{PREV_QUARTER_END_DAYS}}": str(cfg.windows.prev_quarter_end_days),
        "{{TOP_N_PER_CLUSTER}}":str(cfg.top_n_per_cluster),
    }
    out = template
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


# ---- Kusto auth + HTTP ------------------------------------------------------

def token_for(cluster_uri: str, aad_tenant: str) -> str:
    r = subprocess.run(
        ["az", "account", "get-access-token", "--resource", cluster_uri,
         "--tenant", aad_tenant, "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, timeout=60, shell=True,
    )
    return r.stdout.strip()


def _run_query(cluster_uri: str, tok: str, csl: str, db: str,
               server_timeout: str = "00:04:00") -> list[dict]:
    body = json.dumps({
        "db": db,
        "csl": csl,
        "properties": {"Options": {"servertimeout": server_timeout}},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{cluster_uri}/v1/rest/query",
        data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=420) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    table = None
    for t in doc.get("Tables", []):
        name = t.get("TableName") or ""
        if name in ("@ExtendedProperties", "QueryStatus", "QueryCompletionInformation"):
            continue
        if t.get("Columns") and t.get("Rows") is not None:
            table = t
            break
    if not table:
        return []
    cols = [c["ColumnName"] for c in table["Columns"]]
    return [dict(zip(cols, r)) for r in table["Rows"]]


# ---- Per-section merge across regions ---------------------------------------

def merge_windows(all_rows: list[dict]) -> list[dict]:
    keys: dict[tuple[str, str], dict] = {}
    for r in all_rows:
        if r.get("Section") != "Windows":
            continue
        k = (r.get("Window"), r.get("Surface"))
        d = keys.setdefault(k, {"Section": "Windows", "Window": k[0], "Surface": k[1],
                                "messages": 0, "conversations": 0, "users": 0})
        d["messages"]      += int(r.get("messages") or 0)
        d["conversations"] += int(r.get("conversations") or 0)
        d["users"]         += int(r.get("users") or 0)
    return list(keys.values())


def merge_schema(all_rows: list[dict], medium_label: str) -> list[dict]:
    keys: dict[str, dict] = {}
    for r in all_rows:
        if r.get("Section") != "Schema":
            continue
        sch = r.get("Schema") or ""
        d = keys.setdefault(sch, {"Section": "Schema", "Window": medium_label, "Schema": sch,
                                  "messages": 0, "conversations": 0, "users": 0})
        d["messages"]      += int(r.get("messages") or 0)
        d["conversations"] += int(r.get("conversations") or 0)
    return list(keys.values())


def merge_tenant_surface(all_rows: list[dict], medium_label: str) -> list[dict]:
    keys: dict[str, dict] = {}
    for r in all_rows:
        if r.get("Section") != "TenantSurface":
            continue
        s = r.get("Surface") or ""
        d = keys.setdefault(s, {"Section": "TenantSurface", "Window": medium_label, "Surface": s,
                                "messages": 0, "conversations": 0, "users": 0})
        d["messages"]      += int(r.get("messages") or 0)
        d["conversations"] += int(r.get("conversations") or 0)
        d["users"]         += int(r.get("users") or 0)
    return list(keys.values())


def merge_tenant_agg(all_rows: list[dict], medium_label: str, cap: int) -> list[dict]:
    """Aggregate per-tenant across regions, return top-`cap` globally as Section='Top10'.
    `cap` is intentionally larger than the leaderboard size so the email builder
    can filter exclusions and still have at least N rows to show."""
    by_tid: dict[str, dict] = {}
    for r in all_rows:
        if r.get("Section") != "TenantAgg":
            continue
        tid = (r.get("Surface") or "").lower()
        if not tid:
            continue
        rgn = r.get("_Region") or ""
        d = by_tid.setdefault(tid, {
            "TenantId": tid, "messages": 0, "users": 0,
            "regions": {}, "surfaces": {},
        })
        msgs = int(r.get("messages") or 0)
        usrs = int(r.get("users") or 0)
        dom  = r.get("Schema") or ""  # in TenantAgg, Schema holds dominant Surface
        d["messages"] += msgs
        d["users"]    += usrs
        d["regions"][rgn] = d["regions"].get(rgn, 0) + msgs
        d["surfaces"][dom] = d["surfaces"].get(dom, 0) + msgs
    rows = sorted(by_tid.values(), key=lambda r: -r["messages"])[:cap]
    out = []
    for r in rows:
        dominant_surface = max(r["surfaces"].items(), key=lambda kv: kv[1])[0] if r["surfaces"] else ""
        dominant_region  = max(r["regions"].items(),  key=lambda kv: kv[1])[0] if r["regions"] else ""
        out.append({
            "Section": "Top10",
            "Window": medium_label,
            "Surface": r["TenantId"],
            "messages": r["messages"],
            "conversations": r["users"],
            "users": r["users"],
            "Schema": dominant_surface,
            "_Region": dominant_region,
            "_RegionMsgs": dict(sorted(r["regions"].items(), key=lambda kv: -kv[1])),
        })
    return out


def merge_user_health(all_rows: list[dict]) -> list[dict]:
    wau = 0
    ret = 0
    for r in all_rows:
        if r.get("Section") != "UserHealth":
            continue
        wau += int(r.get("messages") or 0)
        ret += int(r.get("conversations") or 0)
    pct10 = int(round(100.0 * ret / wau * 10)) if wau > 0 else 0
    return [{
        "Section": "UserHealth", "Window": "curr_v_prev", "Surface": "",
        "messages": wau, "conversations": ret, "users": pct10, "Schema": "",
    }]


def merge_daily(daily_by_region: dict[str, list[dict]]) -> list[dict]:
    keys: dict[tuple[str, str], dict] = {}
    for rgn, rows in daily_by_region.items():
        for r in rows:
            day = r.get("Day")
            if isinstance(day, str):
                day = day[:10]
            surface = r.get("Surface") or ""
            k = (day, surface)
            d = keys.setdefault(k, {"Day": day, "Surface": surface,
                                    "messages": 0, "conversations": 0, "users": 0, "tenants": 0})
            d["messages"]      += int(r.get("messages") or 0)
            d["conversations"] += int(r.get("conversations") or 0)
            d["users"]         += int(r.get("users") or 0)
            d["tenants"]       += int(r.get("tenants") or 0)
    return sorted(keys.values(), key=lambda r: ((r["Day"] or ""), r["Surface"]))


def region_rollup(all_rows: list[dict], daily_by_region: dict[str, list[dict]],
                  short_label: str) -> list[dict]:
    by_region: dict[str, dict] = {}
    for r in all_rows:
        rgn = r.get("_Region") or ""
        sec = r.get("Section")
        d = by_region.setdefault(rgn, {"region": rgn, "messages_20d": 0, "users_20d": 0,
                                       "tenants_20d": 0, "msgs_7d": 0, "users_7d": 0,
                                       "daily_avg_users": 0.0})
        if sec == "TenantSurface":
            d["messages_20d"] += int(r.get("messages") or 0)
            d["users_20d"]    += int(r.get("users")    or 0)
            d["tenants_20d"]  += int(r.get("conversations") or 0)
        elif sec == "Windows" and r.get("Window") == short_label and r.get("Surface") == "TOTAL":
            d["msgs_7d"]  += int(r.get("messages") or 0)
            d["users_7d"] += int(r.get("users")    or 0)

    for rgn, drows in daily_by_region.items():
        per_day: dict[str, int] = {}
        for r in drows:
            day = r.get("Day")
            if isinstance(day, str):
                day = day[:10]
            per_day[day] = per_day.get(day, 0) + int(r.get("users") or 0)
        if per_day and rgn in by_region:
            by_region[rgn]["daily_avg_users"] = round(sum(per_day.values()) / len(per_day), 1)

    return sorted(by_region.values(), key=lambda r: -r["messages_20d"])


# ---- v2 JSON shape (compat with the existing email builder) -----------------

def dump_v2(rows: list[dict], path: Path, col_order: list[str]) -> None:
    columns = [{"ColumnName": c, "DataType": "Dynamic", "ColumnType": "dynamic"} for c in col_order]
    data_rows = [[r.get(c) for c in col_order] for r in rows]
    doc = {"Tables": [{"TableName": "Table_0", "Columns": columns, "Rows": data_rows}]}
    path.write_text(json.dumps(doc, default=str), encoding="utf-8")


# ---- Top-level pipeline ------------------------------------------------------

def run(cfg: ReportConfig, from_cache: bool = False) -> dict:
    """Fan out to all configured clusters (or read from raw cache), merge, and
    write the v2 JSONs + region-rollup.json to cfg.output_dir."""
    raw_dir = cfg.output_dir / "per-cluster-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    if from_cache:
        sys.stdout.write(f"[{now_iso()}] --from-cache: re-merging from {raw_dir} without hitting Kusto\n")
    else:
        sys.stdout.write(f"[{now_iso()}] Running per-cluster queries against {len(cfg.kusto.clusters)} clusters\n")
    sys.stdout.flush()

    kql_main  = render_kql(KQL_MAIN_TMPL,  cfg)
    kql_daily = render_kql(KQL_DAILY_TMPL, cfg)

    all_rows: list[dict] = []
    daily_by_region: dict[str, list[dict]] = {}

    for cl in cfg.kusto.clusters:
        if from_cache:
            main_p  = raw_dir / f"{cl.name}-main.json"
            daily_p = raw_dir / f"{cl.name}-daily.json"
            if main_p.exists():
                rows = json.loads(main_p.read_text(encoding="utf-8"))
                for r in rows:
                    r["_Region"] = cl.name
                all_rows.extend(rows)
                print(f"  {cl.name:<8} cache main  {len(rows):>4} rows")
            if daily_p.exists():
                rows_d = json.loads(daily_p.read_text(encoding="utf-8"))
                daily_by_region[cl.name] = rows_d
                print(f"  {cl.name:<8} cache daily {len(rows_d):>4} rows")
            continue

        sys.stdout.write(f"  {cl.name:<8} main  ... ")
        sys.stdout.flush()
        t0 = time.time()
        try:
            tok = token_for(cl.uri, cfg.kusto.aad_tenant)
            if not tok:
                print("token fail (check VPN / az login)")
                continue
            rows = _run_query(cl.uri, tok, kql_main, cfg.kusto.database, "00:06:00")
            print(f"{len(rows):>4} rows  [{time.time()-t0:.1f}s]")
            for r in rows:
                r["_Region"] = cl.name
            (raw_dir / f"{cl.name}-main.json").write_text(json.dumps(rows, default=str), encoding="utf-8")
            all_rows.extend(rows)
        except Exception as e:
            print(f"ERR: {e}")
            continue

        sys.stdout.write(f"  {cl.name:<8} daily ... ")
        sys.stdout.flush()
        t1 = time.time()
        try:
            rows_d = _run_query(cl.uri, tok, kql_daily, cfg.kusto.database, "00:03:00")
            print(f"{len(rows_d):>4} rows  [{time.time()-t1:.1f}s]")
            for r in rows_d:
                day = r.get("Day")
                if isinstance(day, str):
                    r["Day"] = day[:10]
            (raw_dir / f"{cl.name}-daily.json").write_text(json.dumps(rows_d, default=str), encoding="utf-8")
            daily_by_region[cl.name] = rows_d
        except Exception as e:
            print(f"ERR: {e}")

    print()
    print(f"Total main rows from all clusters: {len(all_rows)}")
    print(f"Daily rows from {len(daily_by_region)} clusters")

    # ---- Merge ----
    windows = merge_windows(all_rows)
    schema  = merge_schema(all_rows, cfg.windows.medium_label)
    ten_sur = merge_tenant_surface(all_rows, cfg.windows.medium_label)
    top_n   = merge_tenant_agg(all_rows, cfg.windows.medium_label,
                               cap=max(cfg.top_n_leaderboard * 3, 25))
    uh      = merge_user_health(all_rows)
    daily   = merge_daily(daily_by_region)
    rollup  = region_rollup(all_rows, daily_by_region, cfg.windows.short_label)

    print(f"  windows rows:        {len(windows)}")
    print(f"  schema rows:         {len(schema)}")
    print(f"  tenant-surface rows: {len(ten_sur)}")
    print(f"  top-N rows:          {len(top_n)}")
    print(f"  daily rows:          {len(daily)}")
    print(f"  region rollup rows:  {len(rollup)}")

    # ---- Persist v2 JSONs (same shape the email builder reads) ----
    out = cfg.output_dir
    a_rows = windows + uh
    b_rows: list[dict] = []
    for r in schema:
        b_rows.append({"Section": "Schema", "Window": r["Window"], "Surface": "",
                       "messages": r["messages"], "conversations": r["conversations"],
                       "users": 0, "Schema": r["Schema"]})
    for r in ten_sur:
        b_rows.append({"Section": "TenantSurface", "Window": r["Window"], "Surface": r["Surface"],
                       "messages": r["messages"], "conversations": r["conversations"],
                       "users": r["users"], "Schema": ""})
    for r in top_n:
        b_rows.append({"Section": "Top10", "Window": r["Window"], "Surface": r["Surface"],
                       "messages": r["messages"], "conversations": r["conversations"],
                       "users": r["users"], "Schema": r["Schema"]})

    cols = ["Section", "Window", "Surface", "messages", "conversations", "users", "Schema"]
    dump_v2(a_rows, out / "report-windows.json", cols)
    dump_v2(b_rows, out / "report-tables.json",  cols)
    dump_v2(daily, out / "report-daily.json",
            ["Day", "Surface", "messages", "conversations", "users", "tenants"])

    (out / "region-rollup.json").write_text(json.dumps({
        "generated": now_iso(),
        "agent_name": cfg.agent_name,
        "schemas": cfg.schemas,
        "clusters_queried": [c.name for c in cfg.kusto.clusters],
        "regions": rollup,
        "top10_with_regions": top_n,
    }, indent=2), encoding="utf-8")

    print()
    print("Wrote:")
    for p in ["report-windows.json", "report-tables.json", "report-daily.json", "region-rollup.json"]:
        print(f"  {out / p}")

    # Brief region table to stdout
    print()
    print("=== Region rollup ===")
    print(f"  {'Region':<8} {'Tenants':>8} {'Users':>10} {'Messages':>12} {'7d msgs':>10}  avg DAU")
    for r in rollup:
        print(f"  {r['region']:<8} {r['tenants_20d']:>8,} {r['users_20d']:>10,} "
              f"{r['messages_20d']:>12,} {r['msgs_7d']:>10,}  {(r.get('daily_avg_users') or 0):>7.0f}")

    return {
        "windows": windows, "schema": schema, "tenant_surface": ten_sur,
        "top_n": top_n, "user_health": uh, "daily": daily, "rollup": rollup,
    }
