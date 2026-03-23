.PHONY: install test lint run

install:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -x -q

lint:
	python -m py_compile src/aros_meta_loop/main.py

run:
	uvicorn aros_meta_loop.main:app --host 0.0.0.0 --port 8200 --reload
