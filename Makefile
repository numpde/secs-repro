SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

override REPOSITORY_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
override HOST_UID := $(shell id -u)
override HOST_GID := $(shell id -g)
CHECKPOINT_IMAGE_TAG := secs-repro/checkpoint-extractor:local
override CHECKPOINT_SPECS := $(wildcard checkpoints/*/checkpoint.toml)
CHECKPOINT_SPEC ?= $(CHECKPOINT_SPECS)
override CHECKPOINT_DIRECTORY = $(patsubst %/,%,$(dir $(CHECKPOINT_SPEC)))
override CHECKPOINT_WEIGHTS := $(CHECKPOINT_DIRECTORY)/secs-v3.safetensors
override CHECKPOINT_WEIGHTS_FILENAME := $(notdir $(CHECKPOINT_WEIGHTS))
override CHECKPOINT_MANIFEST := $(CHECKPOINT_DIRECTORY)/manifest.json
override CHECKPOINT_MANIFEST_FILENAME := $(notdir $(CHECKPOINT_MANIFEST))
override SECS_REPOSITORY := $(shell git config -f .gitmodules --get submodule.secs.url)
override SECS_REVISION := $(shell git ls-files --stage secs | awk '{print $$2}')
DOCKER := env -u DOCKER_HOST -u DOCKER_CONTEXT docker --context default

.PHONY: help checkpoint checkpoint/image checkpoint/manifest

help:
	@printf '%s\n' \
		'SECS CPU workflow' \
		'' \
		'1. Prepare CPU dependencies' \
		'  make packages/base-images/pull packages/cpu/wheelhouse' \
		'      Pull the pinned base images and download the hash-locked CPU package archives.' \
		'' \
		'2. Prepare the checkpoint' \
		'  make checkpoint' \
		'      Stream and verify the archive configured by checkpoint.toml, then write' \
		'      secs-v3.safetensors and manifest.json beside checkpoint.toml.' \
		'  make checkpoint ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz' \
		'      Read a local archive without network access; the source file is left unchanged.' \
		'  make checkpoint ARCHIVE_URL=https://example.org/zenodo_secs_v3.tar.gz' \
		'      Download from this HTTPS URL instead of the URL in checkpoint.toml.' \
		'  make checkpoint CHECKPOINT_PRECISION=float16' \
		'      Store floating-point tensors as float16; float32 and bfloat16 are also supported.' \
		'      ARCHIVE and ARCHIVE_URL are alternatives; either may be combined with CHECKPOINT_PRECISION.' \
		'      The archive stream and extracted Lightning checkpoint are not retained after conversion.' \
		'' \
		'3. Prepare the MolFormer cache' \
		'  make molformer/cache' \
		'      Download the pinned MolFormer tokenizer, configuration, and model code into cache/.' \
		'      Steps 2 and 3 may be completed in either order.' \
		'' \
		'4. Run the CPU integration proof' \
		'  make test/integration' \
		'      Rank SMILES and run one GA generation without network access.' \
		'  make test/integration/bruker-reference' \
		'      Compare nmrglue decoding of F3697/1 with the pinned frontend vector.' \
		'' \
		'Dependency maintenance' \
		'' \
		'  make checkpoint/manifest' \
		'      Recreate manifest.json only if its recorded specification and weight hashes still match.' \
		'  make packages/base-images/pull packages/locks/write' \
		'      Regenerate the CPU and GPU dependency locks after dependency intent changes.' \
		'  make fixtures/frontend-reference/base-image/pull fixtures/frontend-reference/write' \
		'      Recreate the F3697/1 SECS vector with the pinned frontend in an offline container.' \
		'' \
		'Optional package images' \
		'' \
		'  make packages/cpu/image' \
		'      Build the CPU package image offline; steps 2-4 build or reuse it automatically.' \
		'  make packages/base-images/pull packages/gpu/wheelhouse packages/gpu/image' \
		'      Pull the base images, download the larger CUDA-enabled package set, and build' \
		'      the GPU package image offline.' \
		'' \
		'No GPU integration test is currently provided.'

checkpoint/image:
	@$(DOCKER) build --quiet --network none --pull=false \
		--file containers/checkpoint/Dockerfile \
		--tag "$(CHECKPOINT_IMAGE_TAG)" \
		"$(REPOSITORY_ROOT)"

checkpoint: private export ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE),)
checkpoint: private export ARCHIVE_URL_INPUT := $(if $(filter command line,$(origin ARCHIVE_URL)),$(value ARCHIVE_URL),)
checkpoint: private export CHECKPOINT_PRECISION_INPUT := $(if $(filter command line,$(origin CHECKPOINT_PRECISION)),$(value CHECKPOINT_PRECISION),)
checkpoint:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Run make checkpoint as a non-root host user.' >&2
		exit 2
	fi
	selected_spec=
	case " $(CHECKPOINT_SPECS) " in *" $(CHECKPOINT_SPEC) "*) selected_spec=1 ;; esac
	if test "$(words $(CHECKPOINT_SPEC))" -ne 1 || test -z "$$selected_spec"; then
		printf '%s\n' 'CHECKPOINT_SPEC must name exactly one checkpoints/*/checkpoint.toml file.' >&2
		exit 2
	fi
	if test -n "$${ARCHIVE_INPUT:-}" && test -n "$${ARCHIVE_URL_INPUT:-}"; then
		printf '%s\n' 'Choose one archive source: set either ARCHIVE or ARCHIVE_URL, not both.' >&2
		exit 2
	fi
	if test -n "$${ARCHIVE_INPUT:-}"; then
		if test ! -f "$${ARCHIVE_INPUT}"; then
			printf '%s\n' "ARCHIVE must name an existing regular file: $${ARCHIVE_INPUT}" >&2
			exit 2
		fi
		archive_path=$$(realpath -e -- "$${ARCHIVE_INPUT}")
	fi
	output_dir=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	if test -e "$(CHECKPOINT_WEIGHTS)" || test -L "$(CHECKPOINT_WEIGHTS)"; then
		printf '%s\n' 'Refusing to overwrite existing checkpoint weights: $(CHECKPOINT_WEIGHTS)' >&2
		exit 2
	fi
	checkpoint_stage=$$(mktemp -d --tmpdir="$$output_dir" .checkpoint.XXXXXXXX)
	trap 'rm -rf "$$checkpoint_stage"' EXIT
	checkpoint_stage_dir=$$(realpath -e -- "$$checkpoint_stage")
	converter_image=$$($(MAKE) --no-print-directory packages/cpu/image)
	extractor_image=$$($(MAKE) --no-print-directory checkpoint/image)
	extractor_args=(--rm --init --pull never --read-only
		"--cap-drop" ALL --security-opt no-new-privileges:true
		"--pids-limit" 32 --cpus 1 --memory 2304m --memory-swap 2304m
		"--tmpfs" /scratch:size=2g,mode=1777,noexec,nosuid,nodev
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly"
	)
	archive_source_args=()
	if test -n "$${ARCHIVE_INPUT:-}"; then
		extractor_args+=(--network none --mount "type=bind,src=$$archive_path,dst=/input/archive.tar.gz,readonly")
		archive_source_args=(--archive /input/archive.tar.gz)
	elif test -n "$${ARCHIVE_URL_INPUT:-}"; then
		extractor_args+=(--network bridge)
		archive_source_args=(--url "$${ARCHIVE_URL_INPUT}")
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
		"--mount" "type=bind,src=$$checkpoint_stage_dir,dst=/output"
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/tools/convert_checkpoint.py,dst=/opt/checkpoint/convert.py,readonly"
		"--mount" "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly"
		"--entrypoint" python
	)
	$(DOCKER) run "$${extractor_args[@]}" "$$extractor_image" \
		"$${archive_source_args[@]}" \
		--spec /input/checkpoint.toml \
		--scratch-directory /scratch \
	| $(DOCKER) run -i "$${converter_args[@]}" "$$converter_image" \
		-P /opt/checkpoint/convert.py \
		--scratch-directory /scratch \
		--weights-output "/output/$(CHECKPOINT_WEIGHTS_FILENAME)" \
		--spec /input/checkpoint.toml \
		--run-name "$(notdir $(CHECKPOINT_DIRECTORY))" \
		"$${precision_args[@]}"
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/tools/write_checkpoint_manifest.py,dst=/opt/checkpoint/write_manifest.py,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly" \
		--mount "type=bind,src=$$checkpoint_stage_dir/$(CHECKPOINT_WEIGHTS_FILENAME),dst=/input/$(CHECKPOINT_WEIGHTS_FILENAME),readonly" \
		--mount "type=bind,src=$$checkpoint_stage_dir,dst=/output" \
		--entrypoint python "$$extractor_image" \
		-P /opt/checkpoint/write_manifest.py \
		--weights /input/$(CHECKPOINT_WEIGHTS_FILENAME) \
		--manifest-output /output/$(CHECKPOINT_MANIFEST_FILENAME) \
		--spec /input/checkpoint.toml \
		--reference-repository "$(SECS_REPOSITORY)" \
		--reference-revision "$(SECS_REVISION)"
	chmod 0644 "$$checkpoint_stage/$(CHECKPOINT_WEIGHTS_FILENAME)"
	# Publish weights last; their presence marks a complete checkpoint.
	mv -f "$$checkpoint_stage/$(CHECKPOINT_MANIFEST_FILENAME)" "$(CHECKPOINT_MANIFEST)"
	ln "$$checkpoint_stage/$(CHECKPOINT_WEIGHTS_FILENAME)" "$(CHECKPOINT_WEIGHTS)"

checkpoint/manifest:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Run make checkpoint/manifest as a non-root host user.' >&2
		exit 2
	fi
	selected_spec=
	case " $(CHECKPOINT_SPECS) " in *" $(CHECKPOINT_SPEC) "*) selected_spec=1 ;; esac
	if test "$(words $(CHECKPOINT_SPEC))" -ne 1 || test -z "$$selected_spec"; then
		printf '%s\n' 'Cannot refresh manifest: CHECKPOINT_SPEC must name exactly one checkpoints/*/checkpoint.toml file.' >&2
		exit 2
	fi
	if test ! -f "$(CHECKPOINT_WEIGHTS)"; then
		printf '%s\n' 'Cannot refresh manifest because checkpoint weights are missing: $(CHECKPOINT_WEIGHTS). Run make checkpoint first.' >&2
		exit 2
	fi
	if test ! -f "$(CHECKPOINT_MANIFEST)"; then
		printf '%s\n' 'Cannot refresh manifest because the existing manifest is missing: $(CHECKPOINT_MANIFEST). Restore the manifest that belongs to these weights before retrying.' >&2
		exit 2
	fi
	output_dir=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	manifest_stage=$$(mktemp -d --tmpdir="$$output_dir" .manifest.XXXXXXXX)
	trap 'rm -rf "$$manifest_stage"' EXIT
	manifest_stage_dir=$$(realpath -e -- "$$manifest_stage")
	extractor_image=$$($(MAKE) --no-print-directory checkpoint/image)
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/tools/write_checkpoint_manifest.py,dst=/opt/checkpoint/write_manifest.py,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_SPEC),dst=/input/checkpoint.toml,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_WEIGHTS),dst=/input/$(CHECKPOINT_WEIGHTS_FILENAME),readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(CHECKPOINT_MANIFEST),dst=/input/$(CHECKPOINT_MANIFEST_FILENAME),readonly" \
		--mount "type=bind,src=$$manifest_stage_dir,dst=/output" \
		--entrypoint python "$$extractor_image" \
		-P /opt/checkpoint/write_manifest.py \
		--weights /input/$(CHECKPOINT_WEIGHTS_FILENAME) \
		--existing-manifest /input/$(CHECKPOINT_MANIFEST_FILENAME) \
		--manifest-output /output/$(CHECKPOINT_MANIFEST_FILENAME) \
		--spec /input/checkpoint.toml \
		--reference-repository "$(SECS_REPOSITORY)" \
		--reference-revision "$(SECS_REVISION)"
	mv -f "$$manifest_stage/$(CHECKPOINT_MANIFEST_FILENAME)" "$(CHECKPOINT_MANIFEST)"

include make/packages.mk
include make/molformer.mk
include make/tests.mk
include make/fixtures.mk
