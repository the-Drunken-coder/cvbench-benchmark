#!/usr/bin/env python3
"""Exercise the trusted runner's fail-closed callback from an unhydrated checkout."""

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
            if callback.get("status") == "failed":
                if (
                    set(callback) != {"status", "lease_token", "error"}
                    or callback.get("lease_token") != LEASE_TOKEN
                    or not isinstance(callback.get("error"), str)
                    or not 1 <= len(callback["error"]) <= 2000
                ):
                    self._json(422, {"error": "invalid failed callback"})
                    return
                self.__class__.callback = callback
                self._json(200, {"accepted": True})
                return
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
    if not callback or callback.get("status") != "failed" or callback.get("lease_token") != LEASE_TOKEN:
        raise RuntimeError(f"trusted runner did not return a valid fail-closed callback: {callback}")
    error = callback.get("error")
    if not isinstance(error, str) or "prepared MOTChallenge corpus is missing" not in error:
        raise RuntimeError(f"trusted runner returned the wrong clean-checkout failure: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="immutable linux/amd64 image reference")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    hydrated = root / "data" / "real-video-v2"
    mot_data = root / "data" / "motchallenge-v1"
    if hydrated.exists():
        raise SystemExit("fresh-checkout regression requires data/real-video-v2 to be absent")
    if mot_data.exists():
        raise SystemExit("fresh-checkout regression requires data/motchallenge-v1 to be absent")

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
        completed = subprocess.run(
            [sys.executable, "scripts/run_control_plane_job.py"],
            cwd=root,
            env=environment,
            timeout=3600,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert_callback(ControlPlaneHandler.callback)
    if completed.returncode != 1:
        raise RuntimeError(f"trusted runner returned {completed.returncode}, expected fail-closed status 1")
    if not (hydrated / "artifacts.sha256").is_file():
        raise RuntimeError("trusted runner did not deterministically hydrate the public corpus")
    if mot_data.exists():
        raise RuntimeError("trusted runner must not download MOTChallenge archives while a lease is active")
    print("fresh checkout lease -> fail-closed missing-MOT callback verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
