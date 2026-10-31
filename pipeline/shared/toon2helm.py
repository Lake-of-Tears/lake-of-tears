from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from typing import Any

import yaml

from pipeline.shared.toon import parse_toon


def _normalize_resources(res: dict[str, Any]) -> dict[str, Any]:
    """Convert flat cpu_request/mem_request/cpu_limit/mem_limit to Helm requests/limits."""
    out: dict[str, Any] = {}
    requests: dict[str, str] = {}
    limits: dict[str, str] = {}

    if "requests" in res and isinstance(res["requests"], dict):
        requests.update(res["requests"])
    if "limits" in res and isinstance(res["limits"], dict):
        limits.update(res["limits"])

    if "cpu_request" in res and res["cpu_request"] is not None:
        requests["cpu"] = str(res["cpu_request"])
    if "mem_request" in res and res["mem_request"] is not None:
        requests["memory"] = str(res["mem_request"])
    if "cpu_limit" in res and res["cpu_limit"] is not None:
        limits["cpu"] = str(res["cpu_limit"])
    if "mem_limit" in res and res["mem_limit"] is not None:
        limits["memory"] = str(res["mem_limit"])

    if requests:
        out["requests"] = requests
    if limits:
        out["limits"] = limits

    # Preserve any other extra resource settings
    for k, v in res.items():
        if k not in ("requests", "limits", "cpu_request", "mem_request", "cpu_limit", "mem_limit"):
            out[k] = v

    return out


def toon_to_helm_dict(toon_data: dict[str, Any]) -> dict[str, Any]:
    """Convert parsed TOON platform configuration into standard Helm values dictionary."""
    helm = deepcopy(toon_data)

    # 1. Normalize service resources
    for section in ("minio", "lakeui", "jupyter", "superset", "airflow", "backend", "postgres"):
        if section in helm and isinstance(helm[section], dict):
            if "resources" in helm[section] and isinstance(helm[section]["resources"], dict):
                helm[section]["resources"] = _normalize_resources(helm[section]["resources"])

    # 2. Normalize ingress TLS hosts
    if "ingress" in helm and isinstance(helm["ingress"], dict):
        ingress = helm["ingress"]
        if "tls" in ingress and isinstance(ingress["tls"], list):
            normalized_tls = []
            for item in ingress["tls"]:
                if isinstance(item, dict):
                    entry = deepcopy(item)
                    hosts_val = entry.get("hosts")
                    if isinstance(hosts_val, str):
                        entry["hosts"] = [h.strip() for h in hosts_val.split(",") if h.strip()]
                    normalized_tls.append(entry)
                else:
                    normalized_tls.append(item)
            ingress["tls"] = normalized_tls

        # 3. Derive lakeui URLs if omitted
        if "lakeui" in helm and isinstance(helm["lakeui"], dict):
            lakeui = helm["lakeui"]
            if not lakeui.get("minioConsoleUrl") and ingress.get("minioConsoleHost"):
                proto = "https" if ingress.get("enabled", True) else "http"
                lakeui["minioConsoleUrl"] = f"{proto}://{ingress['minioConsoleHost']}"
            if not lakeui.get("supersetUrl") and ingress.get("supersetHost"):
                proto = "https" if ingress.get("enabled", True) else "http"
                lakeui["supersetUrl"] = f"{proto}://{ingress['supersetHost']}"

    # 4. Expand backend.oauth tabular array into Helm provider objects
    if "backend" in helm and isinstance(helm["backend"], dict):
        backend = helm["backend"]
        oauth_list = backend.pop("oauth", None)
        if isinstance(oauth_list, list):
            for row in oauth_list:
                if isinstance(row, dict) and "provider" in row:
                    provider = str(row["provider"]).strip().lower()
                    provider_conf: dict[str, Any] = {}
                    if "enabled" in row and row["enabled"] is not None:
                        val = row["enabled"]
                        if isinstance(val, bool):
                            provider_conf["enabled"] = val
                        elif isinstance(val, str):
                            provider_conf["enabled"] = val.strip().lower() in ("true", "1", "yes")
                        else:
                            provider_conf["enabled"] = bool(val)
                    if "clientId" in row and row["clientId"] is not None:
                        provider_conf["clientId"] = str(row["clientId"])
                    if "clientSecret" in row and row["clientSecret"] is not None:
                        provider_conf["clientSecret"] = str(row["clientSecret"])
                    if "tenantId" in row and row["tenantId"]:
                        provider_conf["tenantId"] = str(row["tenantId"])
                    if "teamId" in row and row["teamId"]:
                        provider_conf["teamId"] = str(row["teamId"])
                    if "keyId" in row and row["keyId"]:
                        provider_conf["keyId"] = str(row["keyId"])
                    if "domain" in row and row["domain"]:
                        provider_conf["domain"] = str(row["domain"])
                    if "url" in row and row["url"]:
                        provider_conf["url"] = str(row["url"])
                    if provider == "apple" and "clientSecret" in provider_conf:
                        provider_conf["privateKey"] = provider_conf["clientSecret"]
                    backend[provider] = provider_conf

    return helm


def toon_to_helm_yaml(data_or_text: str | dict[str, Any]) -> str:
    """Translate TOON text or dictionary directly into formatted Helm values YAML string."""
    if isinstance(data_or_text, str):
        parsed = parse_toon(data_or_text)
    else:
        parsed = data_or_text

    helm_dict = toon_to_helm_dict(parsed)
    return yaml.dump(helm_dict, sort_keys=False, default_flow_style=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile Lake of Tears TOON specification into Helm values.yaml"
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="input_file",
        help="Path to .toon file (reads from stdin if omitted or '-')",
        default=None,
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output_file",
        help="Path to output values.yaml file (prints to stdout if omitted)",
        default=None,
    )

    args = parser.parse_args()

    if args.input_file and args.input_file != "-":
        with open(args.input_file, encoding="utf-8") as f:
            content = f.read()
    else:
        content = sys.stdin.read()

    yaml_output = toon_to_helm_yaml(content)

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(yaml_output)
    else:
        sys.stdout.write(yaml_output)


if __name__ == "__main__":
    main()
