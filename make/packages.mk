override PACKAGES_PYTHON_VERSION := 3.12.12
override PYTHON_BASE := docker.io/library/python:$(PACKAGES_PYTHON_VERSION)-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
override UV_IMAGE := ghcr.io/astral-sh/uv@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d
PACKAGES_CPU_TORCH_WHEELS := https://download.pytorch.org/whl/cpu/torch/
PACKAGES_GPU_TORCH_WHEELS := https://download.pytorch.org/whl/cu130/torch/
PACKAGES_LOCK_IMAGE_TAG := secs-repro/packages-lock

.PHONY: packages/base-images/pull packages/lock-image packages/locks/write
.PHONY: packages/cpu/wheelhouse packages/gpu/wheelhouse packages/wheelhouse
.PHONY: packages/cpu/image packages/gpu/image packages/image packages/images

packages/base-images/pull:
	$(DOCKER) pull "$(PYTHON_BASE)"
	$(DOCKER) pull "$(UV_IMAGE)"

packages/lock-image:
	@$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PYTHON_BASE)" \
		--build-arg UV_IMAGE="$(UV_IMAGE)" \
		--file containers/packages/Dockerfile.lock \
		--tag "$(PACKAGES_LOCK_IMAGE_TAG)" .

packages/locks/write:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot write package locks as host UID 0.' >&2
		exit 2
	fi
	lock_image=$$($(MAKE) --no-print-directory packages/lock-image)
	locks_directory="$(REPOSITORY_ROOT)/requirements"
	stage=$$(mktemp -d --tmpdir="$$locks_directory" .packages-locks.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	for variant in cpu gpu; do
		case "$$variant" in
			cpu) group=packages-cpu ;;
			gpu) group=packages-gpu ;;
		esac
		$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
			--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
			--pull never --pids-limit 64 --cpus 2 --memory 1g --memory-swap 1g \
			--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
			--env UV_CACHE_DIR=/output/.uv-cache \
			--workdir /input \
			--mount type=bind,src="$(REPOSITORY_ROOT)/pyproject.toml",dst=/input/pyproject.toml,readonly \
			--mount type=bind,src="$(REPOSITORY_ROOT)/secs",dst=/input/secs,readonly \
			--mount type=bind,src="$$stage",dst=/output \
			"$$lock_image" pyproject.toml \
			--extra elucidation \
			--group secs/pyproject.toml:packages-build \
			--group "secs/pyproject.toml:$$group" --no-emit-package secs \
			--python-version "$(PACKAGES_PYTHON_VERSION)" --python-platform x86_64-manylinux_2_28 \
			--generate-hashes --index-url https://pypi.org/simple \
			--output-file "/output/packages-$$variant.raw"
		$(DOCKER) run --rm --network none --read-only --cap-drop ALL \
			--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
			--pull never --pids-limit 16 --cpus 1 --memory 64m --memory-swap 64m \
			--entrypoint python --mount type=bind,src="$$stage",dst=/output \
			"$$lock_image" /usr/local/bin/normalize_lock.py \
			"/output/packages-$$variant.raw" "/output/packages-$$variant.lock"
		rm "$$stage/packages-$$variant.raw"
	done
	rm -rf "$$stage/.uv-cache"
	chmod 0644 "$$stage/packages-cpu.lock" "$$stage/packages-gpu.lock"
	mv -f "$$stage/packages-cpu.lock" "$$locks_directory/packages-cpu.lock"
	mv -f "$$stage/packages-gpu.lock" "$$locks_directory/packages-gpu.lock"

packages/cpu/wheelhouse: requirements/packages-cpu.lock
	@$(MAKE) --no-print-directory packages/wheelhouse VARIANT=cpu

packages/gpu/wheelhouse: requirements/packages-gpu.lock
	@$(MAKE) --no-print-directory packages/wheelhouse VARIANT=gpu

packages/wheelhouse:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot materialize a wheelhouse as host UID 0.' >&2
		exit 2
	fi
	case "$(VARIANT)" in
		cpu) torch_wheels="$(PACKAGES_CPU_TORCH_WHEELS)" ;;
		gpu) torch_wheels="$(PACKAGES_GPU_TORCH_WHEELS)" ;;
		*) printf '%s\n' 'VARIANT must be cpu or gpu.' >&2; exit 2 ;;
	esac
	lock="$(REPOSITORY_ROOT)/requirements/packages-$(VARIANT).lock"
	wheelhouse_parent="$(REPOSITORY_ROOT)/wheelhouse"
	wheelhouse_path="$$wheelhouse_parent/packages-$(VARIANT)"
	mkdir -p "$$wheelhouse_parent"
	stage=$$(mktemp -d --tmpdir="$$wheelhouse_parent" .packages-$(VARIANT).XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	mkdir "$$stage/.tmp"
	# The lock resolves antlr4-python3-runtime 4.9.3, which PyPI publishes only as source.
	$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
		--pull never --pids-limit 64 --cpus 2 --memory 1g --memory-swap 1g \
		--env PIP_NO_CACHE_DIR=1 --env TMPDIR=/wheelhouse/.tmp \
		--mount type=bind,src="$$lock",dst=/input/requirements.lock,readonly \
		--mount type=bind,src="$$stage",dst=/wheelhouse \
		"$(PYTHON_BASE)" python -m pip download --require-hashes \
		--only-binary=:all: --no-binary=antlr4-python3-runtime \
		--no-deps --index-url https://pypi.org/simple --find-links "$$torch_wheels" \
		--dest /wheelhouse -r /input/requirements.lock
	rm -rf "$$stage/.tmp"
	( cd "$$stage" && sha256sum * | LC_ALL=C sort ) > "$$stage/.complete"
	rm -rf "$$wheelhouse_path"
	mv -T "$$stage" "$$wheelhouse_path"
	trap - EXIT

packages/cpu/image:
	@test -f wheelhouse/packages-cpu/.complete || { \
		printf '%s\n' 'CPU wheelhouse is absent; run make packages/cpu/wheelhouse.' >&2; exit 2; }
	$(MAKE) --no-print-directory packages/image VARIANT=cpu

packages/gpu/image:
	@test -f wheelhouse/packages-gpu/.complete || { \
		printf '%s\n' 'GPU wheelhouse is absent; run make packages/gpu/wheelhouse.' >&2; exit 2; }
	$(MAKE) --no-print-directory packages/image VARIANT=gpu

packages/image:
	@case "$(VARIANT)" in
		cpu|gpu) ;;
		*) printf '%s\n' 'VARIANT must be cpu or gpu.' >&2; exit 2 ;;
	esac
	id=$$( {
		printf '%s\n' "$(PYTHON_BASE)" "$(VARIANT)"
		cat containers/packages/Dockerfile.dockerignore \
			requirements/packages-$(VARIANT).lock containers/packages/Dockerfile \
			pyproject.toml secs/pyproject.toml secs/README.md wheelhouse/packages-$(VARIANT)/.complete
		find src -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
		find secs/src -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
	} | sha256sum | cut -d' ' -f1 )
	$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PYTHON_BASE)" \
		--build-arg PACKAGES_INPUT_ID="$$id" \
		--build-arg PACKAGES_VARIANT="$(VARIANT)" \
		--build-context "wheelhouse=$(REPOSITORY_ROOT)/wheelhouse/packages-$(VARIANT)" \
		--file containers/packages/Dockerfile \
		--tag "secs-repro/packages-$(VARIANT):inputs-$$id" \
		--tag "secs-repro/packages-$(VARIANT):local" .

packages/images: packages/cpu/image packages/gpu/image
