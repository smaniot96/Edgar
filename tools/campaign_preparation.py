"""Private operator inspection and exact response-ID reconciliation for graph v2.

Reconciliation never submits a model request. Resume is a separate explicit action
through the application's existing retry endpoint after the receipt is verified.
"""

import argparse
import asyncio
import json

from apps.api.services.adventures_meta import load_meta
from apps.api.services.campaign_prep.provider import SemanticClient
from apps.api.services.campaign_prep.store import VERSION, Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug")
    parser.add_argument("action", choices=("inspect", "reconcile"))
    parser.add_argument("--dispatch-id")
    parser.add_argument("--response-id")
    args = parser.parse_args()
    if load_meta(args.slug).get("preparation_workflow") != VERSION:
        parser.error("Archived preparations are held and immutable.")
    store = Store(args.slug)
    if args.action == "reconcile":
        if not args.dispatch_id or not args.response_id:
            parser.error("Reconciliation needs both exact dispatch and response IDs.")
        asyncio.run(SemanticClient(store).reconcile(args.dispatch_id, args.response_id))
    print(json.dumps({"state": store.state(), "usage": store.usage()}, indent=2))


if __name__ == "__main__":
    main()
