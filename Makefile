# engram verification gate.
#
#   make verify   — the full gate: lint -> phi-scan -> test (fail-fast, stops at first failure)
#   make lint     — typed store linter (scripts/lint_store.py)
#   make phi-scan — store PHI audit summary (scripts/phi_scan_store.py --summary; WARN-only on
#                   flags, non-zero only on an internal scan error — fail-closed)
#   make test     — the pytest regression suite (scripts/tests/)
#
# `verify` lists its prerequisites in order, so make runs them left-to-right and aborts on the
# first non-zero exit. Pin the interpreter with `make verify PY=python3.12` if needed.
PY ?= python3

.PHONY: verify lint phi-scan test

verify: lint phi-scan test

lint:
	$(PY) scripts/lint_store.py

phi-scan:
	$(PY) scripts/phi_scan_store.py --summary

test:
	$(PY) -m pytest scripts/tests/ -q
