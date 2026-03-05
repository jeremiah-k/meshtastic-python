#!/usr/bin/env bash

set -euo pipefail

if [[ -d dist ]]; then
	rm -f dist/*
fi

bin/regen-docs.sh
pandoc --from=markdown --to=rst --output=README README.md

poetry publish -r test-pypi --build
echo "view the upload at https://test.pypi.org/ it it looks good upload for real"
