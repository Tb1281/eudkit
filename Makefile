# eudkit developer entry points. Keep these in sync with CONTRIBUTING.md.
CARGO ?= cargo
PYTHON ?= python3
NPM ?= npm
JOBS ?= 2

.PHONY: build release test-rust test-ts test-py test-quickstart test-examples typecheck test clean

build:
	$(CARGO) build --workspace -j $(JOBS)

release:
	$(CARGO) build --release --workspace -j $(JOBS)

test-rust:
	$(CARGO) test --workspace -j $(JOBS)
	$(CARGO) test -p eudkit-lower --test map_bridge -j $(JOBS)

typecheck:
	cd ts && $(NPM) install && $(NPM) run typecheck

test-ts: build typecheck
	cd ts && $(NPM) test

test-py: build
	PYTHONPATH=. $(PYTHON) -m unittest discover -s py/tests -p 'test_*.py' -t .

test-quickstart: build
	PYTHONPATH=. $(PYTHON) -m unittest py/tests/test_quickstart_examples.py

test-examples: build
	PYTHONPATH=. $(PYTHON) -m unittest py/tests/test_examples.py

test:
	$(MAKE) test-rust
	$(MAKE) test-ts
	$(MAKE) test-py

clean:
	$(CARGO) clean
	cd ts && $(NPM) run clean
