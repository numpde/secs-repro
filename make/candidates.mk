CANDIDATE_SPEC := $(CHECKPOINT_DIRECTORY)/candidates.toml
CANDIDATE_DIRECTORY := $(CHECKPOINT_DIRECTORY)/candidates
CANDIDATE_GPU ?= 0
CANDIDATE_CPUS ?= 8
# Qualification applies its reserve policy to this same production limit, so
# the default must stay above the measured full-build projection.
CANDIDATE_MEMORY ?= 40g
CANDIDATE_DTYPE ?= bfloat16

.PHONY: candidates
candidates: export ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(ARCHIVE))
candidates: export ARCHIVE_URL_INPUT := $(if $(filter command line,$(origin ARCHIVE_URL)),$(ARCHIVE_URL))
candidates: export CANDIDATE_GPU_INPUT := $(CANDIDATE_GPU)
candidates: export CANDIDATE_CPUS_INPUT := $(CANDIDATE_CPUS)
candidates: export CANDIDATE_MEMORY_INPUT := $(CANDIDATE_MEMORY)
candidates: export CANDIDATE_DTYPE_INPUT := $(CANDIDATE_DTYPE)
candidates: checkpoint/image packages/gpu/image
	@set -eu -o pipefail; \
	test "$$(id -u)" -ne 0 || { \
		printf '%s\n' 'Run make candidates as a non-root host user.' >&2; exit 2; \
	}; \
	test -z "$${ARCHIVE_INPUT}" || test -z "$${ARCHIVE_URL_INPUT}" || { \
		printf '%s\n' 'ARCHIVE and ARCHIVE_URL are mutually exclusive.' >&2; exit 2; \
	}; \
	[[ "$${CANDIDATE_GPU_INPUT}" =~ ^[0-9]+$$ ]] || { \
		printf '%s\n' 'CANDIDATE_GPU must name one GPU by its nonnegative integer index.' >&2; exit 2; \
	}; \
	case "$${CANDIDATE_DTYPE_INPUT}" in \
		float32|bfloat16) ;; \
		*) printf 'Cannot build candidates with CANDIDATE_DTYPE=%q; select float32 or bfloat16 forward compute.\n' \
			"$${CANDIDATE_DTYPE_INPUT}" >&2; exit 2 ;; \
	esac; \
	test ! -e "$(CANDIDATE_DIRECTORY)" || { \
		printf '%s\n' 'Candidate bundle already exists at $(CANDIDATE_DIRECTORY).' >&2; exit 2; \
	}; \
	checkpoint_directory=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)"); \
	candidate_spec=$$(realpath -e -- "$(CANDIDATE_SPEC)"); \
	cache_directory=$$(realpath -e -- "$(MOLFORMER_CACHE)"); \
	package_image=$$($(DOCKER) image inspect --format '{{.Id}}' "$(call packages_image_tag,gpu)"); \
	$(DOCKER) run --rm --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 \
		--cpus 1 --memory 2g --memory-swap 2g --gpus "device=$${CANDIDATE_GPU_INPUT}" \
		--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m --entrypoint python "$$package_image" \
		-P -c 'import sys, torch; sys.exit("Selected GPU is unavailable to the candidate container.") if not torch.cuda.is_available() else torch.empty(1, device="cuda:0")'; \
	output_parent=$$(dirname -- "$(CANDIDATE_DIRECTORY)"); \
	mkdir -p -- "$$output_parent"; \
	stage=$$(mktemp -d "$$output_parent/.candidate-build.XXXXXX"); \
	stage=$$(realpath -e -- "$$stage"); \
	chmod 2770 "$$stage"; \
	trap 'rm -rf -- "$$stage"' EXIT HUP INT TERM; \
	run_consumer() { \
		$(DOCKER) run --rm -i --pull never --network none --read-only \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 512 \
			--cpus "$${CANDIDATE_CPUS_INPUT}" --memory "$${CANDIDATE_MEMORY_INPUT}" \
			--memory-swap "$${CANDIDATE_MEMORY_INPUT}" --gpus "device=$${CANDIDATE_GPU_INPUT}" \
			--group-add "$$(id -g)" \
			--tmpfs /scratch:rw,noexec,nosuid,nodev,size=2g \
			--tmpfs /modules:rw,noexec,nosuid,nodev,size=128m \
			--tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
			--env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 --env HF_HUB_CACHE=/cache/hub \
			--env HF_MODULES_CACHE=/modules --env PYTHONDONTWRITEBYTECODE=1 \
			--mount type=bind,src="$$checkpoint_directory",dst=/checkpoint,readonly \
			--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
			--mount type=bind,src="$(MOLFORMER_LOCK)",dst=/input/molformer.lock.toml,readonly \
			--mount type=bind,src="$$cache_directory",dst=/cache,readonly \
			--mount type=bind,src="$(CURDIR)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
			--mount type=bind,src="$(CURDIR)/tools/build_candidate_index.py",dst=/opt/build.py,readonly \
			--mount type=bind,src="$$stage",dst=/output \
			--entrypoint /bin/sh "$$package_image" \
			-c 'umask 0022; python -P /opt/materialize.py --verify-only --lock /input/molformer.lock.toml --output /cache && exec python -P /opt/build.py --archive-spec /checkpoint/checkpoint.toml --candidate-spec /input/candidates.toml --checkpoint-manifest /checkpoint/manifest.json --molformer-lock /input/molformer.lock.toml --scratch-directory /scratch --output-directory /output --source-kind "$$1" --device cuda:0 --compute-dtype "$$2" --threads "$$3" --package-image-id "$$4"' \
			candidate-builder "$$1" "$${CANDIDATE_DTYPE_INPUT}" "$${CANDIDATE_CPUS_INPUT}" "$$package_image"; \
	}; \
	run_network_extractor() { \
		$(DOCKER) run --rm --pull never --network bridge --read-only \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 \
			--cpus 1 --memory 2304m --memory-swap 2304m \
			--tmpfs /scratch:rw,noexec,nosuid,nodev,size=2g \
			--mount type=bind,src="$$checkpoint_directory/checkpoint.toml",dst=/input/checkpoint.toml,readonly \
			--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
			--mount type=bind,src="$(CURDIR)/tools/extract_checkpoint.py",dst=/opt/extract.py,readonly \
			--entrypoint python3 $(CHECKPOINT_IMAGE_TAG) \
			-P /opt/extract.py "$$@" --spec /input/checkpoint.toml \
			--member-spec /input/candidates.toml --scratch-directory /scratch; \
	}; \
	if test -n "$${ARCHIVE_INPUT}"; then \
		archive=$$(realpath -e -- "$${ARCHIVE_INPUT}"); \
		$(DOCKER) run --rm --pull never --network none --read-only \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 \
			--cpus 1 --memory 2304m --memory-swap 2304m \
			--tmpfs /scratch:rw,noexec,nosuid,nodev,size=2g \
			--mount type=bind,src="$$archive",dst=/archive/input.tar.gz,readonly \
			--mount type=bind,src="$$checkpoint_directory/checkpoint.toml",dst=/input/checkpoint.toml,readonly \
			--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
			--mount type=bind,src="$(CURDIR)/tools/extract_checkpoint.py",dst=/opt/extract.py,readonly \
			--entrypoint python3 $(CHECKPOINT_IMAGE_TAG) \
			-P /opt/extract.py --archive /archive/input.tar.gz --spec /input/checkpoint.toml \
			--member-spec /input/candidates.toml --scratch-directory /scratch \
		| run_consumer local; \
	elif test -n "$${ARCHIVE_URL_INPUT}"; then \
		run_network_extractor --url "$${ARCHIVE_URL_INPUT}" | run_consumer override; \
	else \
		run_network_extractor | run_consumer configured; \
	fi; \
	chmod g-s,u=rwx,go=rx "$$stage"; \
	mv -T -- "$$stage" "$(CANDIDATE_DIRECTORY)"; \
	trap - EXIT HUP INT TERM
