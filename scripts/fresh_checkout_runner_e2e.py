#!/usr/bin/env python3
"""Exercise lease -> trusted Docker run -> scored callback from an unhydrated checkout."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cvbench.reporting import validate_report

try:
    from scripts.run_control_plane_job import (
        PUBLIC_BENCHMARK_ID,
        PUBLIC_BENCHMARK_MANIFEST,
        PUBLIC_BENCHMARK_VERSION,
        PUBLIC_DELIVERY_POLICY,
        PUBLIC_LEADERBOARD_POLICY,
        PUBLIC_REPLAY_PROFILE,
        PUBLIC_REPLAY_RATE,
        PUBLIC_SCENARIO_IDS,
        PUBLIC_TIMING_COMPUTE_CONTRACT,
    )
except ModuleNotFoundError:  # Direct `python scripts/fresh_checkout_runner_e2e.py` execution.
    from run_control_plane_job import (  # type: ignore[no-redef]
        PUBLIC_BENCHMARK_ID,
        PUBLIC_BENCHMARK_MANIFEST,
        PUBLIC_BENCHMARK_VERSION,
        PUBLIC_DELIVERY_POLICY,
        PUBLIC_LEADERBOARD_POLICY,
        PUBLIC_REPLAY_PROFILE,
        PUBLIC_REPLAY_RATE,
        PUBLIC_SCENARIO_IDS,
        PUBLIC_TIMING_COMPUTE_CONTRACT,
    )

SUBMISSION_ID = "12345678-1234-4123-8123-123456789abc"
LEASE_TOKEN = "lease-token-" + "a" * 52
RUNNER_TOKEN = "runner-token-for-local-fresh-checkout-e2e"


class ControlPlaneHandler(BaseHTTPRequestHandler):
    callback: dict[str, Any] | None = None
    image: str = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.headers.get("Authorization") != f"Bearer {RUNNER_TOKEN}":
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/api/v1/internal/leases":
            self._json(
                200,
                {
                    "submission": {
                        "id": SUBMISSION_ID,
                        "image": self.image,
                        "argv": ["python", "-m", "cvbench.examples.good_tracker"],
                        "model": {"version": "fresh-checkout-e2e"},
                        "benchmark": {
                            "id": PUBLIC_BENCHMARK_ID,
                            "version": PUBLIC_BENCHMARK_VERSION,
                            "manifest": PUBLIC_BENCHMARK_MANIFEST,
                            "timing_compute_contract": PUBLIC_TIMING_COMPUTE_CONTRACT,
                            "delivery_policy": PUBLIC_DELIVERY_POLICY,
                            "replay_profile": PUBLIC_REPLAY_PROFILE,
                            "replay_rate": PUBLIC_REPLAY_RATE,
                            "leaderboard_policy": PUBLIC_LEADERBOARD_POLICY,
                        },
                    },
                    "lease": {"token": LEASE_TOKEN, "max_result_bytes": 1024 * 1024},
                },
            )
            return
        if self.path == f"/api/v1/internal/submissions/{SUBMISSION_ID}/result":
            length = int(self.headers.get("Content-Length", "0"))
            callback = json.loads(self.rfile.read(length))
            report = callback.get("report")
            try:
                validate_report(report)
                if report["outcome"]["resolved_image"] != self.image:
                    raise ValueError("callback image does not match lease")
                if report["system"]["command"] != ["python", "-m", "cvbench.examples.good_tracker"]:
                    raise ValueError("callback command does not match lease")
            except (KeyError, TypeError, ValueError) as exc:
                self._json(422, {"error": str(exc)})
                return
            self.__class__.callback = callback
            self._json(200, {"accepted": True})
            return
        self._json(404, {"error": "not found"})


def assert_callback(callback: dict[str, Any] | None) -> None:
    if not callback or callback.get("status") != "succeeded" or callback.get("lease_token") != LEASE_TOKEN:
        raise RuntimeError(f"trusted runner did not return a successful callback: {callback}")
    report = callback.get("report", {})
    if report.get("outcome", {}).get("status") != "completed":
        raise RuntimeError("callback report is not completed")
    benchmark = report.get("benchmark", {})
    if (benchmark.get("id"), benchmark.get("version")) != (PUBLIC_BENCHMARK_ID, PUBLIC_BENCHMARK_VERSION):
        raise RuntimeError("callback report has the wrong benchmark identity")
    scenarios = report.get("provenance", {}).get("comparison_inputs", {}).get("scenarios", [])
    ids = [scenario.get("id") for scenario in scenarios if isinstance(scenario, dict)]
    if len(ids) != len(PUBLIC_SCENARIO_IDS) or len(set(ids)) != len(ids) or set(ids) != PUBLIC_SCENARIO_IDS:
        raise RuntimeError("callback report does not contain exactly the 16 public scenarios")
    isolation = report.get("runtime_isolation", {})
    if isolation.get("status") != "verified" or isolation.get("ground_truth_access") is not False:
        raise RuntimeError("callback report did not verify runner isolation")
    runner = report.get("runner", {})
    if set(runner) != {"schema_version", "commit", "workflow_run_url", "workflow_name"}:
        raise RuntimeError("callback report has incomplete trusted-runner metadata")
    if runner.get("schema_version") != "cvbench.runner/v1":
        raise RuntimeError("callback report has the wrong trusted-runner metadata version")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="immutable linux/amd64 image reference")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    hydrated = root / "data" / "real-video-v2"
    if hydrated.exists():
        raise SystemExit("fresh-checkout regression requires data/real-video-v2 to be absent")

    ControlPlaneHandler.image = args.image
    ControlPlaneHandler.callback = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update(
        {
            "CVBENCH_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "CVBENCH_RUNNER_TOKEN": RUNNER_TOKEN,
        }
    )
    try:
        subprocess.run(
            [sys.executable, "scripts/run_control_plane_job.py"],
            cwd=root,
            env=environment,
            timeout=1800,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert_callback(ControlPlaneHandler.callback)
    if not (hydrated / "artifacts.sha256").is_file():
        raise RuntimeError("trusted runner did not deterministically hydrate the public corpus")
    print("fresh checkout lease -> 16-scenario Docker score -> callback verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
