"""Exercise real HTTP boundaries between the SDK, runtime, and MCP tool server."""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from trussium_sdk import APIError, TrussiumClient, WorkflowRequest  # type: ignore[import-untyped]

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class WorkflowServices:
    runtime_url: str
    marker_directory: Path
    source_root: Path
    processes: tuple[subprocess.Popen[bytes], ...]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_http(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    last_status: int | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, stderr = process.communicate()
            detail = stderr.decode("utf-8", errors="replace") if stderr else ""
            raise RuntimeError(
                f"An integration service exited before becoming healthy: {detail.strip()}"
            )
        try:
            response = httpx.get(url, timeout=0.25, trust_env=False)
        except httpx.HTTPError:
            time.sleep(0.05)
            continue
        last_status = response.status_code
        if response.status_code == 200:
            return
        time.sleep(0.05)
    process.terminate()
    _, stderr = process.communicate(timeout=5)
    detail = stderr.decode("utf-8", errors="replace") if stderr else ""
    raise TimeoutError(
        "An integration service did not become healthy within 30 seconds "
        f"(last status: {last_status}): {detail.strip()}"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.fixture(scope="module")
def services(tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorkflowServices]:
    root = tmp_path_factory.mktemp("workflow-boundary")
    source_root = root / "docs"
    source_root.mkdir()
    (source_root / "guide.md").write_text(
        "# Example guide\n\nSee the [missing page](missing.md).\n", encoding="utf-8"
    )
    marker_directory = root / "markers"
    runtime_port = _free_port()
    mcp_port = _free_port()
    token = secrets.token_hex(32)
    mcp_url = f"http://127.0.0.1:{mcp_port}/v1/mcp"
    environment = os.environ.copy()
    environment.update(
        {
            "KNOWLEDGE_AGENT_ALLOW_LOCAL_HTTP": "true",
            "KNOWLEDGE_AGENT_AUDIT_ROOT": str(source_root),
            "KNOWLEDGE_AGENT_MCP_URL": mcp_url,
            "KNOWLEDGE_AGENT_TOOL_TOKEN": token,
            "WORKFLOW_TEST_MARKER_DIRECTORY": str(marker_directory),
        }
    )
    mcp_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "trussium_knowledge_agent.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(mcp_port),
            "--log-level",
            "error",
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    runtime_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "integration_tests.runtime_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(runtime_port),
            "--log-level",
            "error",
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_http(f"http://127.0.0.1:{mcp_port}/health/live", mcp_process)
        _wait_for_http(f"http://127.0.0.1:{runtime_port}/health/live", runtime_process)
        yield WorkflowServices(
            runtime_url=f"http://127.0.0.1:{runtime_port}",
            marker_directory=marker_directory,
            source_root=source_root,
            processes=(runtime_process, mcp_process),
        )
    finally:
        _stop_process(runtime_process)
        _stop_process(mcp_process)


def _workflow(
    name: str,
    arguments: dict[str, object],
    *,
    deadline: float = 10,
) -> WorkflowRequest:
    return {
        "steps": [{"id": "integration-step", "invocation": {"name": name, "arguments": arguments}}],
        "deadline_seconds": deadline,
        "depth": 1,
    }


def test_sdk_executes_registered_audit_across_real_processes(services: WorkflowServices) -> None:
    with TrussiumClient(services.runtime_url) as client:
        result = client.execute_workflow(
            _workflow("knowledge.audit_links", {"max_findings": 20}),
            request_id="cross-process-audit-001",
        )

    assert result["status"] == "completed"
    assert len(result["steps"]) == 1
    report = result["steps"][0]["output"]
    assert report["files_checked"] == 1
    assert report["links_checked"] == 1
    assert report["findings"] == [
        {
            "rule_id": "link-target-missing",
            "source_path": "guide.md",
            "line": 3,
            "target": "missing.md",
            "message": "The local link target does not exist beneath the source root.",
        }
    ]


def test_unregistered_tool_is_rejected_by_runtime(services: WorkflowServices) -> None:
    with TrussiumClient(services.runtime_url) as client, pytest.raises(APIError) as error:
        client.execute_workflow(_workflow("knowledge.unregistered", {}))

    assert error.value.status_code == 404
    assert error.value.code == "tool_not_found"


def test_caller_cannot_override_operator_selected_source_path(services: WorkflowServices) -> None:
    with TrussiumClient(services.runtime_url) as client, pytest.raises(APIError) as error:
        client.execute_workflow(
            _workflow(
                "knowledge.audit_links",
                {"max_findings": 20, "source_root": str(services.source_root.parent)},
            )
        )

    assert error.value.status_code == 422
    assert error.value.code == "invalid_tool_arguments"


def test_workflow_deadline_cancels_the_running_tool(services: WorkflowServices) -> None:
    marker_id = secrets.token_hex(16)
    with TrussiumClient(services.runtime_url) as client:
        result = client.execute_workflow(
            _workflow(
                "test.wait",
                {"marker_id": marker_id, "wait_seconds": 5},
                deadline=0.25,
            )
        )

    assert result["status"] == "timed_out"
    assert (services.marker_directory / f"{marker_id}.started").exists()
    assert (services.marker_directory / f"{marker_id}.cancelled").exists()


def test_client_disconnect_cancels_the_running_tool(services: WorkflowServices) -> None:
    marker_id = secrets.token_hex(16)
    asyncio.run(_cancel_client_request(services, marker_id))


async def _cancel_client_request(services: WorkflowServices, marker_id: str) -> None:
    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
        request = asyncio.create_task(
            client.post(
                f"{services.runtime_url}/v1/workflows/executions",
                headers={"X-Request-ID": f"disconnect-{marker_id}"},
                json=_workflow(
                    "test.wait",
                    {"marker_id": marker_id, "wait_seconds": 5},
                    deadline=10,
                ),
            )
        )
        await _wait_for_marker(services.marker_directory / f"{marker_id}.started")
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    await _wait_for_marker(services.marker_directory / f"{marker_id}.cancelled")


async def _wait_for_marker(path: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Expected workflow marker was not created: {path.name}")
