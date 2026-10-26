from __future__ import annotations

import re
from typing import Any

# Regex to match TOON tabular array headers: key[N]{col1,col2,...}:
TABLE_HEADER_REGEX = re.compile(r"^([\w-]+)\[(\d*)\]\{([^}]+)\}:\s*$")


def _parse_scalar(val: str) -> Any:
    val = val.strip()
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.lower() in ("null", "none"):
        return None
    # Integer
    if re.match(r"^-?\d+$", val):
        try:
            return int(val)
        except ValueError:
            pass
    # Float
    if re.match(r"^-?\d+\.\d+$", val):
        try:
            return float(val)
        except ValueError:
            pass
    # Strip optional quotes if present
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def parse_toon(text: str) -> dict[str, Any]:
    """Parse Token-Oriented Object Notation (TOON) string into a Python dictionary.

    Supports:
    - Standard key: value mappings
    - Indented nested object sections
    - TOON array tables: name[count]{field1,field2,...}:
        val1,val2,...
        val3,val4,...
    - Inline comma-separated lists: tags: [a, b, c]
    """
    lines = text.splitlines()
    result: dict[str, Any] = {}
    i = 0
    total_lines = len(lines)

    while i < total_lines:
        raw_line = lines[i]
        line = raw_line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            i += 1
            continue

        # Check for tabular array: key[N]{col1,col2}:
        tbl_match = TABLE_HEADER_REGEX.match(line)
        if tbl_match:
            key_name = tbl_match.group(1)
            columns = [c.strip() for c in tbl_match.group(3).split(",")]
            rows: list[dict[str, Any]] = []

            i += 1
            while i < total_lines:
                curr = lines[i]
                curr_stripped = curr.strip()
                if not curr_stripped or curr_stripped.startswith("#"):
                    i += 1
                    continue
                # Row must be indented or follow array block
                if not curr.startswith(" ") and not curr.startswith("\t"):
                    break

                # Parse CSV-style values for columns
                row_vals = [c.strip() for c in curr_stripped.split(",")]
                row_dict = {}
                for idx, col in enumerate(columns):
                    val = row_vals[idx] if idx < len(row_vals) else None
                    row_dict[col] = _parse_scalar(val) if val is not None else None
                rows.append(row_dict)
                i += 1

            result[key_name] = rows
            continue

        # Check for standard key: value
        if ":" in line:
            key, _, val_part = line.partition(":")
            key = key.strip()
            val_part = val_part.strip()

            # Inline list: [a, b, c]
            if val_part.startswith("[") and val_part.endswith("]"):
                inner = val_part[1:-1].strip()
                items = [_parse_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
                result[key] = items
                i += 1
                continue

            # Non-empty scalar
            if val_part:
                result[key] = _parse_scalar(val_part)
                i += 1
                continue

            # Empty scalar value could be a nested object
            nested_block: list[str] = []
            i += 1
            while i < total_lines:
                curr = lines[i]
                curr_stripped = curr.strip()
                if not curr_stripped or curr_stripped.startswith("#"):
                    i += 1
                    continue
                if not curr.startswith(" ") and not curr.startswith("\t"):
                    break
                nested_block.append(curr)
                i += 1

            if nested_block:
                # Deduce common leading indent
                dedented = "\n".join(line.lstrip() for line in nested_block)
                result[key] = parse_toon(dedented)
            else:
                result[key] = None
            continue

        i += 1

    return result


def dump_toon(data: dict[str, Any]) -> str:
    """Serialize a Python dictionary into TOON format."""
    lines: list[str] = []
    for k, v in data.items():
        if isinstance(v, list):
            if v and isinstance(v[0], dict):
                # Format as TOON table: key[count]{cols...}:
                cols = list(v[0].keys())
                lines.append(f"{k}[{len(v)}]{{{','.join(cols)}}}:")
                for row in v:
                    row_vals = [str(row.get(c, "")) for c in cols]
                    lines.append(f"  {','.join(row_vals)}")
            else:
                inner = ", ".join(str(item) for item in v)
                lines.append(f"{k}: [{inner}]")
        elif isinstance(v, dict):
            lines.append(f"{k}:")
            for sub_k, sub_v in v.items():
                lines.append(f"  {sub_k}: {sub_v}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"
