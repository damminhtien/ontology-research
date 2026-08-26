.PHONY: setup validate dag test lint fmt check clean

PY := .venv/bin/python

setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

validate:
	$(PY) tools/validate.py \
		--shapes shapes/core_shapes.ttl \
		--data benchmarks/datasets/sample_data.ttl

dag:
	$(PY) tools/check_dependency_dag.py

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

fmt:
	.venv/bin/ruff check . --fix
	.venv/bin/ruff format .

check: lint validate dag test

clean:
	find . -name __pycache__ -type d -exec rm -rf {} +
	rm -rf .pytest_cache
