SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
CHECKPOINT_IMAGE := secs-repro/checkpoint-extractor:local
CHECKPOINT_CONVERTER_IMAGE := secs-repro/packages-cpu:local
ARCHIVE_APPLICATION_ROOT := MoleculeBindZenodo/app-cpu
ARCHIVE_MEMBER := $(ARCHIVE_APPLICATION_ROOT)/checkpoints/residual_augment_resolution_physics_finetune_20250708_1747/best_model.ckpt
CHECKPOINT_RELATIVE_PATH := $(patsubst $(ARCHIVE_APPLICATION_ROOT)/%,%,$(ARCHIVE_MEMBER))
CHECKPOINT_DIRECTORY := $(patsubst %/,%,$(dir $(CHECKPOINT_RELATIVE_PATH)))
CHECKPOINT_WEIGHTS := $(CHECKPOINT_DIRECTORY)/secs-v3.safetensors
CHECKPOINT_MODEL := $(CHECKPOINT_DIRECTORY)/model.yaml
CHECKPOINT_MANIFEST := $(CHECKPOINT_DIRECTORY)/manifest.json
CHECKPOINT_PRECISION ?= float32
ZENODO_RECORD := https://zenodo.org/records/14638782
ARCHIVE_URL ?= $(ZENODO_RECORD)/files/zenodo_secs_v3.tar.gz?download=1
ARCHIVE_MD5 := 5ca6bed3fb7e70630020f55796fd26ab
MAX_CHECKPOINT_BYTES := 4294967296
SECS_REPOSITORY := $(shell git config -f .gitmodules --get submodule.secs.url)
SECS_REVISION := $(shell git -C secs rev-parse HEAD)
DOCKER := env -u DOCKER_HOST -u DOCKER_CONTEXT docker --context default

.PHONY: help checkpoint checkpoint/image

help:
	@printf '%s\n' \
		'SECS checkpoint preparation' \
		'' \
		'  make checkpoint' \
		'      Stream the pinned Zenodo archive and publish inference-only artifacts.' \
		'  make checkpoint ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz' \
		'      Read the same archive from a local, read-only path without network access.' \
		'  make checkpoint CHECKPOINT_PRECISION=float16' \
		'      Store floating-point weights as float32, float16, or bfloat16.' \
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
		'The archive and Lightning checkpoint are not retained.'

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
	case "$(CHECKPOINT_PRECISION)" in
		float32|float16|bfloat16) ;;
		*) printf '%s\n' 'CHECKPOINT_PRECISION must be float32, float16, or bfloat16.' >&2; exit 2 ;;
	esac
	if test -n "$${ARCHIVE_INPUT:-}"; then
		if test ! -f "$${ARCHIVE_INPUT}"; then
			printf '%s\n' "ARCHIVE must name a regular file: $${ARCHIVE_INPUT}" >&2
			exit 2
		fi
		archive_path=$$(realpath -e -- "$${ARCHIVE_INPUT}")
	fi
	mkdir -p -- "$(CHECKPOINT_DIRECTORY)"
	output_dir=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	for artifact in "$(CHECKPOINT_WEIGHTS)" "$(CHECKPOINT_MODEL)" "$(CHECKPOINT_MANIFEST)"; do
		if test -e "$$artifact" || test -L "$$artifact"; then
			printf '%s\n' "Cannot prepare checkpoint artifacts: $$artifact already exists." >&2
			exit 2
		fi
	done
	$(MAKE) --no-print-directory packages/cpu/image
	$(MAKE) --no-print-directory checkpoint/image
	extractor_args=(--rm --init --pull never --read-only
		"--cap-drop" ALL --security-opt no-new-privileges:true
		"--pids-limit" 32 --cpus 1 --memory 2304m --memory-swap 2304m
		"--tmpfs" /scratch:size=2g,mode=0700,uid=65532,gid=65532,noexec,nosuid,nodev
	)
	source_args=()
	if test -n "$${ARCHIVE_INPUT:-}"; then
		extractor_args+=(--network none --mount "type=bind,src=$$archive_path,dst=/input/archive.tar.gz,readonly")
		source_args=(--archive /input/archive.tar.gz)
	else
		extractor_args+=(--network bridge)
		source_args=(--url "$${ARCHIVE_URL_INPUT}")
	fi
	converter_args=(--rm --init --pull never --network none --read-only --user "$(HOST_UID):$(HOST_GID)"
		"--cap-drop" ALL --security-opt no-new-privileges:true
		"--pids-limit" 64 --cpus 2 --memory 6g --memory-swap 6g
		"--tmpfs" /scratch:size=2g,mode=0700,uid=$(HOST_UID),gid=$(HOST_GID),noexec,nosuid,nodev
		"--mount" "type=bind,src=$$output_dir,dst=/output"
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/tools/convert_checkpoint.py,dst=/opt/checkpoint/convert.py,readonly"
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/configs/secs-v3.yaml,dst=/opt/checkpoint/model.yaml,readonly"
		"--entrypoint" python
	)
	$(DOCKER) run "$${extractor_args[@]}" "$(CHECKPOINT_IMAGE)" \
		"$${source_args[@]}" \
		--expected-archive-md5 "$(ARCHIVE_MD5)" \
		--member "$(ARCHIVE_MEMBER)" \
		--max-member-bytes "$(MAX_CHECKPOINT_BYTES)" \
		--scratch-directory /scratch \
	| $(DOCKER) run -i "$${converter_args[@]}" "$(CHECKPOINT_CONVERTER_IMAGE)" \
		-P /opt/checkpoint/convert.py \
		--scratch-directory /scratch \
		--weights-output /output/secs-v3.safetensors \
		--model-output /output/model.yaml \
		--manifest-output /output/manifest.json \
		--model-config /opt/checkpoint/model.yaml \
		--precision "$(CHECKPOINT_PRECISION)" \
		--source-record "$(ZENODO_RECORD)" \
		--source-archive-md5 "$(ARCHIVE_MD5)" \
		--source-member "$(ARCHIVE_MEMBER)" \
		--implementation-repository "$(SECS_REPOSITORY)" \
		--implementation-revision "$(SECS_REVISION)"

include make/packages.mk
