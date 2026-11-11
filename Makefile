PY := .venv/bin/python

.PHONY: install test lint demo sample

install:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check radar tests

demo:
	$(PY) -m radar demo

# Rebuild the bundled synthetic database and the example brief (same rows every time).
sample:
	$(PY) -m radar demo --quiet --db sample/radar_sample.sqlite --out data/demo/runs --overwrite
	cp data/demo/runs/LATEST.md sample/example-brief.md
