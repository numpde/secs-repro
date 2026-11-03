#!/usr/bin/env bash
# Call the shared acquisition owner with explicit image, grants and arguments.

secs_acquire() {
    local _secs_image=$1 _secs_grants=$2 _secs_application=$3
    local _secs_root _secs_endpoint
    _secs_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || return 2
    python3 -B "$_secs_root/tools/acquisition_bootstrap.py" || return $?
    source "$_secs_root/tools/network_interface.sh"
    select_network_interface || return $?
    _secs_endpoint=$(python3 -B "$_secs_root/tools/docker_context.py" --inherited-endpoint) || return $?
    source "$_secs_root/tools/bootstrap/acquisition/acquisition_tool/owner.sh"
    _secs_acquisition_worker() {
        local _secs_worker_status
        acquisition_set_route "$SECS_SELECTED_INTERFACE" || return $?
        acquisition_worker online _secs_worker_status "$_secs_image" "$_secs_grants" "$_secs_application" || return $?
        return "$_secs_worker_status"
    }
    local -a _secs_scope=(--endpoint "$_secs_endpoint" --bootstrap-image "${SECS_BOOTSTRAP_IMAGE:?Pinned bootstrap image is required}")
    if [[ -n ${SECS_RETIREMENT_RECEIPT:-} ]]; then
        _secs_scope+=(--retired-receipt "$SECS_RETIREMENT_RECEIPT")
    fi
    acquisition_scope "${_secs_scope[@]}" -- _secs_acquisition_worker
}
