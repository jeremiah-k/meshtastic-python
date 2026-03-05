.PHONY: all clean test ci ci-strict ci-base lint docs cov open-coverage virt virt-meshtasticd virt-smokevirt-meshtasticd virt-multinode-meshtasticd smoke1 smoke1-destructive slow install examples protobufs protobufs-update FORCE

POETRY_RUN := poetry run

all: test

clean:
	rm -rf htmlcov .coverage coverage.xml .mypy_cache .pytest_cache dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# only run the fast unit tests
test:
	$(POETRY_RUN) pytest -m unit

# run baseline CI checks locally
# Runs the first CI stages in order:
# pytest (with coverage) -> pylint
ci-base:
	$(POETRY_RUN) pytest --cov=meshtastic --cov-report=xml
	$(MAKE) lint

ci:
	$(MAKE) ci-base
	$(POETRY_RUN) mypy meshtastic/

# run CI checks with strict mypy (for maintainers)
ci-strict:
	$(MAKE) ci-base
	$(POETRY_RUN) mypy meshtastic/ --strict

# only run the smoke tests against the virtual device
virt:
	$(POETRY_RUN) pytest -m smokevirt

# run meshtasticd simulator integration tests (defaults to meshtastic/tests/test_meshtasticd_ci.py unless MESHTASTICD_PYTEST_TARGETS is set)
virt-meshtasticd:
	./bin/run-smokevirt-with-meshtasticd.sh

# run the full legacy smokevirt suite against meshtasticd simulator container
virt-smokevirt-meshtasticd:
	MESHTASTICD_PYTEST_TARGETS="meshtastic/tests/test_smokevirt.py" \
	MESHTASTICD_PYTEST_MARK_EXPR="smokevirt and not smoke1_destructive" \
	./bin/run-smokevirt-with-meshtasticd.sh

# run dual-daemon meshtasticd integration tests against host-network simulators
virt-multinode-meshtasticd:
	./bin/run-multinode-with-meshtasticd.sh

# run stable non-destructive smoke1 hardware checks
smoke1:
	$(POETRY_RUN) pytest -m "smoke1 and not smoke1_destructive" -s -vv

# run destructive smoke1 hardware checks (reboot/reset/config mutation)
smoke1-destructive:
	$(POETRY_RUN) pytest -m smoke1_destructive -s -vv

# local install
install:
	poetry install

# generate the docs (for local use)
docs:
	$(POETRY_RUN) pdoc3 --html -f --output-dir docs meshtastic

# lint the codebase (same command as CI)
lint:
	PYLINTHOME=$${TMPDIR:-/tmp}/pylint-cache $(POETRY_RUN) pylint meshtastic examples/ --ignore-patterns ".*_pb2\\.pyi?$$"

# show the slowest unit tests
slow:
	$(POETRY_RUN) pytest -m unit --durations=5

protobufs: FORCE
	git submodule update --init --recursive
	./bin/regen-protobufs.sh

protobufs-update: FORCE
	git submodule update --init --recursive
	git submodule update --remote --merge
	./bin/regen-protobufs.sh

# run the coverage report and open results in a browser
open-coverage:
	@# Open report when possible; otherwise print location.
	@if command -v open >/dev/null 2>&1; then \
		open htmlcov/index.html >/dev/null 2>&1 || echo "Coverage report generated at htmlcov/index.html"; \
	elif command -v xdg-open >/dev/null 2>&1; then \
		xdg-open htmlcov/index.html >/dev/null 2>&1 || echo "Coverage report generated at htmlcov/index.html"; \
	else \
		echo "Coverage report generated at htmlcov/index.html"; \
	fi

cov:
	$(POETRY_RUN) pytest --cov-report html --cov=meshtastic
	@$(MAKE) open-coverage

# run cli examples
examples: FORCE
	$(POETRY_RUN) pytest -m examples

# Makefile hack to get the examples to always run
FORCE: ;
