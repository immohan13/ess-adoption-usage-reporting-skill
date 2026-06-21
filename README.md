# agent-adoption-report

A reusable skill for generating an **adoption + usage report** for any Copilot
Studio / Power Virtual Agents bot whose runtime traffic lands in your
regional Kusto/ADX clusters. One YAML config in, one HTML email out.

## What you get

For your agent, across all 13 PVA Runtime regions:

1. **Headlines** -- 28d MAU, 7d msgs/users, WoW deltas, returning %, WAU/MAU
2. **Daily trend** -- messages + DAU dual axis (last 20 days)
3. **Active tenants and users** -- daily tenants + users dual axis
4. **Tenants per schema** -- horizontal bar
5. **Regional footprint** -- per-region table + horizontal bar of msg share
6. **Top customers leaderboard** -- top N by message volume, with friendly
   tenant names, a per-tenant **Cadence** column (avg active days/week per
   weekly-active user), auto-excluding bot/automation traffic
7. **Schema split** and **surface footprint** (Copilot / web client / others)

All charts are embedded as base64 PNGs in a single self-contained `email.html`
that can be pasted directly into Outlook.

## Quick start

```powershell
# 1. Install dependencies (one time)
pip install pyyaml matplotlib pillow

# 2. Make sure you can reach Kusto
az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47
# (and connect to the MS corp VPN)

# 3. Copy the example config and edit the 3 required fields
cp <skill-dir>/assets/config.example.yaml my-agent.yaml
# Edit:  agent_name, schemas, email.recipients

# 4. Run the full pipeline
python -m agent_adoption_report.cli all --config my-agent.yaml

# 5. Review the HTML
Start-Process .\out\email.html

# 6. Open an Outlook compose window (does NOT auto-send)
powershell -ExecutionPolicy Bypass `
  -File <skill-dir>/assets/send_email.ps1 `
  -Config my-agent.yaml
```

## Required config fields

| Field | What |
|-------|------|
| `agent_name` | Short display name. Used in titles, subject, headlines. |
| `schemas` | List of exact `BotSchemaName` values (Kusto `has_any` match). |
| `email.recipients` | One or more SMTP addresses for the review draft. |

Every other field has a sensible default. See `assets/config.example.yaml`
for the full schema with inline comments.

## How it works

```
config.yaml ->  pipeline.py ->  charts.py ->  mailer.py ->  send_email.ps1
                |               |              |             |
                Kusto fan-out   matplotlib     HTML + base64  Outlook COM
                + merge         PNGs           PNGs           (Display only)
```

- **`pipeline.run(cfg)`** fans out a templated KQL to each cluster in
  parallel (24 worker threads), saves the raw per-cluster JSON under
  `<output_dir>/per-cluster-raw/`, then merges into:
  - `report-windows.json`  -- short/medium/long/prev windows + UserHealth
  - `report-tables.json`   -- Schema / TenantSurface / Top-N per-tenant
  - `report-daily.json`    -- per-day per-region per-surface roll-up
  - `region-rollup.json`   -- per-region totals + top-tenants-by-region
- **`charts.build_all(cfg)`** reads those JSONs and writes 4 PNGs.
- **`mailer.build(cfg, paths)`** writes `email.html` with charts embedded.
- **`send_email.ps1`** opens an Outlook compose window for review.

## Re-run from cache

The Kusto fan-out is the slow part. Once you have a raw cache, re-render
without re-querying:

```powershell
python -m agent_adoption_report.cli all --config my-agent.yaml --from-cache
```

## Safety rules

This skill follows three non-negotiable rules:

1. **Never auto-sends email.** `send_email.ps1` always uses `.Display()`
   unless `-Send` is explicitly passed.
2. **Never invents tenant IDs.** Friendly tenant labels only appear when
   the GUID is in `known_tenants` in your config.
3. **Network access is required.** Kusto auth uses `az account get-access-token`
   against your AAD tenant; the clusters must be reachable from your network.

## Limitations (v1)

- **Cross-cluster overcount footnoted, not deduped.** Tenants typically
  route to one home cluster, so summing is safe in practice. The email
  footer states this explicitly.
- **Deep-dive customer sections are out of scope for v1** -- the original
  ESS-specific deep dives (top user, schema timeline per tenant) require
  per-tenant follow-up KQLs and are tracked separately.
- **Outlook COM only.** No Graph / SMTP path. Adding one would require an
  app registration.

## Files

- `SKILL.md` -- the agent-facing skill manifest
- `assets/agent_adoption_report/` -- the Python package
  - `config.py` -- YAML loader and dataclasses
  - `pipeline.py` -- Kusto fan-out + merge
  - `charts.py` -- matplotlib chart generation
  - `mailer.py` -- HTML email body
  - `cli.py` -- command-line entry
- `assets/kql/main.kql.tmpl` -- templated multi-section KQL
- `assets/kql/daily.kql.tmpl` -- templated daily roll-up KQL
- `assets/config.example.yaml` -- annotated config template
- `assets/send_email.ps1` -- Outlook compose helper

## License

MIT. See `SKILL.md` frontmatter.
