#!/usr/bin/env python3
"""Check live Team OS profile chains for dead provider routing.

This intentionally scans only *routing chains* (main model provider, fallback
providers/model, delegation provider, and auxiliary providers). It does not flag
provider config sections, model catalogs, bundled plugins, docs, or examples.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

DISALLOWED_PROVIDERS = {"anthropic", "nous", "openrouter"}
ANTHROPIC_EXCEPTION_MARKERS = {"trader", "trader-migration"}
_ALLOWED_EMPTY = {"", "auto", None}


def _provider_from_model(value: Any) -> str | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    provider = value.split("/", 1)[0].strip().lower()
    return provider or None


def _anthropic_exception_allowed(file: Path) -> bool:
    """Return true for the only approved Anthropic API routing exception.

    MJ's default Team OS chains must stay subscription-only.  Anthropic API is
    intentionally limited to trader-migration surfaces, so the checker treats any
    other Anthropic routing chain as a live-route defect.
    """

    normalized_path = str(file).lower()
    return any(marker in normalized_path for marker in ANTHROPIC_EXCEPTION_MARKERS)


def _add_if_disallowed(findings: list[dict[str, str]], *, file: Path, chain: str, provider: Any) -> None:
    if provider in _ALLOWED_EMPTY:
        return
    normalized = str(provider).strip().lower()
    if normalized == "anthropic" and _anthropic_exception_allowed(file):
        return
    if normalized in DISALLOWED_PROVIDERS:
        findings.append({"file": str(file), "chain": chain, "provider": normalized})


def scan_config(path: Path) -> list[dict[str, str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    findings: list[dict[str, str]] = []

    model = data.get("model")
    if isinstance(model, dict):
        _add_if_disallowed(findings, file=path, chain="model.provider", provider=model.get("provider"))
        _add_if_disallowed(findings, file=path, chain="model.default provider prefix", provider=_provider_from_model(model.get("default")))
    else:
        _add_if_disallowed(findings, file=path, chain="model provider prefix", provider=_provider_from_model(model))

    fallback_providers = data.get("fallback_providers") or []
    if isinstance(fallback_providers, list):
        for index, entry in enumerate(fallback_providers):
            if not isinstance(entry, dict):
                continue
            _add_if_disallowed(findings, file=path, chain=f"fallback_providers[{index}].provider", provider=entry.get("provider"))
            _add_if_disallowed(findings, file=path, chain=f"fallback_providers[{index}].model provider prefix", provider=_provider_from_model(entry.get("model")))

    fallback_model = data.get("fallback_model")
    if isinstance(fallback_model, dict):
        _add_if_disallowed(findings, file=path, chain="fallback_model.provider", provider=fallback_model.get("provider"))
        _add_if_disallowed(findings, file=path, chain="fallback_model.model provider prefix", provider=_provider_from_model(fallback_model.get("model")))
    elif isinstance(fallback_model, list):
        for index, entry in enumerate(fallback_model):
            if isinstance(entry, dict):
                _add_if_disallowed(findings, file=path, chain=f"fallback_model[{index}].provider", provider=entry.get("provider"))
                _add_if_disallowed(findings, file=path, chain=f"fallback_model[{index}].model provider prefix", provider=_provider_from_model(entry.get("model")))
            else:
                _add_if_disallowed(findings, file=path, chain=f"fallback_model[{index}] provider prefix", provider=_provider_from_model(entry))
    else:
        _add_if_disallowed(findings, file=path, chain="fallback_model provider prefix", provider=_provider_from_model(fallback_model))

    delegation = data.get("delegation") or {}
    if isinstance(delegation, dict):
        _add_if_disallowed(findings, file=path, chain="delegation.provider", provider=delegation.get("provider"))
        _add_if_disallowed(findings, file=path, chain="delegation.model provider prefix", provider=_provider_from_model(delegation.get("model")))

    auxiliary = data.get("auxiliary") or {}
    if isinstance(auxiliary, dict):
        for name, entry in sorted(auxiliary.items()):
            if not isinstance(entry, dict):
                continue
            _add_if_disallowed(findings, file=path, chain=f"auxiliary.{name}.provider", provider=entry.get("provider"))
            _add_if_disallowed(findings, file=path, chain=f"auxiliary.{name}.model provider prefix", provider=_provider_from_model(entry.get("model")))

    return findings


def default_config_paths(home: Path) -> list[Path]:
    candidates = [home / "config.yaml"]
    profiles = home / "profiles"
    for name in ("cortex", "cto", "ruta", "billprinter"):
        candidates.append(profiles / name / "config.yaml")
    return [path for path in candidates if path.exists()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="config.yaml paths to scan; defaults to Team OS live profiles")
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    paths = args.paths or default_config_paths(args.home)
    findings: list[dict[str, str]] = []
    for path in paths:
        findings.extend(scan_config(path.expanduser()))

    result = {"checked": [str(path.expanduser()) for path in paths], "disallowed": sorted(DISALLOWED_PROVIDERS), "findings": findings, "finding_count": len(findings)}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"checked={len(paths)} finding_count={len(findings)}")
        for finding in findings:
            print(f"{finding['file']}: {finding['chain']} -> {finding['provider']}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
