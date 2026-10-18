from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compose_airflow_base_url_has_no_trailing_slash():
    compose = (ROOT / "docker-compose.yml").read_text()
    match = re.search(r"AIRFLOW__WEBSERVER__BASE_URL:\s*(\S+)", compose)
    assert match, "AIRFLOW__WEBSERVER__BASE_URL is not set in docker-compose.yml"
    # Airflow 2.10 refuses to start: "webserver.base_url conf cannot have a trailing slash"
    assert not match.group(1).endswith("/")


def test_nginx_keeps_the_airflow_prefix():
    conf = (ROOT / "deploy" / "nginx" / "lake.conf").read_text()
    block = re.search(r"location /airflow/ \{(.*?)\n    \}", conf, re.S)
    assert block, "location /airflow/ not found in deploy/nginx/lake.conf"
    proxy = re.search(r"proxy_pass\s+(\S+);", block.group(1))
    assert proxy, "no proxy_pass in the /airflow/ location"
    # A URI after host:port (even a bare "/") makes nginx strip the /airflow/ prefix.
    assert re.fullmatch(r"https?://[^/]+", proxy.group(1)), proxy.group(1)
