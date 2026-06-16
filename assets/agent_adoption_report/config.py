"""Configuration loader for the adoption-report pipeline.

Reads a single YAML file and surfaces typed accessors so the rest of the
pipeline never reaches into raw dicts. Validation is intentionally light --
fail-fast on the required fields, accept extras silently.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install with `pip install pyyaml`."
    ) from e


@dataclass
class Cluster:
    name: str
    uri: str


@dataclass
class KustoConfig:
    aad_tenant: str = "72f988bf-86f1-41af-91ab-2d7cd011db47"
    database: str = "YourDatabase"
    application_name: str = "fabric:/YourAgent.Platform"
    service_name: str = "RuntimeService"
    activity_name: str = "YourBotActivityEvent"
    clusters: list[Cluster] = field(default_factory=list)


@dataclass
class WindowsConfig:
    short_days: int = 7
    medium_days: int = 20
    long_days: int = 28
    quarter_days: int = 90

    @property
    def prev_end_days(self) -> int:
        # End of the "previous short" window: short_days * 2 (e.g. 7 -> 14).
        return self.short_days * 2

    @property
    def prev_quarter_end_days(self) -> int:
        # End of the "previous quarter" window: quarter_days * 2 (e.g. 90 -> 180).
        return self.quarter_days * 2

    @property
    def short_label(self) -> str:
        return f"{self.short_days}d"

    @property
    def medium_label(self) -> str:
        return f"{self.medium_days}d"

    @property
    def long_label(self) -> str:
        return f"{self.long_days}d"

    @property
    def quarter_label(self) -> str:
        return f"{self.quarter_days}d"

    @property
    def prev_short_label(self) -> str:
        return f"prev{self.short_days}d"

    @property
    def prev_quarter_label(self) -> str:
        return f"prev{self.quarter_days}d"


@dataclass
class EmailConfig:
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    from_address: str | None = None


@dataclass
class ReportConfig:
    agent_name: str
    schemas: list[str]
    kusto: KustoConfig
    windows: WindowsConfig
    email: EmailConfig
    known_tenants: dict[str, str]
    exclude_from_leaderboard: set[str]
    top_n_per_cluster: int
    top_n_leaderboard: int
    output_dir: Path
    config_path: Path
    # Renderer dispatch (set in YAML).
    # - "single_scope" (default) -> the built-in `mailer.build()` that runs Kusto +
    #   charts + email, producing one aggregate scope with today's QAU/QoQ headline
    #   columns, day-aligned WoW, and spike/drop narration.
    # - "three_scope" -> `renderers.three_scope.build()` which reads pre-cached
    #   JSONs + PNGs from `data_dir` (typically the workspace's `queries/` folder)
    #   and produces the morning rich template with 3-row Headlines and per-scope
    #   Observations.
    template: str = "single_scope"
    data_dir: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "ReportConfig":
        cfg_path = Path(path).resolve()
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        # Required
        for key in ("agent_name", "schemas", "email"):
            if key not in raw:
                raise SystemExit(f"config missing required field: {key}")

        # Kusto
        kraw = raw.get("kusto") or {}
        clusters = [Cluster(name=c["name"], uri=c["uri"]) for c in (kraw.get("clusters") or [])]
        if not clusters:
            clusters = DEFAULT_CLUSTERS[:]
        kusto = KustoConfig(
            aad_tenant=kraw.get("aad_tenant", "72f988bf-86f1-41af-91ab-2d7cd011db47"),
            database=kraw.get("database", "YourDatabase"),
            application_name=kraw.get("application_name", "fabric:/YourAgent.Platform"),
            service_name=kraw.get("service_name", "RuntimeService"),
            activity_name=kraw.get("activity_name", "YourBotActivityEvent"),
            clusters=clusters,
        )

        # Windows
        wraw = raw.get("windows") or {}
        windows = WindowsConfig(
            short_days=int(wraw.get("short_days", 7)),
            medium_days=int(wraw.get("medium_days", 20)),
            long_days=int(wraw.get("long_days", 28)),
            quarter_days=int(wraw.get("quarter_days", 90)),
        )

        # Email
        eraw = raw["email"]
        recipients = eraw.get("recipients") or []
        if isinstance(recipients, str):
            recipients = [a.strip() for a in re.split(r"[;,]", recipients) if a.strip()]
        if not recipients:
            raise SystemExit("config.email.recipients must list at least one address")
        email = EmailConfig(
            recipients=list(recipients),
            subject=eraw.get("subject", f"{raw['agent_name']} adoption report"),
            from_address=eraw.get("from_address"),
        )

        # Output dir (relative to the config file unless absolute).
        out_str = raw.get("output_dir") or "out"
        out = Path(out_str)
        if not out.is_absolute():
            out = cfg_path.parent / out
        out.mkdir(parents=True, exist_ok=True)

        # Renderer template + (optional) data_dir for cached-data renderers.
        template = str(raw.get("template", "single_scope")).strip().lower()
        if template not in ("single_scope", "three_scope"):
            raise SystemExit(
                f"config.template must be 'single_scope' or 'three_scope' (got {template!r})"
            )
        data_dir: Path | None = None
        if raw.get("data_dir"):
            data_dir = Path(str(raw["data_dir"]))
            if not data_dir.is_absolute():
                data_dir = cfg_path.parent / data_dir
        if template == "three_scope" and data_dir is None:
            # Default to <config_dir>/morning-data, but DON'T create it; the renderer
            # itself will fail-fast with a clear "required inputs missing" message if
            # the folder is empty.
            data_dir = cfg_path.parent / "morning-data"

        return cls(
            agent_name=str(raw["agent_name"]),
            schemas=[str(s) for s in raw["schemas"]],
            kusto=kusto,
            windows=windows,
            email=email,
            known_tenants={k.lower(): v for k, v in (raw.get("known_tenants") or {}).items()},
            exclude_from_leaderboard={t.lower() for t in (raw.get("exclude_from_leaderboard") or [])},
            top_n_per_cluster=int(raw.get("top_n_per_cluster", 30)),
            top_n_leaderboard=int(raw.get("top_n_leaderboard", 10)),
            output_dir=out,
            config_path=cfg_path,
            template=template,
            data_dir=data_dir,
        )


# Default cluster list -- PLACEHOLDER regional ADX/Kusto endpoints.
# Replace these with your own cluster URIs, or (preferred) supply them in YAML
# under `kusto.clusters:` so no infrastructure endpoints live in source control.
DEFAULT_CLUSTERS: list[Cluster] = [
    Cluster("US",     "https://your-adx-us.<region>.kusto.windows.net"),
    Cluster("EU-WEU", "https://your-adx-eu.<region>.kusto.windows.net"),
    Cluster("ROW-AU", "https://your-adx-row.<region>.kusto.windows.net"),
]
