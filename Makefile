.PHONY: setup validate dag test lint fmt check clean docs-setup docs-sync docs-serve docs-build

PY := .venv/bin/python

setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

validate:
	$(PY) tools/validate.py \
		--shapes shapes/core_shapes.ttl \
		--data benchmarks/datasets/sample_data.ttl \
		--data benchmarks/datasets/domain_tracking.ttl
	$(PY) tools/validate.py \
		--shapes shapes/domain_shapes.ttl \
		--data benchmarks/datasets/sample_data.ttl \
		--data benchmarks/datasets/domain_tracking.ttl

dag:
	$(PY) tools/check_dependency_dag.py

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

versions:
	$(PY) tools/manage_ontology.py check-versions

fmt:
	.venv/bin/ruff check . --fix
	.venv/bin/ruff format .

visualize-ontology:
	$(PY) tools/visualize_ontology.py

ontology-report:
	$(PY) tools/manage_ontology.py report
	$(PY) tools/manage_ontology.py stats

benchmark:
	$(PY) tools/benchmark.py --scale 1000 --observations 3

slo:
	$(PY) tools/check_slo.py

check: lint versions validate dag test slo


clean:
	find . -name __pycache__ -type d -exec rm -rf {} +
	rm -rf .pytest_cache

console:
	$(PY) -m uvicorn foundry.console.app:app --port 8787

seed-console:
	$(PY) tools/seed_console_data.py

docs-setup:
	$(PY) -m pip install -r requirements-docs.txt

docs-sync:
	mkdir -p docs/generated
	cp roadmap.md docs/generated/roadmap.md
	cp CODING_CONVENTIONS.md docs/generated/conventions.md
	cp requirements/performance_slo.md docs/generated/performance_slo.md
	cp requirements/competency_questions.md docs/generated/competency_questions.md
	cp requirements/scale_targets.md docs/generated/scale_targets.md

docs-build: docs-sync
	$(PY) -m mkdocs build --strict

docs-serve: docs-sync
	$(PY) -m mkdocs serve
