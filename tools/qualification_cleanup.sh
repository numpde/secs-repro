#!/usr/bin/env bash
# Retire only containers identified by this qualification's private CID files.

retire_qualification_containers() {
    local directory=$1 marker phase cid_file cid inspection failed=0
    for marker in "$directory"/*.started; do
        [[ -e $marker || -L $marker ]] || continue
        phase=${marker##*/}; phase=${phase%.started}
        cid_file="$directory/$phase.cid"
        if [[ ! -f $marker || -L $marker || ! -f $cid_file || -L $cid_file ]]; then
            printf 'Qualification container identity is unconfirmed for %s; retaining %s.\n' "$phase" "$directory" >&2
            failed=1
            continue
        fi
        cid=$(<"$cid_file")
        if [[ ! $cid =~ ^[a-f0-9]{64}$ ]] || { ! cmp -s "$cid_file" <(printf '%s\n' "$cid") && ! cmp -s "$cid_file" <(printf '%s' "$cid"); }; then
            printf 'Qualification container identity is invalid for %s; retaining %s.\n' "$phase" "$directory" >&2
            failed=1
            continue
        fi
        if inspection=$(qualification_docker container inspect "$cid" 2>&1); then
            if ! qualification_docker rm -f "$cid" >/dev/null; then
                printf 'Qualification container removal is unconfirmed for %s.\n' "$phase" >&2
                failed=1
            fi
        else
            case "$inspection" in
                *'No such object'*|*'No such container'*) ;;
                *) printf 'Qualification container inspection is unconfirmed for %s.\n' "$phase" >&2; failed=1 ;;
            esac
        fi
    done
    [[ $failed == 0 ]]
}
