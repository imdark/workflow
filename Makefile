.PHONY: help test test-all test-destroy lint typecheck install

help:
	@echo "Available commands:"
	@echo "  make test         - Run non-destructive integration tests"
	@echo "  make test-all     - Run all integration tests (including destructive)"
	@echo "  make lint        - Run linting"
	@echo "  make typecheck   - Run type checking"
	@echo "  make install     - Install dependencies"

test:
	@echo "Running non-destructive integration tests..."
	@pip install -q pytest requests aiohttp pyppeteer 2>/dev/null || true
	@pytest tests/integration/ -v --ignore=tests/integration/test_browser_destructive.py

test-all:
	@echo "Running all integration tests..."
	@pip install -q pytest requests aiohttp pyppeteer 2>/dev/null || true
	@pytest tests/integration/ -v

lint:
	@echo "Running linting..."
	ruff check .

typecheck:
	@echo "Running type checking..."
	mypy workflow/ --ignore-missing-imports

install:
	@echo "Installing dependencies..."
	pip install -e .
	pip install pytest requests aiohttp pyppeteer
