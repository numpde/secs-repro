PROVIDER_IMAGE_TAG := secs-repro/provider:local
PROVIDER_WHEELHOUSE := $(REPOSITORY_ROOT)/wheelhouse/provider

.PHONY: provider/lock/write provider/wheelhouse provider/image

provider/lock/write:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot write the provider lock as host UID 0.' >&2
		exit 2
	fi
	lock_image=$$($(MAKE) --no-print-directory packages/lock-image)
	locks_directory="$(REPOSITORY_ROOT)/requirements"
	stage=$$(mktemp -d --tmpdir="$$locks_directory" .provider-lock.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
		--pull never --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
		--env UV_CACHE_DIR=/output/.uv-cache \
		--mount type=bind,src="$$locks_directory/provider.in",dst=/input/provider.in,readonly \
		--mount type=bind,src="$$stage",dst=/output \
		"$$lock_image" /input/provider.in \
		--python-version "$(PACKAGES_PYTHON_VERSION)" \
		--python-platform x86_64-manylinux_2_28 \
		--generate-hashes --index-url https://pypi.org/simple \
		--output-file /output/provider.lock
	rm -rf "$$stage/.uv-cache"
	chmod 0644 "$$stage/provider.lock"
	mv -f "$$stage/provider.lock" "$$locks_directory/provider.lock"

provider/wheelhouse: requirements/provider.lock
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot materialize the provider wheelhouse as host UID 0.' >&2
		exit 2
	fi
	wheelhouse_parent="$(REPOSITORY_ROOT)/wheelhouse"
	mkdir -p "$$wheelhouse_parent"
	stage=$$(mktemp -d --tmpdir="$$wheelhouse_parent" .provider.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	mkdir "$$stage/.tmp"
	$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
		--pull never --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--env PIP_NO_CACHE_DIR=1 --env TMPDIR=/wheelhouse/.tmp \
		--mount type=bind,src="$(REPOSITORY_ROOT)/requirements/provider.lock",dst=/input/requirements.lock,readonly \
		--mount type=bind,src="$$stage",dst=/wheelhouse \
		"$(PYTHON_BASE)" python -m pip download --require-hashes \
		--only-binary=:all: --no-deps --index-url https://pypi.org/simple \
		--dest /wheelhouse -r /input/requirements.lock
	rm -rf "$$stage/.tmp"
	( cd "$$stage" && sha256sum * | LC_ALL=C sort ) > "$$stage/.complete"
	rm -rf "$(PROVIDER_WHEELHOUSE)"
	mv -T "$$stage" "$(PROVIDER_WHEELHOUSE)"
	trap - EXIT

provider/image:
	@test -f "$(PROVIDER_WHEELHOUSE)/.complete" || { \
		printf '%s\n' 'Provider wheelhouse is absent; run make provider/wheelhouse.' >&2; exit 2; }
	id=$$( {
		printf '%s\n' "$(PYTHON_BASE)"
		cat containers/provider/Dockerfile containers/provider/Dockerfile.dockerignore \
			requirements/provider.lock "$(PROVIDER_WHEELHOUSE)/.complete"
		sha256sum src/secs_inference/__init__.py
		find src/secs_inference/provider -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
	} | sha256sum | cut -d' ' -f1 )
	$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PYTHON_BASE)" \
		--build-arg PROVIDER_INPUT_ID="$$id" \
		--build-context "wheelhouse=$(PROVIDER_WHEELHOUSE)" \
		--file containers/provider/Dockerfile \
		--tag "secs-repro/provider:inputs-$$id" \
		--tag "$(PROVIDER_IMAGE_TAG)" .
