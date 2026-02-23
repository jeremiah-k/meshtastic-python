#!/bin/bash

# This script lets you run github ci actions locally
# You need to have act installed.  You can get it at https://nektosact.com/

# by default it simulates a push event
# other useful options
# -j build-and-publish-ubuntu

# also: we only run one matrix variant locally, because otherwise it absolutely hammers the CPU
act -P ubuntu-latest=-self-hosted --matrix "python-version:3.13" "$@"
