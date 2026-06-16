---
name: agent-adoption-report
description: "Generate a multi-region adoption-and-usage report for any Power Virtual Agents / Copilot Studio agent (or set of agents sharing a schema family) from Kusto/ADX telemetry. Produces an HTML email with regional rollup, top customer tenants, schema split, surface footprint, and daily trend charts -- and opens it in Outlook for review. USE FOR: per-agent adoption reports, multi-region usage rollups, weekly customer scorecards for a custom agent, agent telemetry summaries, anything that starts with 'send me the adoption email for <agent-x>'. DO NOT USE FOR: real-time monitoring (use azure-observability), agent build/deploy, MCS publishing, or anything that requires writing back to Dataverse. INVOKES: az CLI for Kusto tokens, Python (matplotlib + PIL), PowerShell (Outlook COM). REQUIRES: network access to your Kusto/ADX clusters and a working az login to the appropriate AAD tenant."
license: MIT
metadata:
  author: Mohan Ganesan
  version: "2.0.0"
---

# Agent adoption report

A reusable pipeline that pulls end-to-end adoption telemetry for any Copilot Studio /
Power Virtual Agents skill family (identified by a `BotSchemaName` filter) from your
regional Kusto/ADX clusters, merges the results across regions, and renders a
ready-to-send Outlook email with charts + tables.

## When to invoke

Trigger on user prompts like:
- "Generate the adoption report for `<agent-x>`"
- "Send me the weekly usage email for my agent"
- "Build the multi-region scorecard for `<custom-agent>` and open it in Outlook"
- "I need a customer-tenant leaderboard for the agent that uses schema `foo*`"

## What the report contains

The email is a single leadership-friendly **Adoption & Usage report**. Section order
(top to bottom), as locked in the v2 template:

1. **Intro + "What's new this week"** — one-line framing, then a blue callout box with
   three bullets: top customers & WoW trend, new customers (named), and the GPT models
   customers are running.
2. **Feedback ask** — a yellow callout inviting recipients to reply with metrics/cuts
   they want to see.
3. **Headline KPI table** — 28d msgs / MAU / 7d msgs / WoW msgs / 7d users / WoW users /
   WAU / WAU÷MAU / Returning %. Followed by narrative bullets (WF surge callout in green,
   tenant-growth line, scale, concentration, surface mix).
4. **Daily trend** — messages and DAU charts.
5. **Top customers** — leaderboard (by messages and by users) with WoW% columns and
   color-coded GPT model per tenant. `EXCLUDE_FROM_LEADERBOARD` tenants filtered out.
6. **Active Tenants and Users** — daily active tenants+users chart + tenants-per-agent chart.
7. **ESS agents** — messages/share/tenants per `BotSchemaName`.
8. **Surface footprint** — Copilot vs web client (Direct Line) vs Others, **sorted by Users**.
9. **Regional footprint** — table + horizontal bar chart of message volume per Kusto region.

> **v2 change:** the old per-tenant deep-dive sections (separate per-flagship
> blocks with Observations) were **removed**
> to keep the email tight for leadership. The Top-customers leaderboard + headline
> callouts now carry that signal.

The pipeline writes all intermediate JSONs and PNGs to `<output_dir>/` and
the final HTML to `<output_dir>/email.html`.

## Workflow

Follow these steps in order. Use the questions tool to gather missing config
values; never invent tenant IDs, recipients, or schema names.

### Step 1 — Confirm prerequisites

Before running anything:
- [ ] User is on the Microsoft corporate network (VPN on). Kusto auth will fail otherwise.
- [ ] `az login` against tenant `72f988bf-86f1-41af-91ab-2d7cd011db47` (microsoft.com)
      is current. Run `az account show` to confirm.
- [ ] Python 3.10+ available with `matplotlib`, `Pillow`, and `PyYAML` installed.

### Step 2 — Locate or create the report config

The pipeline is driven by a single YAML config. If the user has not pointed you at
one, ask for the path or offer to create one from `config.example.yaml` in this
skill's `assets/` folder.

**Minimum config fields** the user must provide for a new agent:
- `agent_name` — short display name used in the subject and headings
- `schemas` — list of `BotSchemaName` values that identify the agent
- `email.recipients` — at least one address
- `email.subject` — full subject line

**Defaults (override these in your YAML config)**:
- Your regional Kusto/ADX clusters (set `kusto.clusters` in YAML)
- AAD tenant, database, and the activity filter
  (`applicationName`, `serviceName`, `activityName`,
  `customDimensions has '"ActivityType":"message"'`)

### Step 3 — Run the pipeline

From the user's working directory:

```powershell
python <skill-assets>/agent_adoption_report/cli.py build --config <path-to-config.yaml>
```

Add `--from-cache` after a first successful run to re-merge / re-render without
re-hitting Kusto (uses the per-cluster raw JSONs cached under `<output_dir>/per-cluster-raw/`).
This is the right flag whenever the user wants to tweak the email layout, exclusion
list, or KNOWN_TENANTS map without spending another ~5 min of Kusto time.

The CLI emits a one-line status per cluster as it goes and a final region rollup
table to stdout.

### Step 4 — Preview, then open Outlook

Before opening Outlook, open `<output_dir>/email.html` in the browser:

```powershell
Start-Process <output_dir>/email.html
```

When the user confirms the preview is good, open the Outlook compose window
(NEVER auto-send):

```powershell
powershell -ExecutionPolicy Bypass -File <skill-assets>/send_email.ps1 -Config <path-to-config.yaml>
```

The PowerShell helper reads recipients and subject from the same config,
creates an Outlook mail item via COM, attaches the HTML body, and calls
`.Display()` so the user can review and click Send manually.

## Email rendering rules (Outlook-safe)

The email is read in **Outlook desktop**, which renders HTML through the Word
engine, not a browser. Two rules are mandatory or the formatting silently breaks
in the sent mail (even though it looks fine in a browser preview):

1. **Background colors need `bgcolor`, not just CSS.** Outlook ignores CSS
   `background:#hex` on rows, table headers, and `<div>`/`<p>` callout boxes. The
   builder runs every page through an `outlook_safe()` post-processor that injects
   a matching legacy `bgcolor="#hex"` attribute onto any tag carrying an inline
   `background:#hex`. Never remove this step. (Cosmetic caveat: `border-radius`
   renders square and left `border-left` accent bars may not show — backgrounds do.)
2. **Wrap the whole body in a fixed-width (1100px) `<table>`.** Outlook ignores
   `max-width` on `<body>`, so colored callout boxes overflow past the charts and
   tables. The body lives inside a single `width="1100" style="width:1100px;max-width:1100px"`
   presentation table cell so every element aligns to the same width.

After any layout edit: rebuild → verify (the markers/colors are present) → reopen in
Outlook to confirm rendering before sending.

### Color legend (keep consistent across editions)

| Element | Hex |
| --- | --- |
| Positive / green (WoW up, growth) | `#2e7d32` / `#1b5e20` |
| Negative / red (WoW down) | `#c62828` |
| Spiking-customers callout (blue) | `#1565c0` |
| New customer names (purple) | `#6a1b9a` |
| Contoso / internal-test note (orange) | `#e67e00` |
| GPT-4.1 models (light red) | `#e57373` |
| GPT-5 models (purple) | `#8e24aa` |
| "What's new" box bg | `#f4f8fc` (border `#d6e4f0`) |
| Feedback callout bg | `#fffce6` (border `#f0c000`) |
| Flagship surge callout bg | `#e8f5e9` (border `#2e7d32`) |

## Weekly reuse (the canonical template)

The weekly email is built by the canonical template:

- `assets/templates/build_3scopes_email.py` — the production HTML builder
  (`outlook_safe()`, 1100px wrapper, surface-by-users sort, color legend above).
  Customer tenant labels + leaderboard exclusions are **not** hard-coded; they load
  from a local, git-ignored `tenant-config.local.json` (see below).
- `assets/templates/send_email.ps1` — the Outlook COM sender (reads `email-3scopes.html`,
  `.Display()` for review; never auto-sends).

### Local config (keeps customer data out of source control)

1. Copy `assets/templates/tenant-config.example.json` → `tenant-config.local.json`
   in your working `queries/` dir and fill in your real tenant GUID → display-name
   map, `exclude_from_leaderboard` list, and `flagship_a_label` / `flagship_b_label`.
   The file is git-ignored. Without it, tenants render as masked GUIDs and the
   flagship rows fall back to generic labels.
2. Recipients: copy `recipients.example.json` → `recipients.local.json`, **or** set
   `$env:ADOPTION_REPORT_RECIPIENTS` / `$env:ADOPTION_REPORT_SUBJECT`, **or** pass
   `-To` / `-Subject` to `send_email.ps1`. Nothing is hard-coded.

To reproduce next week: refresh the `bot-scope-*-v2.json` + chart PNGs in the working
`queries/` dir, run `python build_3scopes_email.py`, then `./send_email.ps1`. The
config-driven `renderers/three_scope.py` mirrors the same v2 layout (section order,
surface-by-users sort, Outlook-safe output) for the YAML-driven `cli.py` path, but the
template script is the source of truth for the leadership email.

## Safety rules (non-negotiable)

1. **Never auto-send email.** Always open Outlook for review. The `send_email.ps1`
   helper deliberately omits any `-Send` switch by default. If the user explicitly
   says "send it" while the Outlook window is open, they click Send themselves.
2. **Never invent tenant IDs.** All entries in `known_tenants` and
   `exclude_from_leaderboard` must come from the user or be resolved via
   Microsoft Graph `tenantRelationships.findTenantInformationByTenantId`.
3. **Respect the VPN requirement.** Do not retry Kusto calls in a loop if they
   fail with auth errors — surface the failure and prompt the user to check VPN /
   `az login`.

## Files in this skill

- `SKILL.md` — this file
- `README.md` — human-readable getting-started doc
- `assets/templates/build_3scopes_email.py` — **canonical weekly template** (source of truth)
- `assets/templates/send_email.ps1` — **canonical Outlook sender** for the template
- `assets/templates/tenant-config.example.json` — placeholder for your local tenant map (copy to `tenant-config.local.json`)
- `assets/templates/recipients.example.json` — placeholder for your local recipient list (copy to `recipients.local.json`)
- `.gitignore` — keeps `*.local.json`, generated HTML/PNGs, and `__pycache__` out of git
- `assets/config.example.yaml` — annotated template config (config-driven path)
- `assets/agent_adoption_report/cli.py` — entry point (`build` / `--from-cache`)
- `assets/agent_adoption_report/pipeline.py` — multi-cluster fan-out + merge
- `assets/agent_adoption_report/charts.py` — matplotlib charts
- `assets/agent_adoption_report/renderers/three_scope.py` — config-driven HTML builder (mirrors v2 layout)
- `assets/kql/main.kql.tmpl` — per-cluster Windows / Schema / TenantSurface / TenantAgg / UserHealth
- `assets/kql/daily.kql.tmpl` — per-cluster daily series
- `assets/send_email.ps1` — config-driven Outlook COM helper

## Known limitations (v2)

- Per-tenant **deep-dive sections** were intentionally removed in v2 (see the v2
  change note above). If a future edition needs a dedicated deep-dive per tenant,
  re-add it from git history — the headline callouts + leaderboard cover it for most
  leadership reads.
- The KQL templates assume PVA Runtime telemetry shape. Agents that don't run on
  PVA (e.g. pure M365 Copilot custom skills with no Direct Line) will need a
  different `kusto.filter` block in the config.
- User and tenant counts are summed across clusters without cross-cluster dedup.
  In practice the same user/tenant rarely lands in more than one home cluster,
  but the email footer surfaces this caveat.
