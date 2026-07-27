from __future__ import annotations

import argparse
import json
import sys

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
    validate.add_argument("--benchmark", required=True)
    validate.add_argument("--system", required=True)
    scenarios = subcommands.add_parser("scenarios", help="scenario utilities")
    scenario_commands = scenarios.add_subparsers(dest="scenario_command", required=True)
    generate = scenario_commands.add_parser("generate", help="generate the deterministic public pack")
    generate.add_argument("output")
    return parser


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
            benchmark = load_benchmark(args.benchmark)
            system = load_system(args.system)
            for path in benchmark.scenarios:
                load_scenario(path)
            print(f"valid: benchmark={benchmark.id} system={system.id}")
        elif args.command == "scenarios" and args.scenario_command == "generate":
            paths = generate_synthetic_pack(args.output)
            print(f"generated {len(paths)} scenarios in {args.output}")
    except (CVBenchError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cvbench: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
