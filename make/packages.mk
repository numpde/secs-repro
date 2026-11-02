override PACKAGES_PYTHON_VERSION := 3.12.12
override PYTHON_BASE := docker.io/library/python:$(PACKAGES_PYTHON_VERSION)-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
override UV_IMAGE := ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b
PACKAGES_CPU_TORCH_WHEELS := https://download.pytorch.org/whl/cpu/torch/
PACKAGES_GPU_TORCH_WHEELS := https://download.pytorch.org/whl/cu130/torch/
PACKAGES_LOCK_IMAGE_TAG := secs-repro/packages-lock
packages_image_tag = secs-repro/packages-$(1):local

.PHONY: packages/base-images/pull packages/lock-image packages/locks/write
.PHONY: packages/cpu/wheelhouse packages/gpu/wheelhouse packages/wheelhouse
.PHONY: packages/cpu/image packages/gpu/image packages/image packages/images
DOCKER_MAKE_TARGETS += packages/base-images/pull packages/lock-image packages/locks/write \
	packages/cpu/wheelhouse packages/gpu/wheelhouse packages/wheelhouse \
	packages/cpu/image packages/gpu/image packages/image packages/images

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
	lock_image=$$($(DOCKER_MAKE) --no-print-directory packages/lock-image)
	locks_directory="$(REPOSITORY_ROOT)/requirements"
	stage=$$(mktemp -d --tmpdir="$$locks_directory" .packages-locks.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	for variant in cpu gpu; do
		case "$$variant" in
			cpu) group=packages-cpu ;;
			gpu) group=packages-gpu ;;
		esac
		secs_engine_active=1
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
		secs_engine_active=0
		rm "$$stage/packages-$$variant.raw"
	done
	rm -rf "$$stage/.uv-cache"
	chmod 0644 "$$stage/packages-cpu.lock" "$$stage/packages-gpu.lock"
	mv -f "$$stage/packages-cpu.lock" "$$locks_directory/packages-cpu.lock"
	mv -f "$$stage/packages-gpu.lock" "$$locks_directory/packages-gpu.lock"

packages/cpu/wheelhouse: requirements/packages-cpu.lock
	@$(DOCKER_MAKE) --no-print-directory packages/wheelhouse VARIANT=cpu

packages/gpu/wheelhouse: requirements/packages-gpu.lock
	@$(DOCKER_MAKE) --no-print-directory packages/wheelhouse VARIANT=gpu

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
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	lock_wheelhouse -x
	stage=$$(mktemp -d --tmpdir="$$wheelhouse_directory" .secs-packages-$(VARIANT).XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	if stage_selected_wheels "$$wheelhouse_directory" "$$lock" "$$stage" "$(PYTHON_BASE)" check; then
		rm -rf "$$stage"
		trap - EXIT
		exit 0
	else
		selection_status=$$?
	fi
	if test "$$selection_status" -ne 10; then exit "$$selection_status"; fi
	secs_engine_active=0
	find "$$stage" -mindepth 1 -delete
	python3 tools/link_cached_wheels.py "$$wheelhouse_directory" "$$stage"
	mkdir "$$stage/.tmp"
	# The lock resolves antlr4-python3-runtime 4.9.3, which PyPI publishes only as source.
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
		--pull never --pids-limit 64 --cpus 2 --memory 1g --memory-swap 1g \
		--env PIP_NO_CACHE_DIR=1 --env TMPDIR=/wheelhouse/.tmp \
		--mount type=bind,src="$$lock",dst=/input/requirements.lock,readonly \
		--mount type=bind,src="$$wheelhouse_directory",dst=/pool,readonly \
		--mount type=bind,src="$$stage",dst=/wheelhouse \
		"$(PYTHON_BASE)" python -m pip download --require-hashes \
		--only-binary=:all: --no-binary=antlr4-python3-runtime \
		--no-deps --index-url https://pypi.org/simple --find-links /pool --find-links "$$torch_wheels" \
		--dest /wheelhouse -r /input/requirements.lock
	secs_engine_active=0
	rm -rf "$$stage/.tmp"
	python3 tools/publish_shared_wheels.py "$$stage" "$$wheelhouse_directory"
	rm -rf "$$stage"
	trap - EXIT

packages/cpu/image:
	@$(DOCKER_MAKE) --no-print-directory packages/image VARIANT=cpu

packages/gpu/image:
	@$(DOCKER_MAKE) --no-print-directory packages/image VARIANT=gpu

packages/image:
	@case "$(VARIANT)" in
		cpu|gpu) ;;
		*) printf '%s\n' 'VARIANT must be cpu or gpu.' >&2; exit 2 ;;
	esac
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	$(DOCKER_MAKE) --no-print-directory provider/wheelhouse/tool WHEELHOUSE="$$wheelhouse_directory"
	lock_wheelhouse -s
	stage=$$(mktemp -d /tmp/secs-package-inputs.XXXXXXXX)
	mkdir "$$stage/packages" "$$stage/native"
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	cp "requirements/packages-$(VARIANT).lock" "$$stage/packages.lock"
	cp requirements/native.lock "$$stage/native.lock"
	stage_selected_wheels "$$wheelhouse_directory" "$$stage/packages.lock" "$$stage/packages" "$(PYTHON_BASE)" report
	stage_selected_wheels "$$wheelhouse_directory" "$$stage/native.lock" "$$stage/native" "$(PYTHON_BASE)" report
	tool="$$wheelhouse_directory/$(WHEELHOUSE_TOOL_NAME)"
	python3 -I -S "$$tool" report --report "$$stage/packages/.pip-report" --pool /pool --directory packages_wheels --allow-source antlr4-python3-runtime-4.9.3.tar.gz > "$$stage/packages.json"
	python3 -I -S "$$tool" report --report "$$stage/native/.pip-report" --pool /pool --directory native_wheels > "$$stage/native.json"
	unlock_wheelhouse
	source_id=$$(python3 tools/packages_stream_inputs.py "$$stage" "$(REPOSITORY_ROOT)" "$(VARIANT)")
	wheel_id=$$(python3 -I -S "$$tool" identity --manifest "$$stage/manifest.json")
	id=$$(printf '%s\n' "$(PYTHON_BASE)" "$(VARIANT)" "$(WHEELHOUSE_TOOL_SHA256)" "$$source_id" "$$wheel_id" | sha256sum | cut -d' ' -f1)
	secs_engine_active=1
	if python3 -I -S "$$tool" stream --pool "$$wheelhouse_directory" --source-allowlist "$$stage/sources.json" --manifest "$$stage/manifest.json" | \
	$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PYTHON_BASE)" \
		--build-arg PACKAGES_INPUT_ID="$$id" \
		--build-arg PACKAGES_VARIANT="$(VARIANT)" \
		--iidfile "$$stage/image-id" --file containers/packages/Dockerfile - > /dev/null; then
		:
	else
		printf '%s\n' 'Package stream or image build failed; no image tag was published.' >&2
		exit 1
	fi
	secs_engine_active=0
	image=$$(cat "$$stage/image-id")
	[[ $$image =~ ^sha256:[0-9a-f]{64}$$ ]] || { printf '%s\n' 'Docker returned an invalid package image ID.' >&2; exit 1; }
	$(DOCKER) tag "$$image" "secs-repro/packages-$(VARIANT):inputs-$$id"
	$(DOCKER) tag "$$image" "$(call packages_image_tag,$(VARIANT))"
	printf '%s\n' "$$image"
	rm -rf "$$stage"
	trap - EXIT

packages/images: packages/cpu/image packages/gpu/image
