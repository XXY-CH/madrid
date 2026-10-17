.PHONY: test doctor clean
test:
	python3 tests/test_smoke.py
doctor:
	python3 -m madrid doctor
clean:
	find madrid tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist *.egg-info
