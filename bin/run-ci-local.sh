#!/bin/bash

# This script lets you run github ci actions locally
# You need to have act installed.  You can get it at https://nektosact.com/

# by default it simulates a push event
# other useful options
# -j build-and-publish-ubuntu

# also: we only run one matrix variant locally, because otherwise it absolutely hammers the CPU
# LOCAL_PYTHON_VERSION can override the local matrix python-version (default: 3.13).
LOCAL_PYTHON_VERSION="${LOCAL_PYTHON_VERSION:-3.13}"
act -P ubuntu-latest=-self-hosted --matrix "python-version:${LOCAL_PYTHON_VERSION}" "$@"
