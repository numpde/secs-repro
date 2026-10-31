PROVIDER_IMAGE_TAG := secs-repro/provider:local
override PROVIDER_PYTHON_BASE := docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487
override PROVIDER_CORE_WHEEL_URL := https://github.com/numpde/nmr-provider-tools/releases/download/v0.5.4/provider_core-0.5.4-py3-none-any.whl

# Initialization is a host filesystem operation, not a build or API operation.
# Pass the literal name through the environment to keep shell syntax out of it.
.PHONY: provider/deployment/init provider/deployment/config provider/deployment/grant provider/deployment/up
.PHONY: provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs test/deployment
.PHONY: test/deployment/e2e
DOCKER_MAKE_TARGETS += provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/status \
	provider/deployment/down provider/deployment/retire-api provider/logs test/deployment/e2e
provider/deployment/init provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/init: private export SECS_WORKER_TARGET := $(if $(filter command line,$(origin TARGET)),$(value TARGET),)
provider/deployment/init: private export SECS_USE_MOCK_INDEX := $(if $(filter command line,$(origin USE_MOCK_INDEX)),$(value USE_MOCK_INDEX),0)
provider/deployment/init: private export SECS_USE_MOCK_RUNNER := $(if $(filter command line,$(origin USE_MOCK_RUNNER)),$(value USE_MOCK_RUNNER),0)
provider/deployment/config provider/deployment/grant provider/deployment/up: private export SECS_USE_MOCK_INDEX := $(if $(filter command line,$(origin USE_MOCK_INDEX)),$(value USE_MOCK_INDEX),)
provider/deployment/config provider/deployment/grant provider/deployment/up: private export SECS_MOCK_FLAG_SUPPLIED := $(if $(filter command line,$(origin USE_MOCK_RUNNER)),1,0)
provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs: private export SECS_MOCK_FLAG_SUPPLIED := $(if $(filter command line,$(origin USE_MOCK_INDEX) $(origin USE_MOCK_RUNNER)),1,0)
provider/deployment/init provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs:
	@cd "$(REPOSITORY_ROOT)"
	if [[ "$${SECS_MOCK_FLAG_SUPPLIED:-0}" == 1 ]]; then printf '%s\n' 'USE_MOCK_RUNNER is selected during init; USE_MOCK_INDEX is accepted during init, config, grant or up.' >&2; exit 2; fi
	python3 -m deployment.provider_deployment "$(@F)" "$${DEPLOYMENT_INPUT}"

.PHONY: provider/credential/install provider/interpreter-key/install
provider/credential/install provider/interpreter-key/install: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/credential/install provider/interpreter-key/install: private export SECRET_SOURCE_INPUT := $(value SOURCE)
provider/credential/install provider/interpreter-key/install:
	@cd "$(REPOSITORY_ROOT)"
	python3 -m deployment.provider_deployment "$(word 2,$(subst /, ,$@))-install" "$${DEPLOYMENT_INPUT}" --source "$${SECRET_SOURCE_INPUT}"

test/deployment:
	@cd "$(REPOSITORY_ROOT)"
	python3 -m unittest discover -v -s tests/deployment -p 'test_*.py'

.PHONY: test/deployment/e2e
test/deployment/e2e:
	@cd "$(REPOSITORY_ROOT)"
	image=$$($(DOCKER_MAKE) --no-print-directory provider/image)
	DEPLOYMENT_TEST_IMAGE="$$image" python3 -m unittest discover -v -s tests/deployment_e2e -p 'test_*.py'

.PHONY: provider/wheelhouse provider/native-wheelhouse provider/image
.PHONY: provider/support/check provider/support/write
DOCKER_MAKE_TARGETS += provider/wheelhouse provider/native-wheelhouse provider/image

provider/support/check:
	@python3 -B tools/project_support_catalog.py check docs/index.html
	@python3 -B tools/check_support_catalog_boundary.py src/secs_inference
	@python3 -B tools/check_secs_source_lock.py

provider/support/write:
	@python3 -B tools/project_support_catalog.py write docs/index.html

# Both install profiles are exact issuer locks from the selected SDK release.
provider/wheelhouse provider/native-wheelhouse: requirements/provider.lock requirements/native.lock
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot materialize SDK wheelhouses as host UID 0.' >&2
		exit 2
	fi
	profile="$(if $(filter provider/native-wheelhouse,$@),native,provider)"
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	stage=$$(mktemp -d --tmpdir="$$wheelhouse_directory" .secs-$$profile.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	if stage_selected_wheels "$$wheelhouse_directory" "$(REPOSITORY_ROOT)/requirements/$$profile.lock" "$$stage" "$(PROVIDER_PYTHON_BASE)" check; then
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
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
		--pull never --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--env PIP_NO_CACHE_DIR=1 --env TMPDIR=/wheelhouse/.tmp \
		--mount type=bind,src="$(REPOSITORY_ROOT)/requirements/$$profile.lock",dst=/input/requirements.lock,readonly \
		--mount type=bind,src="$$wheelhouse_directory",dst=/pool,readonly \
		--mount type=bind,src="$$stage",dst=/wheelhouse \
		"$(PROVIDER_PYTHON_BASE)" python -m pip download --require-hashes \
		--only-binary=:all: --no-deps --index-url https://pypi.org/simple \
		--find-links /pool --find-links "$(PROVIDER_CORE_WHEEL_URL)" --dest /wheelhouse -r /input/requirements.lock
	secs_engine_active=0
	rm -rf "$$stage/.tmp"
	python3 tools/publish_shared_wheels.py "$$stage" "$$wheelhouse_directory"
	rm -rf "$$stage"
	trap - EXIT

provider/image:
	@source tools/wheelhouse_selection.sh
	select_wheelhouse
	stage=$$(mktemp -d /tmp/secs-provider-wheels.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=1
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	stage_selected_wheels "$$wheelhouse_directory" "$(REPOSITORY_ROOT)/requirements/provider.lock" "$$stage" "$(PROVIDER_PYTHON_BASE)"
	id=$$( {
		printf '%s\n' "$(PROVIDER_PYTHON_BASE)"
		cat containers/provider/Dockerfile containers/provider/Dockerfile.dockerignore \
			requirements/provider.lock "$$stage/.complete"
		sha256sum src/secs_inference/__init__.py src/secs_inference/model_failure.py
		find src/secs_inference/provider -type f \
			! -path '*/__pycache__/*' ! -name '*.py[co]' -print0 \
			| LC_ALL=C sort -z | xargs -0 sha256sum
	} | sha256sum | cut -d' ' -f1 )
	secs_engine_active=1
	$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PROVIDER_PYTHON_BASE)" \
		--build-arg PROVIDER_INPUT_ID="$$id" \
		--build-context "wheelhouse=$$stage" \
		--file containers/provider/Dockerfile \
		--tag "secs-repro/provider:inputs-$$id" \
		--tag "$(PROVIDER_IMAGE_TAG)" .
	secs_engine_active=0
	rm -rf "$$stage"
	trap - EXIT
