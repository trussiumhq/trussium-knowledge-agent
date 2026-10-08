"""Invoke the registered, read-only link audit through Trussium's Python SDK."""

from __future__ import annotations

import json
import os

from trussium_sdk import TrussiumClient, WorkflowRequest


def main() -> None:
    """Run one declared link-audit tool call and print its bounded report."""
    runtime_url = os.getenv("TRUSSIUM_URL", "http://127.0.0.1:9000")
    workflow: WorkflowRequest = {
        "steps": [
            {
                "id": "audit-doc-links",
                "invocation": {
                    "name": "knowledge.audit_links",
                    "arguments": {"max_findings": 100},
                },
            }
        ],
        "deadline_seconds": 30.0,
        "depth": 1,
    }

    with TrussiumClient(runtime_url) as client:
        result = client.execute_workflow(workflow, request_id="docs-audit-001")

    print(json.dumps(result, indent=2))
    if result["status"] != "completed":
        raise SystemExit(f"Documentation audit ended with status: {result['status']}")


if __name__ == "__main__":
    main()
