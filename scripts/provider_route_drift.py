#!/usr/bin/env python3
"""Provider-route drift scanner for Hermes profiles.

Read-only tool for AGENTS-266. It inventories provider/model routing surfaces in
Hermes profile config files and compares them against an explicit approved
baseline. It never prints credential values: secret-bearing keys are reduced to
boolean markers or env-var names.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception as exc:  # pragma: no cover - exercised only on broken envs
    raise SystemExit(f"PyYAML is required: {exc}")

SECRET_MARKERS = ("api_key", "apikey", "token", "secret", "password", "authorization")
ROUTE_MODEL_KEYS = ("model", "imageModel", "imageGenerationModel", "videoGenerationModel", "musicGenerationModel", "pdfModel")


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to an object")
    return data


def add_scalar(routes: dict[str, Any], path: str, value: Any) -> None:
    if value is None:
        return
    if value == "":
        return
    if isinstance(value, (str, int, float, bool)):
        routes[path] = value


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def add_secret_marker(routes: dict[str, Any], path: str, value: Any) -> None:
    routes[path] = bool(value)


def extract_model_config(routes: dict[str, Any], prefix: str, value: Any) -> None:
    if isinstance(value, str):
        routes[prefix] = value
        return
    if not isinstance(value, dict):
        return
    for key in ("provider", "default", "model", "primary", "fallback_model"):
        add_scalar(routes, f"{prefix}.{key}", value.get(key))
    fallbacks = value.get("fallbacks")
    if isinstance(fallbacks, list):
        for idx, item in enumerate(fallbacks):
            if isinstance(item, str):
                routes[f"{prefix}.fallbacks[{idx}]"] = item
            elif isinstance(item, dict):
                for key in ("provider", "model", "primary"):
                    add_scalar(routes, f"{prefix}.fallbacks[{idx}].{key}", item.get(key))


def extract_list_of_model_configs(routes: dict[str, Any], prefix: str, value: Any) -> None:
    if not isinstance(value, list):
        return
    for idx, item in enumerate(value):
        if isinstance(item, str):
            routes[f"{prefix}[{idx}]"] = item
        elif isinstance(item, dict):
            for key in ("provider", "model", "default", "primary"):
                add_scalar(routes, f"{prefix}[{idx}].{key}", item.get(key))


def extract_top_level_fallback_model(routes: dict[str, Any], value: Any) -> None:
    if isinstance(value, list):
        extract_list_of_model_configs(routes, "fallback_model", value)
    else:
        extract_model_config(routes, "fallback_model", value)


def extract_auxiliary(routes: dict[str, Any], cfg: dict[str, Any]) -> None:
    aux = cfg.get("auxiliary")
    if not isinstance(aux, dict):
        return
    for name, block in sorted(aux.items()):
        if not isinstance(block, dict):
            continue
        for key, value in sorted(block.items()):
            if is_secret_key(key):
                add_secret_marker(routes, f"auxiliary.{name}.has_{key}", value)
            elif key in {"provider", "model", "base_url", "api", "api_mode"}:
                add_scalar(routes, f"auxiliary.{name}.{key}", value)


def extract_providers(routes: dict[str, Any], cfg: dict[str, Any]) -> None:
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return
    for provider, block in sorted(providers.items()):
        base = f"providers.{provider}"
        routes[f"{base}.present"] = True
        if not isinstance(block, dict):
            continue
        for key, value in sorted(block.items()):
            lower = key.lower()
            if is_secret_key(key):
                add_secret_marker(routes, f"{base}.has_{lower}", value)
                continue
            if key == "key_env":
                add_scalar(routes, f"{base}.key_env", value)
            elif key in {"model", "provider", "base_url", "api", "api_mode"}:
                add_scalar(routes, f"{base}.{key}", value)
            elif key == "models" and isinstance(value, list):
                # Model allowlists affect reachable routes but can be large; keep stable scalar ids only.
                for idx, item in enumerate(value):
                    if isinstance(item, str):
                        routes[f"{base}.models[{idx}]"] = item
                    elif isinstance(item, dict):
                        add_scalar(routes, f"{base}.models[{idx}].id", item.get("id"))


def extract_routes(cfg: dict[str, Any]) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for key in ROUTE_MODEL_KEYS:
        if key in cfg:
            extract_model_config(routes, key, cfg.get(key))
    extract_top_level_fallback_model(routes, cfg.get("fallback_model"))
    extract_list_of_model_configs(routes, "fallback_providers", cfg.get("fallback_providers"))
    extract_model_config(routes, "delegation", cfg.get("delegation"))
    extract_auxiliary(routes, cfg)
    extract_providers(routes, cfg)
    return dict(sorted(routes.items()))


def profile_config_paths(home: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    default_cfg = home / "config.yaml"
    if default_cfg.exists():
        paths.append(("default", default_cfg))
    profiles_dir = home / "profiles"
    if profiles_dir.exists():
        for cfg in sorted(profiles_dir.glob("*/config.yaml")):
            paths.append((cfg.parent.name, cfg))
    return paths


def build_baseline(home: Path) -> dict[str, Any]:
    baseline: dict[str, Any] = {}
    for profile, path in profile_config_paths(home):
        baseline[profile] = {"config_path": str(path), "routes": extract_routes(load_yaml(path))}
    return baseline


def classify_impact(field: str) -> str:
    if field.startswith("fallback_providers") or field.startswith("fallback_model") or ".fallbacks" in field or field.endswith("fallback_model"):
        return "fallback-only route"
    if field.startswith("delegation"):
        return "delegation/subagent route"
    if field.startswith("auxiliary"):
        return "auxiliary task route"
    if field.startswith("providers"):
        return "provider availability/auth route"
    return "primary model route"


def compare_routes(profile: str, actual: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    expected = baseline.get(profile, {}).get("routes", {})
    if not isinstance(expected, dict):
        expected = {}
    drift: list[dict[str, Any]] = []
    for field in sorted(set(expected) | set(actual)):
        exp = expected.get(field)
        act = actual.get(field)
        if exp == act:
            continue
        if field not in expected:
            kind = "added"
        elif field not in actual:
            kind = "removed"
        else:
            kind = "changed"
        drift.append({
            "profile": profile,
            "field": field,
            "expected": exp,
            "actual": act,
            "kind": kind,
            "impact": classify_impact(field),
        })
    return drift


def scan_one(config: Path, profile: str, baseline_path: Path) -> dict[str, Any]:
    baseline = load_json(baseline_path)
    actual = extract_routes(load_yaml(config))
    drift = compare_routes(profile, actual, baseline)
    return {
        "status": "drift" if drift else "clean",
        "profile": profile,
        "config_path": str(config),
        "routes_count": len(actual),
        "drift_count": len(drift),
        "drift": drift,
    }


def scan_home(home: Path, baseline_path: Path) -> dict[str, Any]:
    baseline = load_json(baseline_path)
    profiles = []
    all_drift: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()
    for profile, path in profile_config_paths(home):
        seen_profiles.add(profile)
        actual = extract_routes(load_yaml(path))
        drift = compare_routes(profile, actual, baseline)
        profiles.append({"profile": profile, "config_path": str(path), "routes_count": len(actual), "drift_count": len(drift)})
        all_drift.extend(drift)
    for missing in sorted(set(baseline) - seen_profiles):
        routes = baseline.get(missing, {}).get("routes", {})
        all_drift.append({
            "profile": missing,
            "field": "profile.config",
            "expected": "present",
            "actual": None,
            "kind": "removed",
            "impact": "profile route surface removed",
            "routes_count": len(routes) if isinstance(routes, dict) else 0,
        })
    return {"status": "drift" if all_drift else "clean", "profiles": profiles, "drift_count": len(all_drift), "drift": all_drift}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Hermes provider route baseline/drift scanner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="Generate an approved-route baseline from a Hermes home")
    b.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    b.add_argument("--json", action="store_true")

    s = sub.add_parser("scan", help="Compare one config or Hermes home against a baseline")
    s.add_argument("--config", type=Path)
    s.add_argument("--profile", default="default")
    s.add_argument("--home", type=Path)
    s.add_argument("--baseline", type=Path, required=True)
    s.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "baseline":
            result = build_baseline(args.home)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.config:
            result = scan_one(args.config, args.profile, args.baseline)
        elif args.home:
            result = scan_home(args.home, args.baseline)
        else:
            parser.error("scan requires --config or --home")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if result["status"] == "drift" else 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
