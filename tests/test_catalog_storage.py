from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from catalog_storage import (  # noqa: E402
    compute_prefix_storage_metrics,
    delete_prefix,
    is_pattern_in_workspace,
    prefixes_to_delete,
    s3_prefix_for_pattern,
    validate_catalog_patterns_for_workspace,
)

B = "datalake"


def test_prefix_strips_globs_to_directory():
    p = "s3://datalake/prod/rev/charges/**/*.parquet"
    assert s3_prefix_for_pattern(p, B) == "prod/rev/charges/"


def test_prefix_with_partial_glob_segment():
    p = "s3://datalake/raw/charges/year=*/*.parquet"
    assert s3_prefix_for_pattern(p, B) == "raw/charges/"


def test_prefix_without_glob_uses_parent_directory():
    assert s3_prefix_for_pattern("s3://datalake/a/b/file.parquet", B) == "a/b/"


def test_prefix_rejects_unsafe_patterns():
    assert s3_prefix_for_pattern("s3://datalake/*.parquet", B) is None
    assert s3_prefix_for_pattern("s3://datalake/", B) is None
    assert s3_prefix_for_pattern("s3://other/a/b/*.parquet", B) is None
    assert s3_prefix_for_pattern("a/b/*.parquet", B) is None


def test_prefixes_to_delete_skips_data_shared_with_other_catalogs():
    own = ["s3://datalake/raw/charges/**/*.parquet", "s3://datalake/mine/t1/*.parquet"]
    other = ["s3://datalake/raw/charges/year=*/*.parquet"]
    assert prefixes_to_delete(own, other) == ["mine/t1/"]


def test_prefixes_to_delete_dedupes():
    own = ["s3://datalake/mine/t1/*.parquet", "s3://datalake/mine/t1/**/*.parquet"]
    assert prefixes_to_delete(own, []) == ["mine/t1/"]


def test_delete_prefix_deletes_all_pages():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "a/1"}, {"Key": "a/2"}]},
        {},
        {"Contents": [{"Key": "a/3"}]},
    ]
    assert delete_prefix("a/", B, client=s3) == 3
    assert s3.delete_objects.call_count == 2
    s3.get_paginator.return_value.paginate.assert_called_once_with(Bucket=B, Prefix="a/")


def test_is_pattern_in_workspace():
    slug = "analytics"
    valid = "s3://datalake/workspaces/analytics/raw/**/*.parquet"
    invalid = "s3://datalake/workspaces/marketing/raw/**/*.parquet"
    root = "s3://datalake/raw/**/*.parquet"

    assert is_pattern_in_workspace(valid, slug, B) is True
    assert is_pattern_in_workspace(invalid, slug, B) is False
    assert is_pattern_in_workspace(root, slug, B) is False


def test_validate_catalog_patterns_for_workspace():
    slug = "analytics"
    patterns = [
        "s3://datalake/workspaces/analytics/t1/*.parquet",
        "s3://datalake/workspaces/marketing/t2/*.parquet",
        "s3://datalake/raw/t3/*.parquet",
    ]
    invalid = validate_catalog_patterns_for_workspace(patterns, slug, B)
    assert len(invalid) == 2
    assert "s3://datalake/workspaces/marketing/t2/*.parquet" in invalid
    assert "s3://datalake/raw/t3/*.parquet" in invalid


def test_compute_prefix_storage_metrics():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Contents": [
                {"Key": "workspaces/analytics/raw/stripe/data.parquet", "Size": 1024},
                {"Key": "workspaces/analytics/raw/shopify/orders.parquet", "Size": 2048},
                {"Key": "workspaces/analytics/models/churn.parquet", "Size": 4096},
            ]
        }
    ]

    metrics = compute_prefix_storage_metrics("workspaces/analytics/", bucket=B, client=s3)
    assert metrics["prefix"] == "workspaces/analytics/"
    assert metrics["total_objects"] == 3
    assert metrics["total_bytes"] == 7168
    assert metrics["total_size_human"] == "7.0 KB"
    assert "raw" in metrics["subprefixes"]
    assert "models" in metrics["subprefixes"]
    assert metrics["subprefixes"]["raw"]["objects"] == 2
    assert metrics["subprefixes"]["models"]["objects"] == 1
