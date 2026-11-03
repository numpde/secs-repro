override PROVIDER_IMAGE_TAG := secs-repro/provider:local
override PROVIDER_PYTHON_BASE := docker.io/library/python:3.12.16-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487
override PROVIDER_CORE_WHEEL_URL := https://github.com/numpde/nmr-provider-tools/releases/download/v0.5.16/provider_core-0.5.16-py3-none-any.whl
override WHEELHOUSE_TOOL_NAME := wheelhouse-tool-0.5.14.pyz
override WHEELHOUSE_TOOL_SHA256 := 9d8cd59c0aeffd61ce462fa81f2951a24fb8a8749a1332d5f87842b4c095d656
override WHEELHOUSE_TOOL_URL := https://github.com/numpde/nmr-provider-tools/releases/download/v0.5.14/$(WHEELHOUSE_TOOL_NAME)

# Initialization is a host filesystem operation, not a build or API operation.
# Pass the literal name through the environment to keep shell syntax out of it.
.PHONY: provider/deployment/init provider/deployment/config provider/deployment/grant provider/deployment/up
.PHONY: provider/deployment/status provider/deployment/down provider/deployment/update provider/deployment/retire-api provider/logs test/deployment
.PHONY: test/deployment/e2e test/deployment/wheelhouse test/deployment/release-asset
DOCKER_MAKE_TARGETS += provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/status \
	provider/deployment/down provider/deployment/retire-api provider/logs test/deployment/e2e
DOCKER_MAKE_TARGETS += provider/deployment/update
provider/deployment/init provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/update provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/deployment/init: private export SECS_WORKER_TARGET := $(if $(filter command line,$(origin TARGET)),$(value TARGET),)
provider/deployment/init: private export SECS_USE_MOCK_INDEX := $(if $(filter command line,$(origin USE_MOCK_INDEX)),$(value USE_MOCK_INDEX),0)
provider/deployment/init: private export SECS_USE_MOCK_RUNNER := $(if $(filter command line,$(origin USE_MOCK_RUNNER)),$(value USE_MOCK_RUNNER),0)
provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/update: private export SECS_USE_MOCK_INDEX := $(if $(filter command line,$(origin USE_MOCK_INDEX)),$(value USE_MOCK_INDEX),)
provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/update: private export SECS_MOCK_FLAG_SUPPLIED := $(if $(filter command line,$(origin USE_MOCK_RUNNER)),1,0)
provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs: private export SECS_MOCK_FLAG_SUPPLIED := $(if $(filter command line,$(origin USE_MOCK_INDEX) $(origin USE_MOCK_RUNNER)),1,0)
provider/deployment/init provider/deployment/config provider/deployment/grant provider/deployment/up provider/deployment/status provider/deployment/down provider/deployment/retire-api provider/logs:
	@cd "$(REPOSITORY_ROOT)"
	if [[ "$${SECS_MOCK_FLAG_SUPPLIED:-0}" == 1 ]]; then printf '%s\n' 'USE_MOCK_RUNNER is selected during init; USE_MOCK_INDEX is accepted during init, config, grant or up.' >&2; exit 2; fi
	python3 -m deployment.provider_deployment "$(@F)" "$${DEPLOYMENT_INPUT}"
	if [[ "$(@F)" == up ]]; then
		$(DOCKER_MAKE) --no-print-directory packages/images/retire || printf '%s\n' 'Deployment started; image retirement was incomplete. Retry make packages/images/retire.' >&2
	fi

.PHONY: provider/credential/install provider/interpreter-key/install
provider/credential/install provider/interpreter-key/install: private export DEPLOYMENT_INPUT := $(value DEPLOYMENT)
provider/credential/install provider/interpreter-key/install: private export SECRET_SOURCE_INPUT := $(value SOURCE)
provider/credential/install provider/interpreter-key/install:
	@cd "$(REPOSITORY_ROOT)"
	python3 -m deployment.provider_deployment "$(word 2,$(subst /, ,$@))-install" "$${DEPLOYMENT_INPUT}" --source "$${SECRET_SOURCE_INPUT}"

.PHONY: provider/deployment/update
provider/deployment/update: private export SECS_UPDATE_WORKER := $(value UPDATE_WORKER)
provider/deployment/update:
	@source "$(REPOSITORY_ROOT)/tools/image_operation_lock.sh"
	hold_image_operation_lock "$(REPOSITORY_ROOT)"
	test "$(origin DEPLOYMENT)" = command\ line || { printf '%s\n' 'DEPLOYMENT must be set on the command line.' >&2; exit 2; }
	@if [[ "$${SECS_MOCK_FLAG_SUPPLIED:-0}" == 1 ]]; then printf '%s\n' 'USE_MOCK_RUNNER is selected during init; USE_MOCK_INDEX is accepted during init, config, grant, up or update.' >&2; exit 2; fi
	@cd "$(REPOSITORY_ROOT)"
	case "$$SECS_UPDATE_WORKER" in ''|cpu) ;; *) printf '%s\n' 'UPDATE_WORKER must be cpu or omitted.' >&2; exit 2 ;; esac
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	image_id=$$($(DOCKER_MAKE) --no-print-directory provider/image WHEELHOUSE="$$wheelhouse_directory")
	worker_args=()
	if [[ $$SECS_UPDATE_WORKER == cpu ]]; then
		worker_id=$$($(DOCKER_MAKE) --no-print-directory packages/cpu/image WHEELHOUSE="$$wheelhouse_directory")
		worker_args=(--worker-image-id "$$worker_id")
	fi
	python3 -m deployment.provider_deployment update "$${DEPLOYMENT_INPUT}" --provider-image-id "$$image_id" "$${worker_args[@]}"
	exec {secs_image_lock_fd}<&-
	$(DOCKER_MAKE) --no-print-directory packages/images/retire || printf '%s\n' 'Deployment updated; image retirement was incomplete. Retry make packages/images/retire.' >&2

test/deployment:
	@cd "$(REPOSITORY_ROOT)"
	python3 -m unittest discover -v -s tests/deployment -p 'test_*.py'

test/deployment/wheelhouse:
	@cd "$(REPOSITORY_ROOT)"
	python3 -m unittest discover -v -s tests/deployment -p 'test_shared_wheelhouse.py'

test/deployment/release-asset:
	@cd "$(REPOSITORY_ROOT)"
	python3 -m unittest discover -v -s tests/deployment -p 'test_release_asset.py'

.PHONY: test/deployment/e2e
test/deployment/e2e:
	@source "$(REPOSITORY_ROOT)/tools/image_operation_lock.sh"
	hold_image_operation_lock "$(REPOSITORY_ROOT)"
	@cd "$(REPOSITORY_ROOT)"
	image=$$($(DOCKER_MAKE) --no-print-directory provider/image)
	DEPLOYMENT_TEST_IMAGE="$$image" python3 -m unittest discover -v -s tests/deployment_e2e -p 'test_*.py'

.PHONY: provider/wheelhouse provider/native-wheelhouse provider/wheelhouse/tool provider/image
.PHONY: provider/support/check provider/support/write
DOCKER_MAKE_TARGETS += provider/wheelhouse provider/native-wheelhouse provider/wheelhouse/tool provider/image

provider/support/check:
	@python3 -B tools/project_support_catalog.py check docs/index.html
	@python3 -B tools/check_support_catalog_boundary.py src/secs_inference
	@python3 -B tools/check_secs_source_lock.py

provider/support/write:
	@python3 -B tools/project_support_catalog.py write docs/index.html

provider/wheelhouse/tool:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot acquire the wheelhouse tool as host UID 0.' >&2
		exit 2
	fi
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	if python3 -m tools.acquire_release_asset check "$$wheelhouse_directory" "$(WHEELHOUSE_TOOL_NAME)" "$(WHEELHOUSE_TOOL_SHA256)" "$(WHEELHOUSE_TOOL_URL)"; then
		exit 0
	else
		status=$$?
	fi
	if test "$$status" -ne 10; then exit "$$status"; fi
	identity=$$(python3 -m deployment.rootless_access "$(PROVIDER_PYTHON_BASE)")
	$(DOCKER) run --rm --network bridge --read-only --user "$$identity" \
		--cap-drop ALL --security-opt no-new-privileges:true --pull never \
		--pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools",dst=/workspace/tools,readonly \
		--mount type=bind,src="$$wheelhouse_directory",dst=/wheelhouse \
		--workdir /workspace --entrypoint python "$(PROVIDER_PYTHON_BASE)" \
		-I -B -c 'import sys; sys.path.insert(0, "/workspace"); from tools.acquire_release_asset import main; raise SystemExit(main())' \
		acquire /wheelhouse "$(WHEELHOUSE_TOOL_NAME)" "$(WHEELHOUSE_TOOL_SHA256)" "$(WHEELHOUSE_TOOL_URL)"

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
	mkdir "$$stage/check"
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	cp "requirements/$$profile.lock" "$$stage/requirements.lock"
	lock_wheelhouse -s
	if stage_selected_wheels "$$wheelhouse_directory" "$$stage/requirements.lock" "$$stage/check" "$(PROVIDER_PYTHON_BASE)" check; then
		unlock_wheelhouse
		exit 0
	else
		selection_status=$$?
	fi
	unlock_wheelhouse
	if test "$$selection_status" -ne 10; then exit "$$selection_status"; fi
	printf '%s\n' 'Shared wheelhouse is missing an SDK artifact; locating locked sources.' >&2
	checksum=$$(awk '/^provider-core==/ {getline; sub(/^ *--hash=sha256:/, ""); print; exit}' "$$stage/requirements.lock")
	[[ $$checksum =~ ^[0-9a-f]{64}$$ ]] || { printf '%s\n' 'SDK lock must contain the pinned provider-core hash.' >&2; exit 2; }
	source_url="$(PROVIDER_CORE_WHEEL_URL)#sha256=$$checksum"
	source tools/network_interface.sh
	select_network_interface
	export SECS_NETWORK_INTERFACE_INPUT="$$SECS_SELECTED_INTERFACE" SECS_NETWORK_INTERFACE_ORIGIN='command line'
	$(DOCKER_MAKE) --no-print-directory provider/wheelhouse/tool WHEELHOUSE="$$wheelhouse_directory" NETWORK_INTERFACE="$$SECS_SELECTED_INTERFACE"
	identity=$$(python3 -m deployment.rootless_access "$(PROVIDER_PYTHON_BASE)")
	key=$$( { sha256sum "$$stage/requirements.lock" | cut -d' ' -f1; printf '%s\n' "$$profile" "$(PROVIDER_PYTHON_BASE)" "$(WHEELHOUSE_TOOL_SHA256)" 'https://pypi.org/simple' "$$source_url"; } | sha256sum | cut -d' ' -f1 )
	mkdir -p "$$wheelhouse_directory/.secs-pylocks"
	exec {metadata_fd}<"$$wheelhouse_directory/.secs-pylocks"
	flock -x "$$metadata_fd"
	metadata="$$wheelhouse_directory/.secs-pylocks/pylock.sdk-$$key.toml"
	if [[ ! -e $$metadata && ! -L $$metadata ]]; then
		secs_engine_active=1
		$(DOCKER) run --rm --network bridge --read-only --user "$$identity" --pull never \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
			--mount type=bind,src="$$wheelhouse_directory",dst=/pool,readonly \
			--mount type=bind,src="$$stage/requirements.lock",dst=/input/requirements.lock,readonly \
			--entrypoint python "$(PROVIDER_PYTHON_BASE)" -I -S "/pool/$(WHEELHOUSE_TOOL_NAME)" locate \
			--lock /input/requirements.lock --pool /pool --index-url https://pypi.org/simple --source "$$source_url" > "$$stage/generated.toml"
		secs_engine_active=0
		ln -T -- "$$stage/generated.toml" "$$metadata"
	fi
	cp --no-dereference -- "$$metadata" "$$stage/pylock.toml"
	test -f "$$stage/pylock.toml" && test ! -L "$$stage/pylock.toml" || { printf '%s\n' 'Cached SDK projection must be a regular file.' >&2; exit 2; }
	exec {metadata_fd}<&-
	secs_engine_active=1
	$(DOCKER) run --rm --network bridge --read-only --user "$$identity" --pull never \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--mount type=bind,src="$$wheelhouse_directory",dst=/pool \
		--mount type=bind,src="$$stage",dst=/input,readonly \
		--entrypoint python "$(PROVIDER_PYTHON_BASE)" -I -S "/pool/$(WHEELHOUSE_TOOL_NAME)" acquire \
		--lock /input/pylock.toml --source-lock /input/requirements.lock --pool /pool > "$$stage/acquired.json"
	secs_engine_active=0
	mkdir "$$stage/recheck"
	lock_wheelhouse -s
	if stage_selected_wheels "$$wheelhouse_directory" "$$stage/requirements.lock" "$$stage/recheck" "$(PROVIDER_PYTHON_BASE)" check; then
		unlock_wheelhouse
	else
		status=$$?
		unlock_wheelhouse
		printf '%s\n' 'Cannot complete SDK acquisition: target pip did not select the acquired pool.' >&2
		exit "$$status"
	fi
	printf '%s\n' 'Selected SDK artifacts are available in the shared wheelhouse.'

provider/image:
	@source "$(REPOSITORY_ROOT)/tools/image_operation_lock.sh"
	hold_image_operation_lock "$(REPOSITORY_ROOT)"
	source tools/wheelhouse_selection.sh
	select_wheelhouse
	$(DOCKER_MAKE) --no-print-directory provider/wheelhouse/tool WHEELHOUSE="$$wheelhouse_directory"
	lock_wheelhouse -s
	stage=$$(mktemp -d /tmp/secs-provider-inputs.XXXXXXXX)
	source tools/retain_uncertain_stage.sh
	secs_engine_active=0
	trap 'retain_uncertain_stage "$$stage"' EXIT
	trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
	cp requirements/provider.lock "$$stage/provider.lock"
	stage_selected_wheels "$$wheelhouse_directory" "$$stage/provider.lock" "$$stage" "$(PROVIDER_PYTHON_BASE)" report
	tool="$$wheelhouse_directory/$(WHEELHOUSE_TOOL_NAME)"
	python3 -I -S "$$tool" report --report "$$stage/.pip-report" --pool /pool --directory provider_wheels > "$$stage/manifest.json"
	unlock_wheelhouse
	source_id=$$(python3 tools/provider_stream_inputs.py "$$stage" "$(REPOSITORY_ROOT)")
	wheel_id=$$(python3 -I -S "$$tool" identity --manifest "$$stage/manifest.json")
	id=$$(printf '%s\n' "$(PROVIDER_PYTHON_BASE)" "$(WHEELHOUSE_TOOL_SHA256)" "$$source_id" "$$wheel_id" | sha256sum | cut -d' ' -f1)
	secs_engine_active=1
	if python3 -I -S "$$tool" stream --pool "$$wheelhouse_directory" --source-allowlist "$$stage/sources.json" --manifest "$$stage/manifest.json" | \
	$(DOCKER) build --quiet --network none --pull=false \
		--build-arg PYTHON_BASE="$(PROVIDER_PYTHON_BASE)" \
		--build-arg PROVIDER_INPUT_ID="$$id" \
		--iidfile "$$stage/image-id" --file containers/provider/Dockerfile - > /dev/null; then
		:
	else
		printf '%s\n' 'Provider stream or image build failed; no image tag was published.' >&2
		exit 1
	fi
	secs_engine_active=0
	image=$$(cat "$$stage/image-id")
	[[ $$image =~ ^sha256:[0-9a-f]{64}$$ ]] || { printf '%s\n' 'Docker returned an invalid provider image ID.' >&2; exit 1; }
	$(DOCKER) tag "$$image" "secs-repro/provider:inputs-$$id"
	$(DOCKER) tag "$$image" "$(PROVIDER_IMAGE_TAG)"
	printf '%s\n' "$$image"
	rm -rf "$$stage"
	trap - EXIT
