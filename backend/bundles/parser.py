"""Bidirectional parser, serializer, and variable resolution engine for Lake Asset Bundles (LABs)."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any

import yaml

try:
    from backend.bundles.schemas import (
        BundleSpec,
        PipelineResource,
        TargetSpec,
    )
    from backend.toon import dump_toon, parse_toon
except ImportError:
    from toon import dump_toon, parse_toon

    from .schemas import (
        BundleSpec,
        PipelineResource,
        TargetSpec,
    )

VARIABLE_REGEX = re.compile(r"\$\{([^}]+)\}")


class BundleError(Exception):
    """Base exception for Lake Asset Bundle errors."""


class BundleParseError(BundleError):
    """Raised when bundle manifest syntax is invalid."""


class BundleValidationError(BundleError):
    """Raised when bundle manifest fails semantic or dependency validation."""


class BundleVariableError(BundleError):
    """Raised when variable interpolation fails or circular references are detected."""


def _normalize_resource_list(raw_val: Any, name_field: str = "name") -> list[dict[str, Any]]:
    """Normalize a resource collection that may be specified as a list, table, or dict mapping."""
    if not raw_val:
        return []

    if isinstance(raw_val, list):
        results: list[dict[str, Any]] = []
        for item in raw_val:
            if isinstance(item, dict):
                results.append(dict(item))
        return results

    if isinstance(raw_val, dict):
        results = []
        for key, val in raw_val.items():
            if isinstance(val, dict):
                item = dict(val)
                item.setdefault(name_field, key)
                results.append(item)
            elif val is None:
                results.append({name_field: key})
        return results

    return []


def _normalize_columns(raw_cols: Any) -> list[dict[str, Any]]:
    """Normalize column definitions from list, table, or dict."""
    if not raw_cols:
        return []
    if isinstance(raw_cols, list):
        return [dict(c) if isinstance(c, dict) else {"name": str(c)} for c in raw_cols]
    if isinstance(raw_cols, dict):
        cols: list[dict[str, Any]] = []
        for col_name, col_spec in raw_cols.items():
            if isinstance(col_spec, dict):
                c = dict(col_spec)
                c.setdefault("name", col_name)
                cols.append(c)
            elif isinstance(col_spec, str):
                cols.append({"name": col_name, "type": col_spec.upper()})
            else:
                cols.append({"name": col_name, "type": "VARCHAR"})
        return cols
    return []


def _normalize_tasks(raw_tasks: Any) -> list[dict[str, Any]]:
    """Normalize pipeline tasks from list, table, or dict."""
    if not raw_tasks:
        return []
    if isinstance(raw_tasks, list):
        return [
            dict(t) if isinstance(t, dict) else {"name": str(t), "entrypoint": ""}
            for t in raw_tasks
        ]
    if isinstance(raw_tasks, dict):
        tasks: list[dict[str, Any]] = []
        for task_name, task_spec in raw_tasks.items():
            if isinstance(task_spec, dict):
                t = dict(task_spec)
                t.setdefault("name", task_name)
                tasks.append(t)
            elif isinstance(task_spec, str):
                tasks.append({"name": task_name, "entrypoint": task_spec})
            else:
                tasks.append({"name": task_name, "entrypoint": ""})
        return tasks
    return []


def _normalize_widgets(raw_widgets: Any) -> list[dict[str, Any]]:
    """Normalize dashboard widgets from list, table, or dict."""
    if not raw_widgets:
        return []
    if isinstance(raw_widgets, list):
        return [dict(w) if isinstance(w, dict) else {"title": str(w)} for w in raw_widgets]
    if isinstance(raw_widgets, dict):
        widgets: list[dict[str, Any]] = []
        for title, widget_spec in raw_widgets.items():
            if isinstance(widget_spec, dict):
                w = dict(widget_spec)
                w.setdefault("title", title)
                widgets.append(w)
            elif isinstance(widget_spec, str):
                widgets.append({"title": title, "query": widget_spec})
            else:
                widgets.append({"title": title})
        return widgets
    return []


def normalize_bundle_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw parsed dictionary (from TOON, YAML, or JSON) into a standard BundleSpec dictionary."""
    normalized: dict[str, Any] = {}

    # 1. Bundle metadata
    if "bundle" in data and isinstance(data["bundle"], dict):
        normalized["bundle"] = dict(data["bundle"])
    else:
        # Fallback: top-level metadata fields
        normalized["bundle"] = {
            "name": data.get("name") or data.get("bundle_name") or "unnamed_bundle",
            "version": data.get("version", "0.1.0"),
            "author": data.get("author"),
            "description": data.get("description"),
        }

    # 2. Variables
    raw_vars = data.get("variables") or {}
    norm_vars: dict[str, Any] = {}
    if isinstance(raw_vars, dict):
        for var_name, var_spec in raw_vars.items():
            if isinstance(var_spec, dict):
                norm_vars[var_name] = var_spec
            else:
                norm_vars[var_name] = {"default": var_spec, "type": "string"}
    normalized["variables"] = norm_vars

    # 3. Targets
    raw_targets = data.get("targets") or {}
    norm_targets: dict[str, Any] = {}
    if isinstance(raw_targets, dict):
        for target_name, target_spec in raw_targets.items():
            if isinstance(target_spec, dict):
                norm_targets[target_name] = target_spec
            else:
                norm_targets[target_name] = {"workspace": str(target_spec)}
    normalized["targets"] = norm_targets

    # 4. Resources
    raw_resources = data.get("resources") or {}
    if not isinstance(raw_resources, dict):
        raw_resources = {}

    norm_resources: dict[str, Any] = {
        "catalogs": _normalize_resource_list(raw_resources.get("catalogs")),
        "schemas": _normalize_resource_list(raw_resources.get("schemas")),
        "tables": [],
        "vector_indexes": _normalize_resource_list(raw_resources.get("vector_indexes")),
        "pipelines": [],
        "dashboards": [],
    }

    # Normalize tables with nested column definitions
    raw_tables = _normalize_resource_list(raw_resources.get("tables"))
    for tbl in raw_tables:
        t_copy = dict(tbl)
        if "columns" in t_copy:
            t_copy["columns"] = _normalize_columns(t_copy["columns"])
        norm_resources["tables"].append(t_copy)

    # Normalize pipelines with nested task lists
    raw_pipelines = _normalize_resource_list(raw_resources.get("pipelines"))
    for pipe in raw_pipelines:
        p_copy = dict(pipe)
        sched = p_copy.get("schedule")
        if sched and str(sched).strip().lower() in ("@webhook", "webhook"):
            p_copy["webhook_enabled"] = True
            p_copy["schedule"] = None
        if "webhook_enabled" in p_copy:
            p_copy["webhook_enabled"] = bool(p_copy["webhook_enabled"])
        if "tasks" in p_copy:
            p_copy["tasks"] = _normalize_tasks(p_copy["tasks"])
        norm_resources["pipelines"].append(p_copy)

    # Normalize dashboards with nested widget lists
    raw_dashboards = _normalize_resource_list(raw_resources.get("dashboards"))
    for dash in raw_dashboards:
        d_copy = dict(dash)
        if "widgets" in d_copy:
            d_copy["widgets"] = _normalize_widgets(d_copy["widgets"])
        norm_resources["dashboards"].append(d_copy)

    normalized["resources"] = norm_resources
    return normalized


def parse_bundle_str(content: str, format: str | None = None) -> BundleSpec:
    """Parse a bundle manifest string (TOON, YAML, or JSON) into a validated BundleSpec."""
    content_stripped = textwrap.dedent(content).strip()
    if not content_stripped:
        raise BundleParseError("Manifest content is empty")

    parsed_dict: dict[str, Any] | None = None

    # Format detection
    target_format = (format or "").lower().strip()
    if not target_format:
        if content_stripped.startswith("{") and content_stripped.endswith("}"):
            target_format = "json"
        elif "[" in content_stripped and "]{" in content_stripped:
            # TOON tabular array indicator
            target_format = "toon"
        else:
            # Default to TOON with YAML fallback
            target_format = "auto"

    if target_format == "json":
        try:
            parsed_dict = json.loads(content_stripped)
        except json.JSONDecodeError as e:
            raise BundleParseError(f"Failed to parse JSON manifest: {e}") from e

    elif target_format == "yaml" or target_format == "yml":
        try:
            parsed_dict = yaml.safe_load(content_stripped)
        except yaml.YAMLError as e:
            raise BundleParseError(f"Failed to parse YAML manifest: {e}") from e

    elif target_format == "toon":
        try:
            parsed_dict = parse_toon(content_stripped)
        except Exception as e:
            raise BundleParseError(f"Failed to parse TOON manifest: {e}") from e

    else:
        # Auto-detect: try TOON first, fallback to YAML
        try:
            parsed_dict = parse_toon(content_stripped)
            # If TOON parsed into an empty dict or failed to extract bundle, verify with YAML
            if not parsed_dict or "bundle" not in parsed_dict:
                yaml_candidate = yaml.safe_load(content_stripped)
                if isinstance(yaml_candidate, dict) and (
                    "bundle" in yaml_candidate or "resources" in yaml_candidate
                ):
                    parsed_dict = yaml_candidate
        except Exception:
            try:
                parsed_dict = yaml.safe_load(content_stripped)
            except Exception as e:
                raise BundleParseError(f"Failed to parse manifest as TOON or YAML: {e}") from e

    if not isinstance(parsed_dict, dict):
        raise BundleParseError(
            f"Parsed manifest must be a mapping/object, got {type(parsed_dict).__name__}"
        )

    try:
        normalized = normalize_bundle_dict(parsed_dict)
        spec = BundleSpec.model_validate(normalized)
    except Exception as e:
        raise BundleValidationError(f"Bundle specification validation failed: {e}") from e

    # Perform structural and dependency checks
    validate_bundle(spec)
    return spec


def parse_bundle_file(filepath: str | Path) -> BundleSpec:
    """Read and parse a bundle manifest file (.toon, .yml, .yaml, or .json)."""
    path = Path(filepath)
    if not path.exists():
        raise BundleParseError(f"Bundle manifest file not found: {path}")

    suffix = path.suffix.lower()
    format_hint = None
    if suffix == ".toon":
        format_hint = "toon"
    elif suffix in (".yaml", ".yml"):
        format_hint = "yaml"
    elif suffix == ".json":
        format_hint = "json"

    content = path.read_text(encoding="utf-8")
    return parse_bundle_str(content, format=format_hint)


def validate_pipeline_dependencies(pipeline: PipelineResource) -> None:
    """Validate that tasks within a pipeline form a valid Directed Acyclic Graph (DAG) with no cycles."""
    task_names = {t.name for t in pipeline.tasks}

    # Check for duplicate task names
    seen = set()
    for t in pipeline.tasks:
        if t.name in seen:
            raise BundleValidationError(
                f"Pipeline '{pipeline.name}' has duplicate task identifier: '{t.name}'"
            )
        seen.add(t.name)

    # Check upstream references
    for task in pipeline.tasks:
        for upstream in task.upstream_tasks:
            if upstream not in task_names:
                raise BundleValidationError(
                    f"Pipeline '{pipeline.name}' task '{task.name}' references non-existent upstream task '{upstream}'"
                )
            if upstream == task.name:
                raise BundleValidationError(
                    f"Pipeline '{pipeline.name}' task '{task.name}' cannot depend on itself"
                )

    # Detect cycles using 3-state DFS
    # 0 = unvisited, 1 = visiting (in current recursion stack), 2 = visited
    state: dict[str, int] = {t.name: 0 for t in pipeline.tasks}
    adj: dict[str, list[str]] = {t.name: list(t.upstream_tasks) for t in pipeline.tasks}

    def dfs(node: str, path: list[str]) -> None:
        state[node] = 1
        path.append(node)
        for dep in adj.get(node, []):
            if state[dep] == 1:
                cycle_path = " -> ".join(path + [dep])
                raise BundleValidationError(
                    f"Pipeline '{pipeline.name}' contains a circular dependency cycle: {cycle_path}"
                )
            if state[dep] == 0:
                dfs(dep, path)
        path.pop()
        state[node] = 2

    for task in pipeline.tasks:
        if state[task.name] == 0:
            dfs(task.name, [])


def validate_bundle(spec: BundleSpec) -> list[str]:
    """Perform comprehensive semantic checks on a BundleSpec and return any warnings."""
    warnings: list[str] = []

    # 1. Validate pipelines for DAG cycles
    for pipe in spec.resources.pipelines:
        validate_pipeline_dependencies(pipe)

    # 2. Check for duplicate table definitions
    table_signatures = set()
    for tbl in spec.resources.tables:
        cat = tbl.catalog or ""
        sch = tbl.schema_name or ""
        sig = f"{cat}.{sch}.{tbl.name}".lower()
        if sig in table_signatures:
            warnings.append(f"Duplicate table declaration detected: '{sig}'")
        table_signatures.add(sig)

    # 3. Check for vector index metric validity
    valid_metrics = {"cosine", "l2", "ip", "dot_product"}
    for idx in spec.resources.vector_indexes:
        if idx.metric.lower() not in valid_metrics:
            raise BundleValidationError(
                f"Vector index '{idx.name}' specifies unsupported metric '{idx.metric}'. "
                f"Supported metrics: {', '.join(sorted(valid_metrics))}"
            )

    return warnings


def resolve_bundle(
    spec: BundleSpec,
    target: str | None = None,
    variables_override: dict[str, Any] | None = None,
) -> BundleSpec:
    """Resolve variable substitutions across all bundle resources for a target deployment environment."""
    active_vars: dict[str, Any] = {}

    # 1. Base default variables
    for var_name, var_spec in spec.variables.items():
        if var_spec.default is not None:
            active_vars[var_name] = var_spec.default

    # 2. Target variables
    target_spec: TargetSpec | None = None
    if target:
        if target in spec.targets:
            target_spec = spec.targets[target]
            for k, v in target_spec.variables.items():
                active_vars[k] = v
        elif spec.targets:
            raise BundleVariableError(
                f"Target environment '{target}' is not defined in bundle. Available targets: {list(spec.targets.keys())}"
            )
        else:
            target_spec = TargetSpec()

    # 3. Explicit overrides
    if variables_override:
        for k, v in variables_override.items():
            active_vars[k] = v

    # 4. Built-in contextual tokens
    workspace_id = target_spec.workspace if target_spec and target_spec.workspace else ""
    context_tokens = {
        "bundle.name": spec.bundle.name,
        "bundle.version": spec.bundle.version,
        "target.name": target or "default",
        "workspace.id": workspace_id,
        "workspace.slug": workspace_id,
    }

    # Iterative variable resolution for variables referencing other variables
    # e.g. path: "${var.base_bucket}/bronze"
    resolved_vars: dict[str, str] = {}
    for k, v in active_vars.items():
        resolved_vars[k] = str(v) if v is not None else ""

    max_depth = 10
    for _ in range(max_depth):
        changed = False
        for k, val_str in list(resolved_vars.items()):
            matches = VARIABLE_REGEX.findall(val_str)
            for m in matches:
                token = m.strip()
                if token.startswith("var."):
                    ref_key = token[4:]
                    if ref_key == k:
                        raise BundleVariableError(f"Variable '{k}' directly references itself")
                    if ref_key in resolved_vars:
                        sub = resolved_vars[ref_key]
                        new_val = val_str.replace(f"${{{m}}}", sub)
                        if new_val != val_str:
                            resolved_vars[k] = new_val
                            val_str = new_val
                            changed = True
                elif token in context_tokens:
                    sub = context_tokens[token]
                    new_val = val_str.replace(f"${{{m}}}", sub)
                    if new_val != val_str:
                        resolved_vars[k] = new_val
                        val_str = new_val
                        changed = True
        if not changed:
            break

    # Deep substitution function
    def _substitute_value(val: Any) -> Any:
        if isinstance(val, str):
            res = val
            for _ in range(max_depth):
                matches = VARIABLE_REGEX.findall(res)
                if not matches:
                    break
                prev_res = res
                for m in matches:
                    token = m.strip()
                    if token.startswith("var."):
                        var_name = token[4:]
                        if var_name not in resolved_vars:
                            raise BundleVariableError(
                                f"Undefined variable '${{{token}}}' encountered during bundle resolution"
                            )
                        replacement = resolved_vars[var_name]
                        res = res.replace(f"${{{m}}}", replacement)
                    elif token in context_tokens:
                        res = res.replace(f"${{{m}}}", context_tokens[token])
                    else:
                        raise BundleVariableError(
                            f"Unknown interpolation token '${{{token}}}'. Supported prefixes: 'var.', 'bundle.', 'target.', 'workspace.'"
                        )
                if res == prev_res:
                    break
            return res
        elif isinstance(val, dict):
            return {k: _substitute_value(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [_substitute_value(item) for item in val]
        return val

    # Dump current spec to dict and perform recursive substitution
    dumped = spec.model_dump(by_alias=True)

    # Preserve metadata and targets, substitute resources and target variables
    dumped["resources"] = _substitute_value(dumped["resources"])
    if target and target in dumped.get("targets", {}):
        dumped["targets"][target] = _substitute_value(dumped["targets"][target])

    resolved_spec = BundleSpec.model_validate(dumped)
    return resolved_spec


def dump_bundle_to_str(spec: BundleSpec, format: str = "toon") -> str:
    """Serialize a BundleSpec into TOON, YAML, or JSON format."""
    fmt = format.lower().strip()
    data = spec.model_dump(by_alias=True, exclude_none=True)

    if fmt == "json":
        return json.dumps(data, indent=2)

    if fmt in ("yaml", "yml"):
        return yaml.dump(data, sort_keys=False)

    # Default to TOON format
    # Build clean TOON structure
    toon_dict: dict[str, Any] = {
        "bundle": data.get("bundle", {}),
    }

    if data.get("variables"):
        toon_dict["variables"] = data["variables"]

    if data.get("targets"):
        toon_dict["targets"] = data["targets"]

    resources = data.get("resources", {})
    toon_resources: dict[str, Any] = {}

    if resources.get("catalogs"):
        toon_resources["catalogs"] = resources["catalogs"]
    if resources.get("schemas"):
        toon_resources["schemas"] = resources["schemas"]
    if resources.get("tables"):
        toon_resources["tables"] = resources["tables"]
    if resources.get("vector_indexes"):
        toon_resources["vector_indexes"] = resources["vector_indexes"]
    if resources.get("pipelines"):
        toon_resources["pipelines"] = resources["pipelines"]
    if resources.get("dashboards"):
        toon_resources["dashboards"] = resources["dashboards"]

    if toon_resources:
        toon_dict["resources"] = toon_resources

    return dump_toon(toon_dict)
