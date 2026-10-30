#!/usr/bin/env bash
# Capture only lock-selected artifacts from a read-only shared wheel pool.
set -euo pipefail
[[ $# == 4 || $# == 5 ]] || { printf '%s\n' 'Usage: stage_shared_wheels.sh POOL LOCK STAGE IMAGE [check]' >&2; exit 2; }
pool=$1 lock=$2 stage=$3 image=$4 mode=${5:-capture}
[[ $mode == capture || $mode == check ]] || exit 2
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
mkdir "$stage/.tmp"
# The container records pip's status, then exits successfully. The Docker
# owner must confirm its retirement before a missing pool can lead to download.
if env -u DOCKER_HOST -u DOCKER_CONTEXT "$root/tools/docker.sh" --context default run --rm \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" --pull never --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
    --env PIP_NO_CACHE_DIR=1 --env TMPDIR=/stage/.tmp \
    --mount "type=bind,src=$lock,dst=/input/requirements.lock,readonly" \
    --mount "type=bind,src=$pool,dst=/pool,readonly" \
    --mount "type=bind,src=$stage,dst=/stage" \
    --entrypoint /bin/sh "$image" -c '
        if [ "$1" = check ]; then
            python -m pip install --dry-run --ignore-installed --no-index --require-hashes --only-binary=:all: \
                --no-binary=antlr4-python3-runtime --no-deps --find-links /pool --report /stage/.pip-report \
                -r /input/requirements.lock > /stage/.pip-output 2> /stage/.pip-error
        else
            python -m pip download --no-index --require-hashes --only-binary=:all: \
                --no-binary=antlr4-python3-runtime --no-deps --find-links /pool \
                --dest /stage -r /input/requirements.lock > /stage/.pip-output 2> /stage/.pip-error
        fi
        result=$?
        printf "%s\n" "$result" > /stage/.pip-status
        exit 0
    ' wheel-selection "$mode"; then :; else
    printf '%s\n' 'Cannot confirm offline wheel selection and Docker retirement.' >&2
    exit 1
fi
status=$(cat "$stage/.pip-status")
rm -rf "$stage/.tmp"
if [[ $status != 0 ]]; then
    cat "$stage/.pip-error" >&2
    if grep -Fq 'No matching distribution found for ' "$stage/.pip-error"; then
        exit 10
    fi
    exit 11
fi
rm -f "$stage/.pip-output" "$stage/.pip-error" "$stage/.pip-status" "$stage/.pip-report"
if [[ $mode == check ]]; then exit 0; fi
( cd "$stage" && sha256sum * | LC_ALL=C sort ) > "$stage/.complete"
