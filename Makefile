SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
HOST_UID := $(shell id -u)
HOST_GID := $(shell id -g)
CHECKPOINT_IMAGE := secs-repro/checkpoint-extractor:local
CHECKPOINT_CONVERTER_IMAGE := secs-repro/packages-cpu:local
CHECKPOINT_SPECS := $(wildcard checkpoints/*/checkpoint.toml)
CHECKPOINT_SPEC ?= $(CHECKPOINT_SPECS)
CHECKPOINT_DIRECTORY = $(patsubst %/,%,$(dir $(CHECKPOINT_SPEC)))
CHECKPOINT_WEIGHTS := $(CHECKPOINT_DIRECTORY)/secs-v3.safetensors
CHECKPOINT_MANIFEST := $(CHECKPOINT_DIRECTORY)/manifest.json
SECS_REPOSITORY := $(shell git config -f .gitmodules --get submodule.secs.url)
SECS_REVISION := $(shell git ls-files --stage secs | awk '{print $$2}')
DOCKER := env -u DOCKER_HOST -u DOCKER_CONTEXT docker --context default

.PHONY: help checkpoint checkpoint/image checkpoint/manifest

help:
	@printf '%s\n' \
		'SECS checkpoint preparation' \
		'' \
		'  make checkpoint' \
		'      Stream the pinned Zenodo archive and publish inference-only artifacts.' \
		'  make checkpoint ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz' \
		'      Read the same archive from a local, read-only path without network access.' \
		'  make checkpoint CHECKPOINT_PRECISION=float16' \
		'      Override the specification precision with float32, float16, or bfloat16.' \
		'  make checkpoint/manifest' \
		'      Refresh the receipt only when archive identity and weight hash still match.' \
		'' \
		'SECS package images' \
		'' \
		'  make packages/base-images/pull packages/locks/write' \
		'      Pull pinned bases and write CPU/GPU hash locks.' \
		'  make packages/cpu/wheelhouse packages/gpu/wheelhouse' \
		'      Download locked artifacts in bounded containers.' \
		'  make packages/images' \
		'      Build both images without network access from existing wheelhouses.' \
		'  make molformer/cache' \
		'      Materialize the pinned non-weight MolFormer Hugging Face cache.' \
		'' \
		'SECS tests' \
		'' \
		'  make test/integration' \
		'      Run real-artifact integration tests offline in the CPU package image.' \
		'' \
		'The archive and Lightning checkpoint are not retained.'

checkpoint/image:
	@$(DOCKER) build --network none --pull=false \
		--file containers/checkpoint/Dockerfile \
		--tag "$(CHECKPOINT_IMAGE)" \
		"$(REPOSITORY_ROOT)"

checkpoint: private export ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE),)
checkpoint: private export ARCHIVE_URL_INPUT := $(if $(filter command line,$(origin ARCHIVE_URL)),$(value ARCHIVE_URL),)
checkpoint: private export CHECKPOINT_PRECISION_INPUT := $(if $(filter command line,$(origin CHECKPOINT_PRECISION)),$(value CHECKPOINT_PRECISION),)
checkpoint:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot prepare the checkpoint as host UID 0.' >&2
		exit 2
	fi
	if test "$(words $(CHECKPOINT_SPEC))" -ne 1 || test ! -f "$(CHECKPOINT_SPEC)"; then
		printf '%s\n' 'CHECKPOINT_SPEC must select one existing checkpoint.toml.' >&2
		exit 2
	fi
	if test -n "$${ARCHIVE_INPUT:-}" && test -n "$${ARCHIVE_URL_INPUT:-}"; then
		printf '%s\n' 'ARCHIVE and ARCHIVE_URL cannot be supplied together.' >&2
		exit 2
	fi
	if test -n "$${ARCHIVE_INPUT:-}"; then
		if test ! -f "$${ARCHIVE_INPUT}"; then
			printf '%s\n' "ARCHIVE must name a regular file: $${ARCHIVE_INPUT}" >&2
			exit 2
		fi
		archive_path=$$(realpath -e -- "$${ARCHIVE_INPUT}")
	fi
	output_dir=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	if test -e "$(CHECKPOINT_WEIGHTS)" || test -L "$(CHECKPOINT_WEIGHTS)"; then
		printf '%s\n' 'Cannot prepare checkpoint: $(CHECKPOINT_WEIGHTS) already exists.' >&2
		exit 2
	fi
	weights_stage=$$(mktemp -d --tmpdir="$$output_dir" .weights.XXXXXXXX)
	manifest_stage=$$(mktemp -d --tmpdir="$$output_dir" .manifest.XXXXXXXX)
	trap 'rm -rf "$$weights_stage" "$$manifest_stage"' EXIT
	weights_stage_dir=$$(realpath -e -- "$$weights_stage")
	manifest_stage_dir=$$(realpath -e -- "$$manifest_stage")
	$(MAKE) --no-print-directory packages/cpu/image
	$(MAKE) --no-print-directory checkpoint/image
	extractor_args=(--rm --init --pull never --read-only
		"--cap-drop" ALL --security-opt no-new-privileges:true
		"--pids-limit" 32 --cpus 1 --memory 2304m --memory-swap 2304m
		"--tmpfs" /scratch:size=2g,mode=0700,uid=65532,gid=65532,noexec,nosuid,nodev
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly"
	)
	source_args=()
	if test -n "$${ARCHIVE_INPUT:-}"; then
		extractor_args+=(--network none --mount "type=bind,src=$$archive_path,dst=/input/archive.tar.gz,readonly")
		source_args=(--archive /input/archive.tar.gz)
	elif test -n "$${ARCHIVE_URL_INPUT:-}"; then
		extractor_args+=(--network bridge)
		source_args=(--url "$${ARCHIVE_URL_INPUT}")
	else
		extractor_args+=(--network bridge)
	fi
	precision_args=()
	if test -n "$${CHECKPOINT_PRECISION_INPUT:-}"; then
		precision_args=(--precision "$${CHECKPOINT_PRECISION_INPUT}")
	fi
	converter_args=(--rm --init --pull never --network none --read-only --user "$(HOST_UID):$(HOST_GID)"
		"--cap-drop" ALL --security-opt no-new-privileges:true
		"--pids-limit" 64 --cpus 2 --memory 6g --memory-swap 6g
		"--tmpfs" /scratch:size=2g,mode=0700,uid=$(HOST_UID),gid=$(HOST_GID),noexec,nosuid,nodev
		"--mount" "type=bind,src=$$weights_stage_dir,dst=/output"
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/tools/convert_checkpoint.py,dst=/opt/checkpoint/convert.py,readonly"
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly"
		"--entrypoint" python
	)
	$(DOCKER) run "$${extractor_args[@]}" "$(CHECKPOINT_IMAGE)" \
		"$${source_args[@]}" \
		--spec /input/checkpoint.toml \
		--scratch-directory /scratch \
	| $(DOCKER) run -i "$${converter_args[@]}" "$(CHECKPOINT_CONVERTER_IMAGE)" \
		-P /opt/checkpoint/convert.py \
		--scratch-directory /scratch \
		--weights-output /output/secs-v3.safetensors \
		--spec /input/checkpoint.toml \
		--run-name "$(notdir $(CHECKPOINT_DIRECTORY))" \
		"$${precision_args[@]}"
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/tools/write_checkpoint_manifest.py,dst=/opt/checkpoint/write_manifest.py,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly" \
		--mount "type=bind,src=$$weights_stage_dir/secs-v3.safetensors,dst=/input/secs-v3.safetensors,readonly" \
		--mount "type=bind,src=$$manifest_stage_dir,dst=/output" \
		--entrypoint python "$(CHECKPOINT_IMAGE)" \
		-P /opt/checkpoint/write_manifest.py \
		--weights /input/secs-v3.safetensors \
		--manifest-output /output/manifest.json \
		--spec /input/checkpoint.toml \
		--reference-repository "$(SECS_REPOSITORY)" \
		--reference-revision "$(SECS_REVISION)"
	mv -f "$$manifest_stage/manifest.json" "$(CHECKPOINT_MANIFEST)"
	ln "$$weights_stage/secs-v3.safetensors" "$(CHECKPOINT_WEIGHTS)"

checkpoint/manifest:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot refresh the checkpoint manifest as host UID 0.' >&2
		exit 2
	fi
	if test "$(words $(CHECKPOINT_SPEC))" -ne 1 \
		|| test ! -f "$(CHECKPOINT_SPEC)" \
		|| test ! -f "$(CHECKPOINT_WEIGHTS)" \
		|| test ! -f "$(CHECKPOINT_MANIFEST)"; then
		printf '%s\n' 'Checkpoint specification, weights, or existing manifest are absent.' >&2
		exit 2
	fi
	output_dir=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	stage=$$(mktemp -d --tmpdir="$$output_dir" .manifest.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	stage_dir=$$(realpath -e -- "$$stage")
	$(MAKE) --no-print-directory checkpoint/image
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/tools/write_checkpoint_manifest.py,dst=/opt/checkpoint/write_manifest.py,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_WEIGHTS),dst=/input/secs-v3.safetensors,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_MANIFEST),dst=/input/manifest.json,readonly" \
		--mount "type=bind,src=$$stage_dir,dst=/output" \
		--entrypoint python "$(CHECKPOINT_IMAGE)" \
		-P /opt/checkpoint/write_manifest.py \
		--weights /input/secs-v3.safetensors \
		--existing-manifest /input/manifest.json \
		--manifest-output /output/manifest.json \
		--spec /input/checkpoint.toml \
		--reference-repository "$(SECS_REPOSITORY)" \
		--reference-revision "$(SECS_REVISION)"
	mv -f "$$stage/manifest.json" "$(CHECKPOINT_MANIFEST)"

include make/packages.mk
include make/molformer.mk
include make/tests.mk
