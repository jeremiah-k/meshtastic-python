.PHONY: all clean test ci lint docs cov virt smoke1 slow install examples protobufs FORCE

all: test

clean:
	rm -rf htmlcov .coverage coverage.xml

# only run the fast unit tests
test:
	poetry run pytest -m unit

# run all CI checks locally (same as CI pipeline)
# Runs the same checks in the same order as .github/workflows/ci.yml:
# pytest (with coverage) -> pylint -> mypy
ci:
	poetry run pytest --cov=meshtastic --cov-report=xml
	$(MAKE) lint
	poetry run mypy meshtastic/

# only run the smoke tests against the virtual device
virt:
	poetry run pytest -m smokevirt

# run the smoke1 test (after doing a factory reset and unplugging/replugging in device)
smoke1:
	poetry run pytest -m smoke1 -s -vv

# local install
install:
	pip install .

# generate the docs (for local use)
docs:
	poetry run pdoc3 --html -f --output-dir docs meshtastic

# lint the codebase (same command as CI)
lint:
	poetry run pylint meshtastic examples/ --ignore-patterns ".*_pb2.pyi?$$"

# show the slowest unit tests
slow:
	poetry run pytest -m unit --durations=5

protobufs: FORCE
	git submodule update --init --recursive

	git submodule update --remote --merge
	./bin/regen-protobufs.sh

# run the coverage report and open results in a browser
cov:
	poetry run pytest --cov-report html --cov=meshtastic
	# on mac, this will open the coverage report in a browser
	open htmlcov/index.html

# run cli examples
examples: FORCE
	poetry run pytest -m examples

# Makefile hack to get the examples to always run
FORCE: ;
