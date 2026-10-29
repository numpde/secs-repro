#!/usr/bin/env bash
# Select a host network for acquisition without an implicit default.

select_network_interface() {
    local path name index answer tty_fd
    local -a choices=(system)
    if [[ ${SECS_NETWORK_INTERFACE_ORIGIN:-undefined} == 'command line' || ${SECS_NETWORK_INTERFACE_ORIGIN:-undefined} == environment ]]; then
        SECS_SELECTED_INTERFACE=${SECS_NETWORK_INTERFACE_INPUT-}
    else
        if [[ ${SECS_NETWORK_INTERFACE_ORIGIN:-undefined} != undefined ]]; then
            printf '%s\n' 'Supply NETWORK_INTERFACE in the environment or on the make command line, or choose interactively.' >&2
            return 2
        fi
        for path in /sys/class/net/*; do
            name=${path##*/}
            [[ $name =~ ^[A-Za-z0-9_.:-]{1,15}$ && $name != . && $name != .. && -d $path ]] && choices+=("$name")
        done
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'Choose NETWORK_INTERFACE=<interface|system> on the make command line; no terminal is available.' >&2
            return 2
        fi
        printf 'Select NETWORK_INTERFACE (no default; system uses normal routing):\n' >&"$tty_fd"
        for index in "${!choices[@]}"; do printf '  %d) %s\n' "$((index + 1))" "${choices[index]}" >&"$tty_fd"; done
        printf 'Choice [1-%d]: ' "${#choices[@]}" >&"$tty_fd"
        if ! IFS= read -r answer <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'Network interface selection ended without a choice.' >&2
            return 2
        fi
        exec {tty_fd}>&-
        SECS_SELECTED_INTERFACE=
        for index in "${!choices[@]}"; do
            if [[ $answer == "$((index + 1))" ]]; then SECS_SELECTED_INTERFACE=${choices[index]}; break; fi
        done
    fi
    if [[ $SECS_SELECTED_INTERFACE != system ]] && [[ ! $SECS_SELECTED_INTERFACE =~ ^[A-Za-z0-9_.:-]{1,15}$ || $SECS_SELECTED_INTERFACE == . || $SECS_SELECTED_INTERFACE == .. || ! -d /sys/class/net/$SECS_SELECTED_INTERFACE ]]; then
        printf 'NETWORK_INTERFACE=%q is not an available network interface.\n' "$SECS_SELECTED_INTERFACE" >&2
        return 2
    fi
}
