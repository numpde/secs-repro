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
ifeq ($(MAKELEVEL),0)
export SECS_NETWORK_INTERFACE_INPUT := $(value NETWORK_INTERFACE)
export SECS_NETWORK_INTERFACE_ORIGIN := $(origin NETWORK_INTERFACE)
export SECS_OLD_WLAN_ORIGIN := $(origin WLAN_INTERFACE)
export SECS_WHEELHOUSE_INPUT := $(value WHEELHOUSE)
export SECS_WHEELHOUSE_ORIGIN := $(origin WHEELHOUSE)
else ifneq ($(origin NETWORK_INTERFACE),undefined)
export SECS_NETWORK_INTERFACE_INPUT := $(value NETWORK_INTERFACE)
export SECS_NETWORK_INTERFACE_ORIGIN := $(origin NETWORK_INTERFACE)
endif
unexport WHEELHOUSE
unexport NETWORK_INTERFACE
unexport WLAN_INTERFACE
shell_quote = '$(subst ','"'"',$(1))'
unexport DOCKER_HOST DOCKER_CONTEXT SECS_DOCKER_ENDPOINT
override export SECS_BOOTSTRAP_IMAGE = $(PYTHON_BASE)
DOCKER := env -u DOCKER_HOST -u DOCKER_CONTEXT $(CURDIR)/tools/docker.sh
DOCKER_MAKE = $(MAKE) SECS_DOCKER_ENDPOINT=$(call shell_quote,$(SECS_DOCKER_ENDPOINT))

.PHONY: help checkpoint checkpoint/image checkpoint/manifest
DOCKER_MAKE_TARGETS += checkpoint checkpoint/image checkpoint/manifest

help:
	@printf '%s\n' \
	'SECS CPU workflow' \
	'' \
	'Network routing' \
	'  make <target> NETWORK_INTERFACE=<name|system>' \
	'      Route cooperative HTTP(S) downloads through that interface, or choose system routing.' \
	'      Omit it to choose interactively; noninteractive downloads require a choice.' \
	'      Docker daemon image pulls continue to use the daemon default route.' \
	'  Select the shared dependency cache with WHEELHOUSE=<directory>.' \
	'      Omit it to choose interactively; unattended dependency and image commands require it.' \
	'' \
	'Docker context' \
	'  make <Docker target> DOCKER_CONTEXT=<local-context>' \
	'      On this host and ProArt, use DOCKER_CONTEXT=science. Direct Docker commands use --context science.' \
	'      Elsewhere, select a local Unix-socket context or omit it in a terminal to choose from a menu.' \
	'      There is no default; noninteractive Docker commands require DOCKER_CONTEXT.' \
	'      The socket endpoint is resolved once and pinned through the whole make operation, including cleanup.' \
	'      Remote TCP and SSH contexts are not supported; the global current context is never changed.' \
	'' \
	'1. Prepare CPU dependencies' \
		'  make packages/base-images/pull packages/cpu/wheelhouse DOCKER_CONTEXT=science' \
		'      Pull the pinned base images and download the hash-locked CPU package archives.' \
		'' \
		'2. Prepare the checkpoint' \
		'  make checkpoint DOCKER_CONTEXT=science' \
		'      Stream and verify the archive configured by checkpoint.toml, then write' \
		'      secs-v3.safetensors and manifest.json beside checkpoint.toml.' \
		'  make checkpoint ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz DOCKER_CONTEXT=science' \
		'      Read a local archive without network access; the source file is left unchanged.' \
		'  make checkpoint ARCHIVE_URL=https://example.org/zenodo_secs_v3.tar.gz DOCKER_CONTEXT=science' \
		'      Download from this HTTPS URL instead of the URL in checkpoint.toml.' \
		'  make checkpoint CHECKPOINT_PRECISION=float16 DOCKER_CONTEXT=science' \
		'      Store floating-point tensors as float16; float32 and bfloat16 are also supported.' \
		'      ARCHIVE and ARCHIVE_URL are alternatives; either may be combined with CHECKPOINT_PRECISION.' \
		'      The archive stream and extracted Lightning checkpoint are not retained after conversion.' \
		'' \
		'3. Prepare the MolFormer cache' \
		'  make molformer/cache DOCKER_CONTEXT=science' \
		'      Download the pinned MolFormer tokenizer, configuration, and model code into cache/.' \
		'      Steps 2 and 3 may be completed in either order.' \
		'' \
		'4. Run the CPU integration proof' \
		'  make test/integration DOCKER_CONTEXT=science' \
		'      Rank SMILES and run one GA generation without network access.' \
	'  make test/integration/bruker-reference DOCKER_CONTEXT=science' \
	'      Compare nmrglue decoding of F3697/1 with the pinned frontend vector.' \
	'  make test/integration/jcamp-reference DOCKER_CONTEXT=science' \
	'      Compare processed JCAMP-DX decoding with its pinned frontend vector.' \
	'  make test/integration/challenges CANDIDATE_GPU=GPU-<uuid> DOCKER_CONTEXT=science' \
	'      Check all 20 published ScoreOnly ranks and one Bruker GA generation.' \
	'      One GPU model and full index load are shared across the cases.' \
	'  make test/integration/challenges/bruker CANDIDATE_GPU=GPU-<uuid> DOCKER_CONTEXT=science' \
	'      Run only the Bruker GA test; still loads the GPU model and full index.' \
	'  make test/provider DOCKER_CONTEXT=science' \
	'      Verify input selection and shared SDK lifecycle composition offline.' \
	'  make provider/support/check' \
	'      Verify that public support documentation matches the code-owned support catalog.' \
	'  make provider/support/write' \
	'      Update only the bounded public support section, then review the result.' \
	'  make test/support' \
	'      Check public support projections and every declared semantic evidence requirement.' \
	'  make test/deployment' \
	'      Test configuration publication, saved-state ownership and container lifecycle rules.' \
	'  make test/deployment/e2e DOCKER_CONTEXT=science' \
	'      Test offline Compose start/stop/restart and retained state using disposable services.' \
	'  make test/provider/mock-runner/e2e DOCKER_CONTEXT=science' \
	'      Test the mock runner and durable delivery handoff without model files.' \
	'  make provider/deployment/init DEPLOYMENT=<name> [TARGET=cpu|gpu] [USE_MOCK_INDEX=yes|no] [USE_MOCK_RUNNER=yes|no]' \
	'      The mock index contains eight molecules and requires CPU; omitted TARGET prompts.' \
	'      Mock runner performs no model work; it cannot be combined with a mock index.' \
	'      Copy committed examples into private config/deployments/<name>; no services start.' \
	'  make provider/deployment/config DEPLOYMENT=<name> [USE_MOCK_INDEX=yes|no] DOCKER_CONTEXT=science' \
	'      Render saved provider.toml and worker settings; Docker runs the existing provider image to project shared grants.' \
	'      No deployment services start.' \
	'      [shared].project and gid must match the shared services and select their volumes and socket group.' \
	'  make provider/credential/install DEPLOYMENT=<name> SOURCE=<private-file>' \
	'  make provider/interpreter-key/install DEPLOYMENT=<name> SOURCE=<private-file>' \
	'      Install an owner-only secret once; existing files are never overwritten.' \
	'  make provider/deployment/up DEPLOYMENT=<name> [USE_MOCK_INDEX=yes|no] DOCKER_CONTEXT=science' \
	'      Start the named deployment from its saved config using existing images; no build or download.' \
	'  make provider/deployment/status DEPLOYMENT=<name> DOCKER_CONTEXT=science' \
	'  make provider/logs DEPLOYMENT=<name> DOCKER_CONTEXT=science' \
	'  make provider/deployment/down DEPLOYMENT=<name> DOCKER_CONTEXT=science' \
	'      Down drains provider, then stops the worker; state and containers remain.' \
	'  make provider/deployment/retire-api DEPLOYMENT=<name> DOCKER_CONTEXT=science' \
	'      Drain and retire the old private API route; preserve scientific and shared storage.' \
	'  make test/provider/e2e DOCKER_CONTEXT=science' \
	'      Prove hello, verified input, real model/FAISS/GA and durable handoff retry offline.' \
	'      Uses eight fixture candidates, not the full index; see notes/004_provider_execution.txt.' \
	'  make interpreter/model-behavior CONFIG_DIR=<provider-config-dir> KEY_FILE=<interpreter-key> DOCKER_CONTEXT=science' \
	'      Opt-in paid live interpreter tests; no Job API or GPU work.' \
	'' \
	'5. Build the GPU candidate bundle' \
	'  make packages/base-images/pull packages/gpu/wheelhouse DOCKER_CONTEXT=science' \
	'      Refresh the pinned GPU package archives before building the candidate image.' \
	'  make candidates CANDIDATE_GPU=GPU-<uuid> DOCKER_CONTEXT=science' \
	'      Stream the configured PubChem parquet, embed its SMILES on the selected GPU, and publish' \
	'      a row-aligned table, FAISS index, and manifest under the checkpoint directory.' \
	'  make candidates CANDIDATE_GPU=GPU-<uuid> ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz DOCKER_CONTEXT=science' \
	'      Read the pinned archive locally without granting the extractor network access.' \
	'      ARCHIVE_URL overrides the configured HTTPS source instead; do not set both.' \
	'      CANDIDATE_GPU requires one full NVIDIA GPU UUID; there is no default GPU.' \
	'      Find UUIDs with: nvidia-smi --query-gpu=uuid,name --format=csv' \
	'      CANDIDATE_CPUS and CANDIDATE_MEMORY tune resources.' \
	'      CANDIDATE_DTYPE selects float32 or bfloat16 forward compute.' \
	'' \
	'6. Qualify the candidate builder without publishing a production bundle' \
	'  make candidates/qualification CANDIDATE_GPU=GPU-<uuid> ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz DOCKER_CONTEXT=science' \
	'      Build, reload, and search fixed nested real-data samples, then retain only' \
	'      non-production evidence and a bounded full-build resource projection.' \
	'' \
	'Dependency maintenance' \
		'' \
		'  make provider/wheelhouse provider/native-wheelhouse provider/image DOCKER_CONTEXT=science' \
		'      Install the issued API and native SDK profiles; build the API provider separately.' \
		'  make checkpoint/manifest DOCKER_CONTEXT=science' \
		'      Recreate manifest.json only if its recorded specification and weight hashes still match.' \
		'  make packages/base-images/pull packages/locks/write DOCKER_CONTEXT=science' \
		'      Regenerate the CPU and GPU dependency locks after dependency intent changes.' \
		'  make fixtures/frontend-reference/base-image/pull fixtures/frontend-reference/write DOCKER_CONTEXT=science' \
		'      Recreate the Bruker and processed JCAMP-DX SECS vectors with the pinned frontend.' \
		'' \
		'Optional package images' \
		'' \
		'  make packages/cpu/image DOCKER_CONTEXT=science' \
		'      Build the CPU package image offline; steps 2-4 build or reuse it automatically.' \
		'  make packages/base-images/pull packages/gpu/wheelhouse packages/gpu/image DOCKER_CONTEXT=science' \
		'      Pull the base images, download the larger CUDA-enabled package set, and build' \
		'      the GPU package image offline.' \
		''

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
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$checkpoint_stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	checkpoint_stage_dir=$$(realpath -e -- "$$checkpoint_stage")
	converter_image=$$($(DOCKER_MAKE) --no-print-directory packages/cpu/image)
	extractor_image=$$($(DOCKER_MAKE) --no-print-directory checkpoint/image)
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
	secs_engine_active=1
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
	secs_engine_active=0
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
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$manifest_stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	manifest_stage_dir=$$(realpath -e -- "$$manifest_stage")
	extractor_image=$$($(DOCKER_MAKE) --no-print-directory checkpoint/image)
	secs_engine_active=1
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
	secs_engine_active=0
	mv -f "$$manifest_stage/$(CHECKPOINT_MANIFEST_FILENAME)" "$(CHECKPOINT_MANIFEST)"

include make/packages.mk
include make/provider.mk
include make/molformer.mk
include make/tests.mk
include make/candidates.mk
include make/candidate-qualification.mk
include make/fixtures.mk

# Resolve one context before a Docker-using make operation starts. The selected
# endpoint, rather than the mutable context name, is inherited by recursive make
# and retained for every later cleanup command in this operation.
ifneq ($(filter $(DOCKER_MAKE_TARGETS),$(MAKECMDGOALS)),)
ifeq ($(MAKELEVEL),0)
ifeq ($(origin DOCKER_CONTEXT),command line)
override export SECS_DOCKER_ENDPOINT := $(shell env -u DOCKER_HOST -u DOCKER_CONTEXT -u SECS_DOCKER_ENDPOINT DOCKER_CONTEXT=$(call shell_quote,$(value DOCKER_CONTEXT)) python3 tools/docker_context.py --command-line-context)
else
override export SECS_DOCKER_ENDPOINT := $(shell env -u DOCKER_HOST -u DOCKER_CONTEXT -u SECS_DOCKER_ENDPOINT python3 tools/docker_context.py --interactive)
endif
else ifeq ($(origin SECS_DOCKER_ENDPOINT),command line)
override export SECS_DOCKER_ENDPOINT := $(shell env -u DOCKER_HOST -u DOCKER_CONTEXT -u SECS_DOCKER_ENDPOINT SECS_DOCKER_ENDPOINT=$(call shell_quote,$(value SECS_DOCKER_ENDPOINT)) python3 tools/docker_context.py --inherited-endpoint)
else ifeq ($(origin DOCKER_CONTEXT),command line)
override export SECS_DOCKER_ENDPOINT := $(shell env -u DOCKER_HOST -u DOCKER_CONTEXT -u SECS_DOCKER_ENDPOINT DOCKER_CONTEXT=$(call shell_quote,$(value DOCKER_CONTEXT)) python3 tools/docker_context.py --command-line-context)
else
override export SECS_DOCKER_ENDPOINT := $(shell env -u DOCKER_HOST -u DOCKER_CONTEXT -u SECS_DOCKER_ENDPOINT python3 tools/docker_context.py --interactive)
endif
ifeq ($(strip $(SECS_DOCKER_ENDPOINT)),)
$(error A local Docker context must be selected before running Docker targets)
endif
endif
