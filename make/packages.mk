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
.PHONY: packages/pylock/locate packages/pylock/acquire
DOCKER_MAKE_TARGETS += packages/base-images/pull packages/lock-image packages/locks/write \
	packages/cpu/wheelhouse packages/gpu/wheelhouse packages/wheelhouse \
	packages/cpu/image packages/gpu/image packages/image packages/images packages/pylock/locate packages/pylock/acquire

packages/pylock/locate packages/pylock/acquire: private export SECS_PYLOCK_VARIANT := $(value VARIANT)
packages/pylock/locate: private export SECS_PYLOCK_OUTPUT := $(value OUTPUT)
packages/pylock/locate:
	@case "$$SECS_PYLOCK_VARIANT" in cpu|gpu) ;; *) printf '%s\n' 'VARIANT must be cpu or gpu.' >&2; exit 2 ;; esac
	test "$(origin OUTPUT)" = 'command line' && test -n "$$SECS_PYLOCK_OUTPUT" || { printf '%s\n' 'Set OUTPUT to a new projected lock path.' >&2; exit 2; }
	output_name=$$(basename -- "$$SECS_PYLOCK_OUTPUT")
	[[ $$output_name != . && $$output_name != .. && $$output_name != / ]] || { printf '%s\n' 'OUTPUT must name a new file.' >&2; exit 2; }
	output_parent=$$(realpath -e -- "$$(dirname -- "$$SECS_PYLOCK_OUTPUT")")
	[[ -d $$output_parent && $$output_parent != *','* && $$output_parent != *$$'\n'* && $$output_parent != *$$'\r'* ]] || { printf '%s\n' 'OUTPUT parent must be a directory without commas or line breaks.' >&2; exit 2; }
	output="$$output_parent/$$output_name"
	test ! -e "$$output" && test ! -L "$$output" || { printf '%s\n' 'Projected lock already exists; preserve it and choose a new OUTPUT.' >&2; exit 2; }
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	$(DOCKER_MAKE) --no-print-directory provider/wheelhouse/tool WHEELHOUSE="$$wheelhouse_directory"
	stage=$$(mktemp -d --tmpdir="$$(dirname -- "$$output")" .secs-pylock.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	cp "requirements/packages-$$SECS_PYLOCK_VARIANT.lock" "$$stage/requirements.lock"
	source_url=$$(python3 -c 'import sys,tomllib; p=tomllib.load(open("secs/pyproject.toml","rb")); print(p["dependency-groups"]["packages-"+sys.argv[1]][0].split(" @ ",1)[1])' "$$SECS_PYLOCK_VARIANT")
	identity=$$(python3 -m deployment.rootless_access "$(PYTHON_BASE)")
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --user "$$identity" --pull never \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--mount type=bind,src="$$wheelhouse_directory",dst=/pool,readonly \
		--mount type=bind,src="$$stage/requirements.lock",dst=/input/requirements.lock,readonly \
		--entrypoint python "$(PYTHON_BASE)" -I -S "/pool/$(WHEELHOUSE_TOOL_NAME)" locate \
		--lock /input/requirements.lock --index-url https://pypi.org/simple \
		--source "$$source_url" --require-source antlr4-python3-runtime > "$$stage/pylock.toml"
	secs_engine_active=0
	ln -T -- "$$stage/pylock.toml" "$$output"
	printf 'Projected lock written to %s\n' "$$output" >&2

packages/pylock/acquire: private export SECS_PYLOCK_INPUT := $(value PYLOCK)
packages/pylock/acquire:
	@case "$$SECS_PYLOCK_VARIANT" in cpu|gpu) ;; *) printf '%s\n' 'VARIANT must be cpu or gpu.' >&2; exit 2 ;; esac
	test "$(origin PYLOCK)" = 'command line' && test -n "$$SECS_PYLOCK_INPUT" || { printf '%s\n' 'Set PYLOCK to the projected lock.' >&2; exit 2; }
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	$(DOCKER_MAKE) --no-print-directory provider/wheelhouse/tool WHEELHOUSE="$$wheelhouse_directory"
	stage=$$(mktemp -d /tmp/secs-pylock-acquire.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	cp -- "$$SECS_PYLOCK_INPUT" "$$stage/pylock.toml"
	cp "requirements/packages-$$SECS_PYLOCK_VARIANT.lock" "$$stage/requirements.lock"
	identity=$$(python3 -m deployment.rootless_access "$(PYTHON_BASE)")
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --user "$$identity" --pull never \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--mount type=bind,src="$$wheelhouse_directory",dst=/pool \
		--mount type=bind,src="$$stage",dst=/input,readonly \
		--entrypoint python "$(PYTHON_BASE)" -I -S "/pool/$(WHEELHOUSE_TOOL_NAME)" acquire \
		--lock /input/pylock.toml --source-lock /input/requirements.lock --pool /pool \
		--require-sdist antlr4-python3-runtime
	secs_engine_active=0

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

packages/wheelhouse: private export SECS_PACKAGES_VARIANT := $(value VARIANT)
packages/wheelhouse:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot materialize a wheelhouse as host UID 0.' >&2
		exit 2
	fi
	case "$$SECS_PACKAGES_VARIANT" in
		cpu|gpu) ;;
		*) printf '%s\n' 'VARIANT must be cpu or gpu.' >&2; exit 2 ;;
	esac
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	stage=$$(mktemp -d --tmpdir="$$wheelhouse_directory" .secs-packages-inputs.XXXXXXXX)
	mkdir "$$stage/check"
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	cp "requirements/packages-$$SECS_PACKAGES_VARIANT.lock" "$$stage/requirements.lock"
	lock_wheelhouse -s
	if stage_selected_wheels "$$wheelhouse_directory" "$$stage/requirements.lock" "$$stage/check" "$(PYTHON_BASE)" check; then
		unlock_wheelhouse
		exit 0
	else
		selection_status=$$?
	fi
	unlock_wheelhouse
	if test "$$selection_status" -ne 10; then exit "$$selection_status"; fi
	printf '%s\n' 'Shared wheelhouse is missing a package artifact; locating locked sources.' >&2
	source_url=$$(python3 -c 'import sys,tomllib; p=tomllib.load(open("secs/pyproject.toml","rb")); print(p["dependency-groups"]["packages-"+sys.argv[1]][0].split(" @ ",1)[1])' "$$SECS_PACKAGES_VARIANT")
	source tools/network_interface.sh
	select_network_interface
	export SECS_NETWORK_INTERFACE_INPUT="$$SECS_SELECTED_INTERFACE" SECS_NETWORK_INTERFACE_ORIGIN='command line'
	$(DOCKER_MAKE) --no-print-directory provider/wheelhouse/tool WHEELHOUSE="$$wheelhouse_directory" NETWORK_INTERFACE="$$SECS_SELECTED_INTERFACE"
	identity=$$(python3 -m deployment.rootless_access "$(PYTHON_BASE)")
	key=$$( { sha256sum "$$stage/requirements.lock" | cut -d' ' -f1; printf '%s\n' "$$SECS_PACKAGES_VARIANT" "$(PYTHON_BASE)" "$(WHEELHOUSE_TOOL_SHA256)" 'https://pypi.org/simple' "$$source_url" 'antlr4-python3-runtime'; } | sha256sum | cut -d' ' -f1 )
	mkdir -p "$$wheelhouse_directory/.secs-pylocks"
	exec {metadata_fd}<"$$wheelhouse_directory/.secs-pylocks"
	flock -x "$$metadata_fd"
	metadata="$$wheelhouse_directory/.secs-pylocks/pylock.packages-$$key.toml"
	if [[ ! -e $$metadata && ! -L $$metadata ]]; then
		secs_engine_active=1
		$(DOCKER) run --rm --network bridge --read-only --user "$$identity" --pull never \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
			--mount type=bind,src="$$wheelhouse_directory",dst=/pool,readonly \
			--mount type=bind,src="$$stage/requirements.lock",dst=/input/requirements.lock,readonly \
			--entrypoint python "$(PYTHON_BASE)" -I -S "/pool/$(WHEELHOUSE_TOOL_NAME)" locate \
			--lock /input/requirements.lock --index-url https://pypi.org/simple \
			--source "$$source_url" --require-source antlr4-python3-runtime > "$$stage/generated.toml"
		secs_engine_active=0
		ln -T -- "$$stage/generated.toml" "$$metadata"
	fi
	cp --no-dereference -- "$$metadata" "$$stage/pylock.toml"
	test -f "$$stage/pylock.toml" && test ! -L "$$stage/pylock.toml" || { printf '%s\n' 'Cached package projection must be a regular file.' >&2; exit 2; }
	exec {metadata_fd}<&-
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --user "$$identity" --pull never \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--mount type=bind,src="$$wheelhouse_directory",dst=/pool \
		--mount type=bind,src="$$stage",dst=/input,readonly \
		--entrypoint python "$(PYTHON_BASE)" -I -S "/pool/$(WHEELHOUSE_TOOL_NAME)" acquire \
		--lock /input/pylock.toml --source-lock /input/requirements.lock --pool /pool \
		--require-sdist antlr4-python3-runtime > "$$stage/acquired.json"
	secs_engine_active=0
	printf '%s\n' 'Selected package artifacts are available in the shared wheelhouse.'

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
