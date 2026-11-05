#!/usr/bin/env python3
"""CLI utility for Lake of Tears lifecycle management, TOON discovery, secret generation, and update checking."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.shared.lifecycle import (  # noqa: E402
    check_platform_updates,
    find_toon_config,
    generate_secure_secrets,
    scan_placeholder_secrets,
)
from pipeline.shared.toon import parse_toon  # noqa: E402


def cmd_find_config(args: argparse.Namespace) -> int:
    config_path = find_toon_config(explicit_path=args.config, repo_root=REPO_ROOT)
    if config_path and config_path.is_file():
        print(str(config_path))
        return 0
    return 1


def cmd_scan_placeholders(args: argparse.Namespace) -> int:
    path = Path(args.input) if args.input else find_toon_config(repo_root=REPO_ROOT)
    if not path or not path.is_file():
        sys.stderr.write(f"Configuration file not found: {args.input}\n")
        return 2

    content = path.read_text(encoding="utf-8")
    placeholders = scan_placeholder_secrets(content)
    if placeholders:
        for ph in placeholders:
            print(ph)
        return 1
    return 0


def cmd_generate_secrets(args: argparse.Namespace) -> int:
    input_path = Path(args.input) if args.input else find_toon_config(repo_root=REPO_ROOT)
    if not input_path or not input_path.is_file():
        sys.stderr.write(f"Configuration file not found: {args.input}\n")
        return 2

    content = input_path.read_text(encoding="utf-8")
    updated, replacements = generate_secure_secrets(content)

    output_path = input_path if args.in_place else (Path(args.output) if args.output else None)
    if output_path:
        # Create backup if updating in-place
        if args.in_place and not args.no_backup:
            bak_path = input_path.with_suffix(input_path.suffix + ".bak")
            bak_path.write_text(content, encoding="utf-8")
        output_path.write_text(updated, encoding="utf-8")
        if not args.quiet:
            sys.stderr.write(f"Generated {len(replacements)} secure secrets into {output_path}\n")
    else:
        sys.stdout.write(updated)

    return 0


def cmd_check_updates(args: argparse.Namespace) -> int:
    result = check_platform_updates(
        train=args.train,
        repo_root=REPO_ROOT,
        fetch_remote=not args.no_fetch,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Update Train:     {result['train']}")
        print(f"Current Version:  {result['current_version']}")
        print(f"Latest Version:   {result['latest_version']}")
        print(f"Update Available: {'Yes' if result['can_update'] else 'No'}")
        if result["changelog"]:
            print("\nRecent Changes:")
            for item in result["changelog"][:10]:
                print(f"  * {item}")
    return 0 if not result["can_update"] else 10


def cmd_extract_urls(args: argparse.Namespace) -> int:
    input_path = Path(args.input) if args.input else find_toon_config(repo_root=REPO_ROOT)
    if not input_path or not input_path.is_file():
        sys.stderr.write(f"Configuration file not found: {args.input}\n")
        return 2

    data = parse_toon(input_path.read_text(encoding="utf-8"))
    ingress = data.get("ingress", {})
    lakeui = data.get("lakeui", {})
    minio = data.get("minio", {})
    superset = data.get("superset", {})

    enabled = ingress.get("enabled", True)
    proto = "https" if enabled else "http"

    lake_host = ingress.get("host") or "localhost"
    lake_port = lakeui.get("service", {}).get("port", 3000)
    lake_url = (
        f"{proto}://{lake_host}"
        if enabled and ingress.get("host")
        else f"http://localhost:{lake_port}"
    )

    minio_host = ingress.get("minioConsoleHost")
    minio_port = minio.get("service", {}).get("consolePort", 9001)
    minio_url = (
        f"{proto}://{minio_host}" if enabled and minio_host else f"http://localhost:{minio_port}"
    )

    superset_host = ingress.get("supersetHost")
    superset_port = superset.get("service", {}).get("port", 8088)
    superset_url = (
        f"{proto}://{superset_host}"
        if enabled and superset_host
        else f"http://localhost:{superset_port}"
    )

    pipelines_url = f"{lake_url}/airflow/"

    urls = {
        "lake_ui": lake_url,
        "pipelines": pipelines_url,
        "minio_console": minio_url,
        "superset": superset_url,
        "ingress_enabled": enabled,
    }

    if args.json:
        print(json.dumps(urls, indent=2))
    else:
        for k, v in urls.items():
            print(f"{k}: {v}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Lake of Tears Platform Lifecycle Helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # find-config
    p_find = subparsers.add_parser("find-config", help="Locate TOON configuration file")
    p_find.add_argument("-c", "--config", help="Explicit configuration file path")
    p_find.set_defaults(func=cmd_find_config)

    # scan-placeholders
    p_scan = subparsers.add_parser("scan-placeholders", help="Scan for changeme-* placeholders")
    p_scan.add_argument("-i", "--input", help="Path to TOON file")
    p_scan.set_defaults(func=cmd_scan_placeholders)

    # generate-secrets
    p_gen = subparsers.add_parser("generate-secrets", help="Auto-generate secure secrets")
    p_gen.add_argument("-i", "--input", help="Path to input TOON file")
    p_gen.add_argument("-o", "--output", help="Path to output file")
    p_gen.add_argument("--in-place", action="store_true", help="Modify input file in-place")
    p_gen.add_argument("--no-backup", action="store_true", help="Do not create .bak backup")
    p_gen.add_argument("-q", "--quiet", action="store_true", help="Suppress status output")
    p_gen.set_defaults(func=cmd_generate_secrets)

    # check-updates
    p_upd = subparsers.add_parser(
        "check-updates", help="Check for platform updates (TrueNAS-style)"
    )
    p_upd.add_argument("--train", default="stable", choices=["stable", "edge"], help="Update train")
    p_upd.add_argument("--json", action="store_true", help="Output JSON format")
    p_upd.add_argument("--no-fetch", action="store_true", help="Do not fetch remote Git repository")
    p_upd.set_defaults(func=cmd_check_updates)

    # extract-urls
    p_urls = subparsers.add_parser("extract-urls", help="Extract service URLs from configuration")
    p_urls.add_argument("-i", "--input", help="Path to TOON file")
    p_urls.add_argument("--json", action="store_true", help="Output JSON format")
    p_urls.set_defaults(func=cmd_extract_urls)

    args = parser.parse_args()
    code = args.func(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
