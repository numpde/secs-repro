PROVIDER_IMAGE_TAG := secs-repro/provider:local
PROVIDER_WHEELHOUSE := $(REPOSITORY_ROOT)/wheelhouse/provider
NATIVE_WHEELHOUSE := $(REPOSITORY_ROOT)/wheelhouse/native
override PROVIDER_PYTHON_BASE := docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487
override PROVIDER_CORE_WHEEL_URL := https://github.com/numpde/nmr-provider-tools/releases/download/v0.5.2/provider_core-0.5.2-py3-none-any.whl

# Initialization is a host filesystem operation, not a build or API operation.
# Pass the literal name through the environment to keep shell syntax out of it.
.PHONY: provider/deployment/init provider/deployment/config provider/deployment/up
.PHONY: provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs test/deployment
provider/deployment/init provider/deployment/config provider/deployment/up provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/init provider/deployment/config provider/deployment/up provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs:
	@cd "$(REPOSITORY_ROOT)"
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
	image=$$($(MAKE) --no-print-directory provider/image)
	DEPLOYMENT_TEST_IMAGE="$$image" python3 -m unittest discover -v -s tests/deployment_e2e -p 'test_*.py'

.PHONY: provider/wheelhouse provider/native-wheelhouse provider/image
.PHONY: provider/support/check provider/support/write

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
	wheelhouse_parent="$(REPOSITORY_ROOT)/wheelhouse"
	mkdir -p "$$wheelhouse_parent"
	stage=$$(mktemp -d --tmpdir="$$wheelhouse_parent" .$$profile.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	mkdir "$$stage/.tmp"
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --user "$(HOST_UID):$(HOST_GID)" \
		--pull never --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--env PIP_NO_CACHE_DIR=1 --env TMPDIR=/wheelhouse/.tmp \
		--mount type=bind,src="$(REPOSITORY_ROOT)/requirements/$$profile.lock",dst=/input/requirements.lock,readonly \
		--mount type=bind,src="$$stage",dst=/wheelhouse \
		"$(PROVIDER_PYTHON_BASE)" python -m pip download --require-hashes \
		--only-binary=:all: --no-deps --index-url https://pypi.org/simple \
		--find-links "$(PROVIDER_CORE_WHEEL_URL)" --dest /wheelhouse -r /input/requirements.lock
	secs_engine_active=0
	rm -rf "$$stage/.tmp"
	( cd "$$stage" && sha256sum * | LC_ALL=C sort ) > "$$stage/.complete"
	python3 tools/publish_wheelhouse.py "$$stage" "$$wheelhouse_parent/$$profile"
	rm -rf "$$stage"
	trap - EXIT

provider/image:
	@test -f "$(PROVIDER_WHEELHOUSE)/.complete" || { \
		printf '%s\n' 'Provider wheelhouse is absent; run make provider/wheelhouse.' >&2; exit 2; }
	id=$$( {
		printf '%s\n' "$(PROVIDER_PYTHON_BASE)"
		cat containers/provider/Dockerfile containers/provider/Dockerfile.dockerignore \
			requirements/provider.lock "$(PROVIDER_WHEELHOUSE)/.complete"
		sha256sum src/secs_inference/__init__.py src/secs_inference/model_failure.py
		find src/secs_inference/provider -type f \
			! -path '*/__pycache__/*' ! -name '*.py[co]' -print0 \
			| LC_ALL=C sort -z | xargs -0 sha256sum
	} | sha256sum | cut -d' ' -f1 )
	$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PROVIDER_PYTHON_BASE)" \
		--build-arg PROVIDER_INPUT_ID="$$id" \
		--build-context "wheelhouse=$(PROVIDER_WHEELHOUSE)" \
		--file containers/provider/Dockerfile \
		--tag "secs-repro/provider:inputs-$$id" \
		--tag "$(PROVIDER_IMAGE_TAG)" .
