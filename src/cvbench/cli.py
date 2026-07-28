from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys

from .agent import (
    DEFAULT_API_URL,
    AgentSubmissionError,
    ControlPlaneClient,
    CredentialStore,
    agent_result,
    build_project,
    docker_available,
    load_agent_project,
    submit_project,
)
from .config import load_benchmark, load_system
from .errors import CVBenchError
from .runner import run_benchmark
from .scenario import load_scenario
from .synthetic import generate_synthetic_pack


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cvbench", description="Benchmark complete online vision systems")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="run an online benchmark")
    run.add_argument("--benchmark", required=True)
    run.add_argument("--system", required=True)
    run.add_argument("--output", default="runs")
    validate = subcommands.add_parser("validate", help="validate definitions without running a SUT")
    validate.add_argument("project", nargs="?")
    validate.add_argument("--benchmark")
    validate.add_argument("--system")
    validate.add_argument("--name")
    validate.add_argument("--version")
    validate.add_argument("--dockerfile")
    login = subcommands.add_parser("login", help="store a submission credential for local agents")
    login.add_argument("--api-url", default=os.environ.get("CVBENCH_API_BASE_URL", DEFAULT_API_URL))
    login.add_argument("--token-stdin", action="store_true")
    doctor = subcommands.add_parser("doctor", help="check the local agent submission environment")
    doctor.add_argument("--api-url", default=os.environ.get("CVBENCH_API_BASE_URL", DEFAULT_API_URL))
    submit = subcommands.add_parser("submit", help="build, upload, and benchmark a local model project")
    submit.add_argument("project", nargs="?", default=".")
    submit.add_argument("--api-url", default=os.environ.get("CVBENCH_API_BASE_URL", DEFAULT_API_URL))
    submit.add_argument("--name")
    submit.add_argument("--version")
    submit.add_argument("--dockerfile")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--json", action="store_true", dest="json_output")
    status = subcommands.add_parser("status", help="read a submission and its agent feedback")
    status.add_argument("submission_id")
    status.add_argument("--api-url", default=os.environ.get("CVBENCH_API_BASE_URL", DEFAULT_API_URL))
    status.add_argument("--json", action="store_true", dest="json_output")
    scenarios = subcommands.add_parser("scenarios", help="scenario utilities")
    scenario_commands = scenarios.add_subparsers(dest="scenario_command", required=True)
    generate = scenario_commands.add_parser("generate", help="generate the deterministic public pack")
    generate.add_argument("output")
    return parser


def _print_agent_output(result: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, indent=2))
        return
    print(f"{result.get('status', 'unknown')}: {result.get('submission_id', 'unknown submission')}")
    print(f"Result: {result.get('result_url', 'unavailable')}")
    feedback = result.get("feedback")
    if isinstance(feedback, dict) and feedback.get("summary"):
        print(f"Feedback: {feedback['summary']}")
    scores = result.get("scores")
    if isinstance(scores, dict):
        for key in ("observed_coverage", "mean_iou", "id_switches", "false_track_births", "latency_p99_ms"):
            if key in scores:
                print(f"{key}: {scores[key]}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            artifacts = run_benchmark(args.benchmark, args.system, args.output)
            print(
                json.dumps(
                    {
                        "run_dir": str(artifacts.run_dir),
                        "report_json": str(artifacts.report_json),
                        "report_html": str(artifacts.report_html),
                    },
                    indent=2,
                )
            )
        elif args.command == "validate":
            if args.project:
                project = load_agent_project(
                    args.project,
                    name=args.name,
                    version=args.version,
                    dockerfile=args.dockerfile,
                )
                image = build_project(project, progress=lambda message: print(message, file=sys.stderr))
                print(json.dumps({
                    "schema_version": "cvbench.agent-validation/v1",
                    "project": str(project.root),
                    "name": project.name,
                    "version": project.version,
                    "dockerfile": str(project.dockerfile),
                    "context": str(project.context),
                    "image_id": image.image_id,
                    "command": image.command,
                    "platform": "linux/amd64",
                }, indent=2))
            else:
                if not args.benchmark or not args.system:
                    raise AgentSubmissionError("provide a project directory or both --benchmark and --system")
                benchmark = load_benchmark(args.benchmark)
                system = load_system(args.system)
                for path in benchmark.scenarios:
                    load_scenario(path)
                print(f"valid: benchmark={benchmark.id} system={system.id}")
        elif args.command == "login":
            token = sys.stdin.read().strip() if args.token_stdin else getpass.getpass("CVBench submission credential: ")
            CredentialStore(args.api_url).save(token)
            print(f"Stored the CVBench agent credential for {args.api_url}.")
        elif args.command == "doctor":
            token = CredentialStore(args.api_url).load()
            client = ControlPlaneClient(args.api_url, token)
            health = client.health()
            result = {
                "schema_version": "cvbench.agent-doctor/v1",
                "docker": "ready" if docker_available() else "unavailable",
                "credential": "ready",
                "service": health,
            }
            print(json.dumps(result, indent=2))
            if result["docker"] != "ready":
                return 2
        elif args.command == "submit":
            project = load_agent_project(
                args.project,
                name=args.name,
                version=args.version,
                dockerfile=args.dockerfile,
            )
            token = CredentialStore(args.api_url).load()
            client = ControlPlaneClient(args.api_url, token)
            result = submit_project(
                project,
                client,
                wait=args.wait,
                progress=lambda message: print(message, file=sys.stderr),
            )
            _print_agent_output(result, json_output=args.json_output)
            if result["status"] == "failed":
                return 1
        elif args.command == "status":
            client = ControlPlaneClient(args.api_url, "")
            result = agent_result(client.submission(args.submission_id), args.api_url)
            _print_agent_output(result, json_output=args.json_output)
            if result["status"] == "failed":
                return 1
        elif args.command == "scenarios" and args.scenario_command == "generate":
            paths = generate_synthetic_pack(args.output)
            print(f"generated {len(paths)} scenarios in {args.output}")
    except (
        AgentSubmissionError,
        CVBenchError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"cvbench: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
