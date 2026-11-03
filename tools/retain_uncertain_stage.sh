#!/usr/bin/env bash
# A failed client does not establish that its Docker workload has retired.
unset secs_receipt_directory SECS_RETIREMENT_RECEIPT

begin_retained_engine() {
    if [[ -n ${secs_receipt_directory:-} ]]; then rm -rf -- "$secs_receipt_directory" || return 1; fi
    local receipt_root
    receipt_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || return 1
    secs_receipt_directory=$(mktemp -d "$receipt_root/.secs-retirement.XXXXXXXX") || return 1
    SECS_RETIREMENT_RECEIPT="$secs_receipt_directory/retired"
    export SECS_RETIREMENT_RECEIPT
    secs_engine_active=1
}

retain_build_receipt() {
    if [[ -e "$1/image-id" || -L "$1/image-id" ]]; then
        if ! find "$1" -mindepth 1 -maxdepth 1 ! -name image-id -exec rm -rf -- {} +; then
            printf 'Could not remove completed build inputs from %s\n' "$1" >&2
        fi
    else
        secs_engine_active=0
    fi
}

retain_uncertain_stage() {
    local status=$?
    if [[ ${secs_engine_active:-0} == 1 && -n ${secs_receipt_directory:-} && ${SECS_RETIREMENT_RECEIPT:-} == "$secs_receipt_directory/retired" ]] &&
        [[ -f $SECS_RETIREMENT_RECEIPT && ! -L $SECS_RETIREMENT_RECEIPT ]] &&
        cmp -s "$SECS_RETIREMENT_RECEIPT" <(printf 'retired\n'); then
        secs_engine_active=0
    fi
    if [[ -n ${secs_receipt_directory:-} ]] && ! rm -rf -- "$secs_receipt_directory"; then
        printf 'Could not remove retirement receipt directory: %s\n' "$secs_receipt_directory" >&2
        [[ $status == 0 ]] && status=1
    fi
    if [[ ${secs_engine_active:-0} == 1 ]]; then
        printf 'Docker operation did not complete successfully; retirement may be unconfirmed. Preserve staging input: %s\n' "$*" >&2
    else
        if ! rm -rf -- "$@"; then
            printf 'Could not remove staging input: %s\n' "$*" >&2
            [[ $status == 0 ]] && status=1
        fi
    fi
    exit "$status"
}
