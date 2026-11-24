#!/usr/bin/env python3
"""Amazon OpenSearch Service demo — index, search, aggregate, health-check.

Works against any OpenSearch endpoint:
  * Local Docker:  docker run -d -p 9200:9200 -e "discovery.type=single-node" \\
                      opensearchproject/opensearch:2.15.0
  * A real domain:  pass --endpoint https://<domain-endpoint> and --region;
                    requests are signed with SigV4 (AWS Signature Version 4)
                    using the default boto3 credential chain.

Usage:
    python opensearch_demo.py --endpoint http://localhost:9200 --demo
    python opensearch_demo.py --endpoint https://search-x.ap-southeast-1.es.amazonaws.com \\
        --region ap-southeast-1 --demo
    python opensearch_demo.py --endpoint http://localhost:9200 --health
    python opensearch_demo.py --endpoint http://localhost:9200 --cost

Requires: opensearch-py >= 2.0, boto3 (only for SigV4 against AWS domains).
    pip install opensearch-py boto3 requests-aws4auth
"""

from __future__ import annotations

import argparse
import json
import sys

INDEX = "products"
SHARD_TARGET_GB = 50  # rule of thumb: 10-50 GB per shard


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def build_client(endpoint: str, region: str, aws: bool):
    """Return an OpenSearch client, SigV4-signed when talking to AWS."""
    from opensearchpy import OpenSearch, RequestsHttpConnection

    if aws:
        from boto3 import Session
        from requests_aws4auth import AWS4Auth

        creds = Session().get_credentials()
        auth = AWS4Auth(
            creds.access_key, creds.secret_key, region, "es",
            session_token=creds.token,
        )
        return OpenSearch(
            hosts=[endpoint], http_auth=auth, use_ssl=True, verify_certs=True,
            connection_class=RequestsHttpConnection, timeout=30,
        )
    return OpenSearch(hosts=[endpoint], http_auth=("admin", "admin"),
                      use_ssl=endpoint.startswith("https"), timeout=30)


# ---------------------------------------------------------------------------
# Index setup — explicit mapping FIRST, documents second
# ---------------------------------------------------------------------------
MAPPING = {
    "settings": {
        "number_of_shards": 1,          # ~50 GB of data per shard is the goal
        "number_of_replicas": 1,        # 1 replica = survives one AZ failure
        "index": {"max_result_window": 1000},
    },
    "mappings": {
        "dynamic": "strict",            # bad documents fail loudly, not silently
        "properties": {
            "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "description": {"type": "text"},
            "brand": {"type": "keyword"},
            "category": {"type": "keyword"},
            "price": {"type": "float"},
            "in_stock": {"type": "boolean"},
            "rating": {"type": "float"},
        },
    },
}

PRODUCTS = [
    {"name": "Aurora Wireless Headphones", "description": "Noise-cancelling wireless headphones with 30-hour battery",
     "brand": "Aurora", "category": "audio", "price": 89.99, "in_stock": True, "rating": 4.6},
    {"name": "Aurora Wired Earbuds", "description": "Budget wired earbuds with in-line mic",
     "brand": "Aurora", "category": "audio", "price": 12.50, "in_stock": True, "rating": 4.1},
    {"name": "Nimbus Mechanical Keyboard", "description": "Hot-swappable mechanical keyboard, RGB backlight",
     "brand": "Nimbus", "category": "accessories", "price": 119.00, "in_stock": False, "rating": 4.8},
    {"name": "Nimbus Wireless Mouse", "description": "Ergonomic wireless mouse, silent clicks",
     "brand": "Nimbus", "category": "accessories", "price": 34.99, "in_stock": True, "rating": 4.4},
    {"name": "Vertex 4K Webcam", "description": "4K webcam with auto-framing for video calls",
     "brand": "Vertex", "category": "video", "price": 149.00, "in_stock": True, "rating": 4.5},
]


def create_index(client) -> None:
    if client.indices.exists(index=INDEX):
        print(f"Index '{INDEX}' already exists — deleting and recreating.")
        client.indices.delete(index=INDEX)
    client.indices.create(index=INDEX, body=MAPPING)
    print(f"Created index '{INDEX}' (1 primary shard, 1 replica, dynamic=strict).")


def bulk_index(client) -> None:
    from opensearchpy.helpers import bulk

    actions = [
        {"_index": INDEX, "_id": i + 1, "_source": doc}
        for i, doc in enumerate(PRODUCTS)
    ]
    ok, errors = bulk(client, actions, refresh=True)
    print(f"Indexed {ok} documents ({len(errors)} errors).")


# ---------------------------------------------------------------------------
# Search — full text + filter + aggregation + highlight
# ---------------------------------------------------------------------------
def search(client, query: str) -> None:
    body = {
        "query": {
            "bool": {
                "must": {"multi_match": {"query": query,
                                         "fields": ["name^3", "description"]}},
                "filter": [{"term": {"in_stock": True}}],
            }
        },
        "aggs": {
            "by_brand": {"terms": {"field": "brand"}},
            "avg_price": {"avg": {"field": "price"}},
        },
        "highlight": {"fields": {"description": {}}},
        "size": 5,  # never ask for 10,000 — paginate with search_after instead
    }
    resp = client.search(index=INDEX, body=body)
    print(f"\nSearch '{query}': {resp['hits']['total']['value']} hits")
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        print(f"  [{hit['_score']:.2f}] {src['name']} — ${src['price']:.2f} ({src['brand']})")
    print("  Brands:", {b["key"]: b["doc_count"]
                         for b in resp["aggregations"]["by_brand"]["buckets"]})
    print(f"  Avg price: ${resp['aggregations']['avg_price']['value']:.2f}")


# ---------------------------------------------------------------------------
# Health & cost sanity checks
# ---------------------------------------------------------------------------
def health(client) -> None:
    h = client.cluster.health()
    print(f"Cluster: {h['cluster_name']} — status {h['status'].upper()} "
          f"({h['number_of_nodes']} nodes, {h['number_of_data_nodes']} data nodes)")
    if h["status"] == "red":
        print("  RED: some primary shards are unassigned — check disk and node logs.")
    elif h["status"] == "yellow":
        print("  YELLOW: replicas unassigned — usually a node or disk issue.")

    stats = client.cluster.stats()
    jvm = stats["nodes"]["jvm"]["mem"]["heap_used_percent"]
    print(f"  JVM heap used: {jvm}%  (alarm at 75%, page at 85%)")
    disk = client.cat.allocation(format="json")
    for node in disk:
        print(f"  Node {node['node'][:12]:12} disk: {node['disk.avail']} free "
              f"of {node['disk.total']}")
    print("  Writes block when free disk drops below 5% — keep ISM deletes running.")


def cost_report(client) -> None:
    stats = client.indices.stats(index=INDEX)
    store = stats["indices"][INDEX]["total"]["store"]["size_in_bytes"] / 1e9
    shards = len(stats["indices"][INDEX]["shards"])
    print(f"\nIndex '{INDEX}': {store:.2f} GB across {shards} shards "
          f"({store / max(shards, 1):.2f} GB/shard, target 10–50 GB)")
    if shards > 10 and store < 5:
        print("  WARNING: many shards for little data — over-sharding taxes heap "
              "and slows recovery. Merge indexes or lower shard counts.")
    else:
        print("  Shard sizing looks sane.")
    print("  Cost levers: ISM rollover + UltraWarm for old data, "
          "drop dev replicas, right-size instance types.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OpenSearch Service demo")
    p.add_argument("--endpoint", required=True, help="e.g. http://localhost:9200")
    p.add_argument("--region", default="ap-southeast-1")
    p.add_argument("--aws", action="store_true",
                   help="Sign requests with SigV4 (real AWS domain)")
    p.add_argument("--demo", action="store_true", help="Full demo: create, index, search")
    p.add_argument("--health", action="store_true", help="Cluster health check only")
    p.add_argument("--cost", action="store_true", help="Shard/cost sanity report only")
    args = p.parse_args(argv)

    try:
        client = build_client(args.endpoint, args.region, args.aws)
    except Exception as exc:  # noqa: BLE001 - surface connection errors plainly
        print(f"Could not build client: {exc}", file=sys.stderr)
        return 1

    if args.health or args.demo:
        health(client)
    if args.demo:
        create_index(client)
        bulk_index(client)
        search(client, "wireless headphones")
        search(client, "keyboard")
    if args.cost or args.demo:
        cost_report(client)
    if not (args.demo or args.health or args.cost):
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
