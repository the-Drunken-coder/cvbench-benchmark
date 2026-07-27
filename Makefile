.PHONY: install test lint validate e2e docker-build docker-e2e

install:
	python -m pip install -e '.[dev]'

test:
	pytest

lint:
	ruff check .

validate:
	cvbench validate --benchmark benchmarks/persistent-target-tracking.yaml --system systems/example-good-local.yaml

e2e:
	cvbench run --benchmark benchmarks/persistent-target-tracking.yaml --system systems/example-good-local.yaml --output runs/

docker-build:
	docker build -f examples/Dockerfile.good -t cvbench-example-good:v1 .

docker-e2e: docker-build
	cvbench run --benchmark benchmarks/persistent-target-tracking.yaml --system systems/example-good-docker.yaml --output docker-runs/
	python scripts/assert_docker_report.py docker-runs/
