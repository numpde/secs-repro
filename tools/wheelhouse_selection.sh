#!/usr/bin/env bash
# Explicit shared pool selection follows nmrnet-repro's wheelhouse_path.sh.
select_wheelhouse() {
    local tty_fd path repository
    if [[ ${SECS_WHEELHOUSE_ORIGIN-} == 'command line' ]]; then
        path=${SECS_WHEELHOUSE_INPUT-}
    else
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'Set WHEELHOUSE=<directory> on the make command line; no interactive terminal is available.' >&2
            return 2
        fi
        printf 'WHEELHOUSE (absolute or relative path): ' >&"$tty_fd"
        if ! IFS= read -r path <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'WHEELHOUSE input ended before a path was read.' >&2
            return 2
        fi
        exec {tty_fd}>&-
    fi
    [[ -n $path ]] || { printf '%s\n' 'WHEELHOUSE cannot be empty.' >&2; return 2; }
    if ! IFS= read -r -d '' wheelhouse_directory < <(realpath -ze -- "$path"); then
        printf '%s\n' 'WHEELHOUSE must be an existing directory.' >&2
        return 2
    fi
    if [[ ! -d $wheelhouse_directory || $wheelhouse_directory == / || $wheelhouse_directory == *','* ||
          $wheelhouse_directory == *$'\n'* || $wheelhouse_directory == *$'\r'* ]]; then
        printf '%s\n' 'WHEELHOUSE must be a directory below root without commas or line breaks.' >&2
        return 2
    fi
    repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    if [[ $wheelhouse_directory == "$repository" || $wheelhouse_directory == "$repository/"* ||
          $repository == "$wheelhouse_directory/"* ]]; then
        printf '%s\n' 'Select a shared cache outside the repository and its ancestors.' >&2
        return 2
    fi
}

stage_selected_wheels() {
    local status
    secs_engine_active=1
    if bash tools/stage_shared_wheels.sh "$@"; then
        secs_engine_active=0
        return 0
    else
        status=$?
    fi
    # These results are emitted only after the Docker owner confirms retirement.
    if [[ $status == 10 || $status == 11 ]]; then secs_engine_active=0; fi
    return "$status"
}
