from __future__ import annotations

from pipeline.shared.toon import dump_toon, parse_toon


def test_toon_multi_level_nesting():
    doc = """
global:
  storageClass: "local-path"
  guard:
    active: true
    threshold: 10

minio:
  persistence:
    size: 200Gi
    accessMode: ReadWriteOnce
  resources:
    requests:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 2
      memory: 4Gi
"""
    parsed = parse_toon(doc)
    assert parsed["global"]["storageClass"] == "local-path"
    assert parsed["global"]["guard"]["active"] is True
    assert parsed["global"]["guard"]["threshold"] == 10
    assert parsed["minio"]["persistence"]["size"] == "200Gi"
    assert parsed["minio"]["resources"]["requests"]["cpu"] == "500m"
    assert parsed["minio"]["resources"]["requests"]["memory"] == "1Gi"
    assert parsed["minio"]["resources"]["limits"]["cpu"] == 2
    assert parsed["minio"]["resources"]["limits"]["memory"] == "4Gi"


def test_toon_tabular_with_quoted_commas():
    doc = """
ingress:
  tls[2]{secretName,hosts}:
    lake-tls,"lake.example.com,minio.example.com"
    wildcard-tls,*.example.com
"""
    parsed = parse_toon(doc)
    assert len(parsed["ingress"]["tls"]) == 2
    assert parsed["ingress"]["tls"][0]["secretName"] == "lake-tls"
    assert parsed["ingress"]["tls"][0]["hosts"] == "lake.example.com,minio.example.com"
    assert parsed["ingress"]["tls"][1]["secretName"] == "wildcard-tls"
    assert parsed["ingress"]["tls"][1]["hosts"] == "*.example.com"


def test_toon_inline_comments():
    doc = """
# Platform configuration
global:
  storageClass: "local-path"  # default k3s storage provisioner
  allowDefaultNamespace: false # security guard

service:
  port: 8080 # default port
  tags: [analytics, production] # service tags

connectors[2]{id,enabled,schedule}:
  stripe,true,0 2 * * * # daily financial sync
  shopify,false,0 3 * * * # e-commerce sync
"""
    parsed = parse_toon(doc)
    assert parsed["global"]["storageClass"] == "local-path"
    assert parsed["global"]["allowDefaultNamespace"] is False
    assert parsed["service"]["port"] == 8080
    assert parsed["service"]["tags"] == ["analytics", "production"]
    assert len(parsed["connectors"]) == 2
    assert parsed["connectors"][0]["id"] == "stripe"
    assert parsed["connectors"][0]["enabled"] is True
    assert parsed["connectors"][0]["schedule"] == "0 2 * * *"
    assert parsed["connectors"][1]["id"] == "shopify"
    assert parsed["connectors"][1]["enabled"] is False


def test_toon_roundtrip_fidelity():
    data = {
        "global": {
            "storageClass": "fast-ssd",
            "replicas": 3,
            "enabled": True,
            "debug": False,
            "extra": None,
        },
        "services": [
            {"name": "api", "port": 8000, "domains": "api.domain.com,api2.domain.com"},
            {"name": "web", "port": 3000, "domains": "web.domain.com"},
        ],
        "tags": ["prod", "us-east-1"],
    }

    dumped = dump_toon(data)
    reparsed = parse_toon(dumped)

    assert reparsed["global"]["storageClass"] == "fast-ssd"
    assert reparsed["global"]["replicas"] == 3
    assert reparsed["global"]["enabled"] is True
    assert reparsed["global"]["debug"] is False
    assert reparsed["global"]["extra"] is None
    assert len(reparsed["services"]) == 2
    assert reparsed["services"][0]["domains"] == "api.domain.com,api2.domain.com"
    assert reparsed["tags"] == ["prod", "us-east-1"]
