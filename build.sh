#!/bin/bash

set -e

cleanup_artifacts() {
    rm -rf dist build *.egg-info
    echo "Removed dist/, build/, and *.egg-info artifacts."
}

trap cleanup_artifacts EXIT

python -m pip install --quiet build twine

python -m build

python -m twine check dist/*

if [[ "$1" == "test" ]]; then
    export TWINE_USERNAME="__token__"
    export TWINE_PASSWORD="${TEST_PYPI_API_TOKEN:?Set TEST_PYPI_API_TOKEN first}"
    python -m twine upload --repository testpypi dist/*

elif [[ "$1" == "release" ]]; then
    export TWINE_USERNAME="__token__"
    export TWINE_PASSWORD="${PYPI_API_TOKEN:?Set PYPI_API_TOKEN first}"
    python -m twine upload dist/*

else
    echo "Usage: $0 {test|release}"
    exit 1
fi
