.PHONY: test test-python test-plugin typecheck

test: test-python test-plugin typecheck

test-python:
	PYTHONPATH=src python3 -m pytest -q

test-plugin:
	cd plugin && bun test

typecheck:
	cd plugin && node_modules/.bin/tsc -p tsconfig.json --noEmit
