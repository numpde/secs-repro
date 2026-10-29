#!/usr/bin/env bash
# A failed client does not establish that its Docker workload has retired.
retain_uncertain_stage() {
    local status=$?
    if [[ ${secs_engine_active:-0} == 1 ]]; then
        printf 'Docker operation did not complete successfully; retirement may be unconfirmed. Preserve staging input: %s\n' "$*" >&2
    else
        if ! rm -rf -- "$@"; then
            printf 'Could not remove staging input: %s\n' "$*" >&2
            [[ $status == 0 ]] && status=1
        fi
    fi
    return "$status"
}
