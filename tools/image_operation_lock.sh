#!/usr/bin/env bash
# Builds and deployment handoffs share the checkout lock with image retirement.
hold_image_operation_lock() {
    exec {secs_image_lock_fd}< "$1"
    if ! flock --shared --wait 60 "$secs_image_lock_fd"; then
        printf '%s\n' 'Cannot begin image work: the checkout image lock is busy. Retry after retirement completes.' >&2
        return 1
    fi
}
