from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from catalog_storage import delete_prefix, prefixes_to_delete, s3_prefix_for_pattern  # noqa: E402

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
