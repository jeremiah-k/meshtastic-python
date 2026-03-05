#!/bin/bash

set -e
if [[ -d dist ]]; then
	rm -f dist/*
fi

poetry build
poetry run pytest
poetry publish
#python3 setup.py sdist bdist_wheel
#python3 -m twine upload dist/*
