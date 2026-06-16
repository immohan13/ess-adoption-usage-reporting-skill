"""CLI entry for the adoption-report skill.

Usage:
    python -m agent_adoption_report.cli build --config <config.yaml> [--from-cache]
    python -m agent_adoption_report.cli charts --config <config.yaml>
    python -m agent_adoption_report.cli email  --config <config.yaml>
    python -m agent_adoption_report.cli all    --config <config.yaml> [--from-cache]   # default

Or invoke as a script:
    python <skill-dir>/assets/agent_adoption_report/cli.py all --config my-config.yaml
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Allow running this file directly (no installed package).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent_adoption_report.config import ReportConfig
    from agent_adoption_report import pipeline, charts, mailer
else:
    from .config import ReportConfig
    from . import pipeline, charts, mailer


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-adoption-report",
                                description="Generate an adoption-and-usage report for a Copilot Studio / PVA agent.")
    p.add_argument("step", choices=["build", "charts", "email", "all"], default="all", nargs="?",
                   help="Which step to run (default: all).")
    p.add_argument("--config", "-c", required=True, help="Path to the report YAML config.")
    p.add_argument("--from-cache", action="store_true",
                   help="Skip Kusto and re-merge from per-cluster-raw/ cache.")
    args = p.parse_args(argv)

    cfg = ReportConfig.load(args.config)
    print(f"Agent:       {cfg.agent_name}")
    print(f"Output dir:  {cfg.output_dir}")
    print(f"Template:    {cfg.template}")
    if cfg.template == "three_scope":
        print(f"Data dir:    {cfg.data_dir}")
    print(f"Clusters:    {len(cfg.kusto.clusters)}")
    print(f"Schemas:     {len(cfg.schemas)}")
    print()

    # ------------------------------------------------------------------
    # three_scope renderer reads pre-cached JSONs + PNGs from data_dir.
    # It does NOT run Kusto or matplotlib itself -- those are produced by
    # the workspace's morning data refresh (run_all_regions.py + plot_*.py
    # in the ESS queries/ folder). Skip build/charts unconditionally.
    # ------------------------------------------------------------------
    if cfg.template == "three_scope":
        if args.step in ("build", "charts"):
            print(f"[three_scope] step '{args.step}' is a no-op for this template -- "
                  f"data is sourced from data_dir={cfg.data_dir}")
            return 0
        from .renderers import three_scope
        out = three_scope.build(cfg)
        size = out.stat().st_size
        print(f"Email:  {out}  ({size:,} bytes)")
        print()
        print("Preview with:")
        print(f"  Start-Process {out}")
        print("Open Outlook compose with (NEVER auto-sends):")
        ps1 = Path(__file__).resolve().parent.parent / "send_email.ps1"
        print(f"  powershell -ExecutionPolicy Bypass -File {ps1} -Config {args.config}")
        return 0

    if args.step in ("build", "all"):
        pipeline.run(cfg, from_cache=args.from_cache)
    if args.step in ("charts", "all"):
        paths = charts.build_all(cfg)
        print()
        print("Charts:")
        for k, v in paths.items():
            print(f"  {k:<14} {v}")
    if args.step in ("email", "all"):
        paths = charts.build_all(cfg) if args.step == "email" else {
            "trend":         cfg.output_dir / "trend.png",
            "tenants_users": cfg.output_dir / "tenants_users.png",
            "schema":        cfg.output_dir / "schema.png",
            "regions":       cfg.output_dir / "regions.png",
        }
        out = mailer.build(cfg, paths)
        size = out.stat().st_size
        print()
        print(f"Email:  {out}  ({size:,} bytes)")
        print()
        print("Preview with:")
        print(f"  Start-Process {out}")
        print("Open Outlook compose with (NEVER auto-sends):")
        ps1 = Path(__file__).resolve().parent.parent / "send_email.ps1"
        print(f"  powershell -ExecutionPolicy Bypass -File {ps1} -Config {args.config}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
