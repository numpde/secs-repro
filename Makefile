SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
CHECKPOINT_IMAGE := secs-repro/checkpoint-extractor:local
CHECKPOINT_DIRECTORY := checkpoints/residual_augment_resolution_physics_finetune_20250708_1747
CHECKPOINT_PATH := $(CHECKPOINT_DIRECTORY)/best_model.ckpt
ARCHIVE_MEMBER_SUFFIX := residual_augment_resolution_physics_finetune_20250708_1747/best_model.ckpt
ARCHIVE_URL ?= https://zenodo.org/records/14638782/files/zenodo_secs_v3.tar.gz?download=1
ARCHIVE_MD5 := 5ca6bed3fb7e70630020f55796fd26ab
MAX_CHECKPOINT_BYTES := 4294967296
DOCKER := env -u DOCKER_HOST -u DOCKER_CONTEXT docker --context default

.PHONY: help checkpoint checkpoint/image

help:
	@printf '%s\n' \
		'SECS checkpoint preparation' \
		'' \
		'  make checkpoint' \
		'      Stream the pinned Zenodo archive and extract the SECS checkpoint.' \
		'  make checkpoint ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz' \
		'      Read the same archive from a local, read-only path without network access.' \
		'' \
		'SECS package images' \
		'' \
		'  make packages/base-images/pull packages/locks/write' \
		'      Pull pinned bases and write CPU/GPU hash locks.' \
		'  make packages/cpu/wheelhouse packages/gpu/wheelhouse' \
		'      Download locked artifacts in bounded containers.' \
		'  make packages/images' \
		'      Build both images without network access from existing wheelhouses.' \
		'' \
		'Only $(CHECKPOINT_PATH) is published; the archive is not retained.'

checkpoint/image:
	@$(DOCKER) build --network none --pull=false \
		--file containers/checkpoint/Dockerfile \
		--tag "$(CHECKPOINT_IMAGE)" \
		"$(REPOSITORY_ROOT)"

checkpoint: private export ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE),)
checkpoint: private export ARCHIVE_URL_INPUT := $(value ARCHIVE_URL)
checkpoint:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot prepare the checkpoint as host UID 0.' >&2
		exit 2
	fi
	if test -e "$(CHECKPOINT_PATH)" || test -L "$(CHECKPOINT_PATH)"; then
		printf '%s\n' 'Cannot prepare the checkpoint: $(CHECKPOINT_PATH) already exists.' >&2
		exit 2
	fi
	mkdir -p -- "$(CHECKPOINT_DIRECTORY)"
	output_dir=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	$(MAKE) --no-print-directory checkpoint/image
	run_args=(
		--rm --init --pull never --read-only
		--user "$(HOST_UID):$(HOST_GID)"
		--cap-drop ALL --security-opt no-new-privileges:true
		--pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m
		--tmpfs /tmp:size=16m,mode=1777,noexec,nosuid,nodev
		--mount "type=bind,src=$$output_dir,dst=/output"
	)
	source_args=()
	# A local archive needs no egress. URL mode grants egress but keeps the
	# checkpoint directory as the container's only writable host mount.
	if test -n "$${ARCHIVE_INPUT:-}"; then
		if test ! -f "$${ARCHIVE_INPUT}"; then
			printf '%s\n' "ARCHIVE must name a regular file: $${ARCHIVE_INPUT}" >&2
			exit 2
		fi
		archive_path=$$(realpath -e -- "$${ARCHIVE_INPUT}")
		run_args+=(--network none --mount "type=bind,src=$$archive_path,dst=/input/archive.tar.gz,readonly")
		source_args=(--archive /input/archive.tar.gz)
	else
		run_args+=(--network bridge)
		source_args=(--url "$${ARCHIVE_URL_INPUT}")
	fi
	$(DOCKER) run "$${run_args[@]}" "$(CHECKPOINT_IMAGE)" \
		"$${source_args[@]}" \
		--expected-archive-md5 "$(ARCHIVE_MD5)" \
		--member-suffix "$(ARCHIVE_MEMBER_SUFFIX)" \
		--max-member-bytes "$(MAX_CHECKPOINT_BYTES)" \
		--output /output/best_model.ckpt

include packages.mk
