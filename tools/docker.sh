#!/usr/bin/env bash
set -euo pipefail

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

if [[ ${SECS_OLD_WLAN_ORIGIN:-undefined} != undefined ]]; then
    printf '%s\n' 'WLAN_INTERFACE was removed; select NETWORK_INTERFACE instead.' >&2
    exit 2
fi
if (( subcommand_index < 0 )); then
    exec docker "${arguments[@]}"
fi
offline=0
if [[ "$network" == none ]]; then
    [[ "${arguments[$subcommand_index]}" == run ]] || exec docker "${arguments[@]}"
    for argument in "${arguments[@]}"; do
        [[ $argument != --cidfile && $argument != --cidfile=* ]] || exec docker "${arguments[@]}"
    done
    offline=1
else
    if [[ "$network" != bridge && "$network" != default ]]; then
        printf 'Acquisition requires --network bridge, default, or none; got %s.\n' "${network:-<unset>}" >&2
        exit 2
    fi
    source "$(dirname -- "${BASH_SOURCE[0]}")/network_interface.sh"
    select_network_interface
fi
wlan_interface=''
if [[ "$offline" == 0 && "$SECS_SELECTED_INTERFACE" != system ]]; then
    wlan_interface=$SECS_SELECTED_INTERFACE
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
proxy_pending=0
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
read_cid() {
    local cid receipt="$scratch/$1.cid"
    [[ -f "$receipt" && ! -L "$receipt" ]] || return 1
    cid=$(<"$receipt")
    [[ "$cid" =~ ^[a-f0-9]{64}$ ]] || return 1
    { cmp -s "$receipt" <(printf '%s' "$cid") || cmp -s "$receipt" <(printf '%s\n' "$cid"); } || return 1
    printf '%s' "$cid"
}
retire_container() {
    local cid observed
    cid=$(read_cid "$1") || return 1
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
    if [[ -n "$proxy_pid" ]]; then
        for child in $(jobs -pr); do
            [[ "$child" == "$proxy_pid" ]] && kill -TERM "$proxy_pid" 2>/dev/null || true
        done
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if [[ "$worker_pending" == 1 ]]; then
        if [[ "${arguments[$subcommand_index]}" == run ]]; then
            retire_container worker || uncertain=1
        else
            uncertain=1
        fi
    fi
    if [[ "$uncertain" == 0 && "$proxy_pending" == 1 ]]; then
        retire_container proxy || uncertain=1
    fi
    if [[ "$uncertain" == 0 ]]; then
        rm -rf -- "$scratch"
    else
        printf 'Docker retirement is unconfirmed; retain %s and inspect the owned worker/proxy before retrying.\n' "$scratch" >&2
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
bootstrap_dir="$(realpath -e -- "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap")"
python3 "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap_snapshot.py"
resolver_mount=()
if [[ -f /run/systemd/resolve/resolv.conf ]]; then
    resolver_mount=(--mount type=bind,src=/run/systemd/resolve/resolv.conf,dst=/run/systemd/resolve/resolv.conf,readonly)
fi
proxy_starting=1
proxy_pending=1
timeout --signal=TERM --kill-after=2 30 docker "${arguments[@]:0:$subcommand_index}" run --detach --pull never \
    --cidfile "$scratch/proxy.cid" --network host --read-only \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
    --pids-limit 64 --cpus 1 --memory 128m --memory-swap 128m \
    --log-driver local --log-opt max-size=1m --log-opt max-file=2 \
    --env PROVIDER_CORE_IN_CONTAINER=1 \
    --mount "type=bind,src=$bootstrap_dir,dst=/bootstrap,readonly" \
    --mount "type=bind,src=$scratch,dst=/receipt" "${resolver_mount[@]}" \
    --entrypoint python "${SECS_BOOTSTRAP_IMAGE:?Pinned bootstrap image is required}" \
    -I -B -c 'import sys; sys.path.insert(0, "/bootstrap"); from provider_core.bound_http_proxy import main; main()' \
    --interface "$wlan_interface" --port 0 --ready-file /receipt/proxy.port >/dev/null &
proxy_pid=$!
proxy_starting=0
[[ "$pending_signal" == 0 ]] || exit "$pending_signal"
if wait "$proxy_pid"; then
    proxy_pid=''
else
    status=$?
    proxy_pid=''
    exit "$status"
fi
proxy_cid=$(read_cid proxy) || { printf '%s\n' 'Cannot verify the proxy container identity.' >&2; exit 2; }
for _ in {1..100}; do
    [[ -s "$ready_file" ]] && break
    sleep 0.05
done
if [[ ! -s "$ready_file" ]]; then
    printf 'WLAN proxy did not become ready before Docker started.\n' >&2
    timeout --signal=TERM --kill-after=2 10 docker "${arguments[@]:0:$subcommand_index}" logs --tail 30 "$proxy_cid" 2>&1 | head -c 8192 >&2 || true
    exit 2
fi
proxy_running=$(timeout --signal=TERM --kill-after=2 10 docker "${arguments[@]:0:$subcommand_index}" inspect --format '{{.State.Running}}' "$proxy_cid") || {
    printf '%s\n' 'Cannot inspect the proxy container.' >&2; exit 2;
}
[[ "$proxy_running" == true ]] || { printf '%s\n' 'Proxy container stopped before acquisition.' >&2; exit 2; }
proxy_port=$(<"$ready_file")
[[ "$proxy_port" =~ ^[0-9]{1,5}$ ]] && (( 10#$proxy_port > 0 && 10#$proxy_port <= 65535 )) || {
    printf '%s\n' 'Proxy published an invalid port.' >&2; exit 2;
}
readonly proxy_url="http://127.0.0.1:$proxy_port"

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
        --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= --env FTP_PROXY= --env ftp_proxy=
        )
    elif [[ "$offline" == 0 ]]; then
        injected+=(--env HTTP_PROXY= --env HTTPS_PROXY= --env http_proxy= --env https_proxy=
                   --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= --env FTP_PROXY= --env ftp_proxy=)
    fi
else
    if [[ -n "$wlan_interface" ]]; then
        injected=(
        --build-arg "HTTP_PROXY=$proxy_url"
        --build-arg "HTTPS_PROXY=$proxy_url"
        --build-arg "http_proxy=$proxy_url"
        --build-arg "https_proxy=$proxy_url"
        --build-arg NO_PROXY= --build-arg no_proxy= --build-arg ALL_PROXY= --build-arg all_proxy= --build-arg FTP_PROXY= --build-arg ftp_proxy=
        )
    else
        injected=(--build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg http_proxy= --build-arg https_proxy=
                  --build-arg NO_PROXY= --build-arg no_proxy= --build-arg ALL_PROXY= --build-arg all_proxy= --build-arg FTP_PROXY= --build-arg ftp_proxy=)
    fi
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
