#!/usr/bin/env bash
set -euo pipefail

readonly wlan_interface="${SECS_WLAN_INTERFACE_INPUT:-}"
arguments=("$@")
subcommand_index=-1
network_index=-1
network=''
for index in "${!arguments[@]}"; do
    case "${arguments[$index]}" in
        run|build)
            if (( subcommand_index < 0 )); then
                subcommand_index=$index
            fi
            ;;
        --network)
            network_index=$index
            network="${arguments[$((index + 1))]}"
            ;;
        --network=*)
            network_index=$index
            network="${arguments[$index]#--network=}"
            ;;
    esac
done

if (( subcommand_index < 0 )) || [[ "$network" == none ]]; then
    exec docker "${arguments[@]}"
fi
if [[ -z "$wlan_interface" ]] && { [[ "${arguments[$subcommand_index]}" != run ]] || [[ "$network" != bridge && "$network" != default ]]; }; then
    exec docker "${arguments[@]}"
fi
if [[ "$network" != bridge && "$network" != default ]]; then
    printf 'WLAN routing supports Docker bridge/default downloads, not --network %s.\n' "${network:-<unset>}" >&2
    exit 2
fi
if [[ "${arguments[$subcommand_index]}" == run ]]; then
    for argument in "${arguments[@]}"; do
        if [[ $argument == --cidfile || $argument == --cidfile=* ]]; then
            printf '%s\n' 'Acquisition owns its CID receipt; do not supply --cidfile.' >&2
            exit 2
        fi
    done
fi

scratch="$(realpath -e -- "$(mktemp -d)")"
proxy_pid=''
docker_pid=''
proxy_starting=0
docker_starting=0
worker_pending=0
pending_signal=0
request_stop() {
    if [[ "$proxy_starting" == 1 || "$docker_starting" == 1 ]]; then
        [[ "$pending_signal" == 0 ]] && pending_signal=$1
        return 0
    else
        exit "$1"
    fi
}
retire_worker() {
    local cid receipt="$scratch/worker.cid" observed
    [[ -f "$receipt" && ! -L "$receipt" ]] || return 1
    cid=$(<"$receipt")
    [[ "$cid" =~ ^[a-f0-9]{64}$ ]] || return 1
    { cmp -s "$receipt" <(printf '%s' "$cid") || cmp -s "$receipt" <(printf '%s\n' "$cid"); } || return 1
    local -a engine=(docker "${arguments[@]:0:$subcommand_index}")
    observed=$(timeout --signal=TERM --kill-after=2 10 "${engine[@]}" ps --all --no-trunc --quiet --filter "id=$cid") || return 1
    [[ -z "$observed" || "$observed" == "$cid" ]] || return 1
    if [[ -n "$observed" ]]; then
        timeout --signal=TERM --kill-after=2 10 "${engine[@]}" rm --force "$cid" >/dev/null || return 1
    fi
    observed=$(timeout --signal=TERM --kill-after=2 10 "${engine[@]}" ps --all --no-trunc --quiet --filter "id=$cid") || return 1
    [[ -z "$observed" ]]
}
cleanup() {
    local status=$?
    local uncertain=0
    trap - EXIT HUP INT TERM
    if [[ "$proxy_starting" == 1 || "$docker_starting" == 1 ]]; then
        uncertain=1
    fi
    if [[ -n "$docker_pid" ]]; then
        for child in $(jobs -pr); do
            [[ "$child" == "$docker_pid" ]] && kill -KILL "$docker_pid" 2>/dev/null || true
        done
        wait "$docker_pid" 2>/dev/null || true
    fi
    if [[ "$worker_pending" == 1 ]]; then
        if [[ "${arguments[$subcommand_index]}" == run ]]; then
            retire_worker || uncertain=1
        else
            uncertain=1
        fi
    fi
    if [[ "$uncertain" == 0 && -n "$proxy_pid" ]]; then
        for child in $(jobs -pr); do
            [[ "$child" == "$proxy_pid" ]] && kill -KILL "$proxy_pid" 2>/dev/null || true
        done
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if [[ "$uncertain" == 0 ]]; then
        rm -rf -- "$scratch"
    else
        printf 'Docker retirement is unconfirmed; retain %s and inspect the owned worker/proxy before retrying.\n' "$scratch" >&2
        printf '%s\n' "$proxy_pid" > "$scratch/proxy.pid"
        [[ "$status" == 0 ]] && status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'request_stop 129' HUP
trap 'request_stop 130' INT
trap 'request_stop 143' TERM

readonly ready_file="$scratch/proxy.port"
if [[ -n "$wlan_interface" ]]; then
proxy_starting=1
python3 "$(dirname -- "${BASH_SOURCE[0]}")/bound_http_proxy.py" \
    --interface "$wlan_interface" --ready-file "$ready_file" &
proxy_pid=$!
proxy_starting=0
[[ "$pending_signal" == 0 ]] || exit "$pending_signal"
for _ in {1..100}; do
    [[ -s "$ready_file" ]] && break
    kill -0 "$proxy_pid" 2>/dev/null || {
        printf 'WLAN proxy stopped before Docker started.\n' >&2
        exit 2
    }
    sleep 0.05
done
if [[ ! -s "$ready_file" ]]; then
    printf 'WLAN proxy did not become ready before Docker started.\n' >&2
    exit 2
fi
readonly proxy_url="http://127.0.0.1:$(<"$ready_file")"

# The proxy is loopback-only. Host networking lets an already-networked build
# or download container reach it without exposing a proxy listener to the LAN.
if [[ "${arguments[$network_index]}" == --network ]]; then
    arguments[$((network_index + 1))]=host
else
    arguments[$network_index]=--network=host
fi
fi

injected=()
if [[ "${arguments[$subcommand_index]}" == run ]]; then
    injected=(--cidfile "$scratch/worker.cid")
    if [[ -n "$wlan_interface" ]]; then
        injected+=(
        --env "HTTP_PROXY=$proxy_url"
        --env "HTTPS_PROXY=$proxy_url"
        --env "http_proxy=$proxy_url"
        --env "https_proxy=$proxy_url"
        )
    fi
else
    injected=(
        --build-arg "HTTP_PROXY=$proxy_url"
        --build-arg "HTTPS_PROXY=$proxy_url"
        --build-arg "http_proxy=$proxy_url"
        --build-arg "https_proxy=$proxy_url"
    )
fi
arguments=(
    "${arguments[@]:0:$((subcommand_index + 1))}"
    "${injected[@]}"
    "${arguments[@]:$((subcommand_index + 1))}"
)

docker_starting=1
worker_pending=1
docker "${arguments[@]}" <&0 &
docker_pid=$!
docker_starting=0
[[ "$pending_signal" == 0 ]] || exit "$pending_signal"
if wait "$docker_pid"; then
    docker_pid=''
    worker_pending=0
else
    status=$?
    docker_pid=''
    exit "$status"
fi
