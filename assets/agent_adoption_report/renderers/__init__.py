"""Renderers for different email templates.

Each renderer exposes a `build(cfg) -> Path` that writes
`<output_dir>/email.html` and returns the path. Callers pick a renderer based
on `cfg.template` (set in YAML, default = `single_scope`).

Built-in renderers:
- single_scope: the original mailer in `..mailer` -- one aggregate scope, with
  today's QAU/QoQ headline columns, spike/drop narration block, day-aligned WoW.
- three_scope: lifted from the `build_3scopes_email.py` template artifact --
  3-row Headlines (All / Flagship customer A / Flagship customer B), plus
  schema/surface/regional footprint. Reads pre-cached v2 JSONs and PNGs from
  `cfg.data_dir` (typically the workspace's `queries/` folder).
"""
