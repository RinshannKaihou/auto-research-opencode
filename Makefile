.PHONY: test test-python test-plugin typecheck verify-specialist-lifecycle

test: test-python test-plugin typecheck

# Specialist child-lifecycle model. Not part of `make test`: CI does not install TLC.
# Script exits 0 pass, 1 counterexample, 2 model, 3 drift, 4 setup.
# GNU make prints that status as "Error N" and then exits 2 itself.
verify-specialist-lifecycle:
	python3 scripts/verify_specialist_lifecycle.py

test-python:
	PYTHONPATH=src python3 -m pytest -q

test-plugin:
	cd plugin && bun test

typecheck:
	cd plugin && node_modules/.bin/tsc -p tsconfig.json --noEmit
