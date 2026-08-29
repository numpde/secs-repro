#!/usr/bin/env bash
set -euo pipefail

readonly wlan_interface="${SECS_WLAN_INTERFACE_INPUT:-}"
if [[ -z "$wlan_interface" ]]; then
    exec docker "$@"
fi
if [[ ! -d "/sys/class/net/$wlan_interface" ]]; then
    printf 'WLAN interface does not exist: %s\n' "$wlan_interface" >&2
    exit 2
fi

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
if [[ "$network" != bridge && "$network" != default ]]; then
    printf 'WLAN routing supports Docker bridge/default downloads, not --network %s.\n' "${network:-<unset>}" >&2
    exit 2
fi

scratch="$(mktemp -d)"
proxy_pid=''
docker_pid=''
cleanup() {
    local status=$?
    trap - EXIT HUP INT TERM
    if [[ -n "$docker_pid" ]]; then
        kill "$docker_pid" 2>/dev/null || true
        wait "$docker_pid" 2>/dev/null || true
    fi
    if [[ -n "$proxy_pid" ]]; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    rm -rf -- "$scratch"
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

readonly ready_file="$scratch/proxy.port"
python3 "$(dirname -- "${BASH_SOURCE[0]}")/bound_http_proxy.py" \
    --interface "$wlan_interface" --ready-file "$ready_file" &
proxy_pid=$!
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

injected=()
if [[ "${arguments[$subcommand_index]}" == run ]]; then
    injected=(
        --env "HTTP_PROXY=$proxy_url"
        --env "HTTPS_PROXY=$proxy_url"
        --env "http_proxy=$proxy_url"
        --env "https_proxy=$proxy_url"
    )
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

docker "${arguments[@]}" &
docker_pid=$!
wait "$docker_pid"
docker_pid=''
