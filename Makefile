# InstaBaileys — the everyday commands.
#
#   make install   install the Python package
#   make rust      build the Rust engine into bin/
#   make test      run the test suite
#   make run       start the bot

PY ?= python3

.PHONY: install rust test run clean

install:
	$(PY) -m pip install -e .

rust:
	cd rust/igfast && cargo build --release
	mkdir -p bin && cp rust/igfast/target/release/igfast bin/igfast
	@echo "the engine is in bin/igfast"

test:
	$(PY) -m pytest -q || $(PY) tools/run_tests.py

run:
	$(PY) -m ibot watch

clean:
	rm -rf bin rust/igfast/target
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
