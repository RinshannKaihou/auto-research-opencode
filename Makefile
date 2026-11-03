.PHONY: test test-python test-plugin typecheck verify-specialist-lifecycle

test: test-python test-plugin typecheck

# Specialist child-lifecycle model. Not part of `make test`: CI does not install TLC.
verify-specialist-lifecycle:
	python3 scripts/verify_specialist_lifecycle.py

test-python:
	PYTHONPATH=src python3 -m pytest -q

test-plugin:
	cd plugin && bun test

typecheck:
	cd plugin && node_modules/.bin/tsc -p tsconfig.json --noEmit
