.PHONY: dev install build clean

dev:
	python -m uvicorn zettabrain_lite.server:app --reload --port 7860

install:
	pip install -e ".[all]"

build:
	python -m build

clean:
	rm -rf dist/ build/ *.egg-info
