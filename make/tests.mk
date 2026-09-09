.PHONY: test/integration test/integration/challenges
.PHONY: test/integration/challenges/bruker
.PHONY: test/integration/bruker-reference test/provider
.PHONY: test/integration/jcamp-reference test/qualification-tools

test/integration:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot run integration tests as host UID 0.' >&2
		exit 2
	fi
	cache_dir=$$(realpath -e "$(MOLFORMER_CACHE)")
	checkpoint_dir=$$(realpath -e "$(CHECKPOINT_DIRECTORY)")
	tests_dir=$$(realpath -e tests/integration)
	fixtures_dir=$$(realpath -e tests/fixtures)
	cpu_packages_image=$$($(MAKE) --no-print-directory packages/cpu/image)
	# Hash verification gates cached Python imports.
	# Docker owns network denial; Transformers offline mode only makes cache misses fail promptly.
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 3g --memory-swap 3g \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
		--tmpfs /modules:rw,nosuid,nodev,noexec,size=16m,mode=1777 \
		--env HF_HUB_CACHE=/cache/hub \
		--env TRANSFORMERS_OFFLINE=1 \
		--env HF_MODULES_CACHE=/modules \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--mount type=bind,src="$(MOLFORMER_LOCK)",dst=/input/molformer.lock.toml,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
		--mount type=bind,src="$$cache_dir",dst=/cache,readonly \
		--mount type=bind,src="$$checkpoint_dir",dst=/checkpoint,readonly \
		--mount type=bind,src="$$fixtures_dir",dst=/fixtures,readonly \
		--mount type=bind,src="$$tests_dir",dst=/tests,readonly \
		--entrypoint /bin/sh "$$cpu_packages_image" \
		-c 'python -P /opt/materialize.py --verify-only --lock /input/molformer.lock.toml --output /cache && python -m unittest discover -v -s /tests -p "test_*.py"'

test/integration/bruker-reference: private REFERENCE_TEST_PATTERN := test_bruker_reference.py
test/integration/jcamp-reference: private REFERENCE_TEST_PATTERN := test_jcamp_reference.py
test/integration/bruker-reference test/integration/jcamp-reference:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot run a spectrum reference test as host UID 0.' >&2
		exit 2
	fi
	tests_dir=$$(realpath -e tests/integration)
	fixtures_dir=$$(realpath -e tests/fixtures)
	cpu_packages_image=$$($(MAKE) --no-print-directory packages/cpu/image)
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--mount type=bind,src="$$fixtures_dir",dst=/fixtures,readonly \
		--mount type=bind,src="$$tests_dir",dst=/tests,readonly \
		--entrypoint python "$$cpu_packages_image" \
		-m unittest discover -v -s /tests -p "$(REFERENCE_TEST_PATTERN)"

test/integration/challenges: private CHALLENGE_TEST := PublishedChallengeTest
test/integration/challenges/bruker: private CHALLENGE_TEST := PublishedChallengeTest.test_bruker_full_index_runs_one_graph_ga_generation
test/integration/challenges test/integration/challenges/bruker: private export CANDIDATE_GPU_INPUT = $(value CANDIDATE_GPU)
test/integration/challenges test/integration/challenges/bruker: packages/gpu/image
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot run the published challenge tests as host UID 0.' >&2
		exit 2
	fi
	[[ "$${CANDIDATE_GPU_INPUT}" =~ ^[0-9]+$$ ]] || {
		printf '%s\n' 'CANDIDATE_GPU must name one GPU by its nonnegative integer index.' >&2
		exit 2
	}
	cache_dir=$$(realpath -e "$(MOLFORMER_CACHE)")
	checkpoint_dir=$$(realpath -e "$(CHECKPOINT_DIRECTORY)")
	test_file=$$(realpath -e tests/challenges/test_published_challenges.py)
	fixtures_dir=$$(realpath -e tests/fixtures/challenges)
	package_image=$$($(DOCKER) image inspect --format '{{.Id}}' "$(call packages_image_tag,gpu)")
	# The pinned full bundle peaked at 42.3 GiB; 64 GiB leaves operating headroom.
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 256 --cpus 8 \
		--memory 64g --memory-swap 64g --gpus "device=$${CANDIDATE_GPU_INPUT}" \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777 \
		--tmpfs /modules:rw,nosuid,nodev,noexec,size=16m,mode=1777 \
		--env HF_HUB_CACHE=/cache/hub --env HF_HUB_OFFLINE=1 \
		--env TRANSFORMERS_OFFLINE=1 --env HF_MODULES_CACHE=/modules \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--mount type=bind,src="$(MOLFORMER_LOCK)",dst=/input/molformer.lock.toml,readonly \
		--mount type=bind,src="$$cache_dir",dst=/cache,readonly \
		--mount type=bind,src="$$checkpoint_dir",dst=/checkpoint,readonly \
		--mount type=bind,src="$$fixtures_dir",dst=/fixtures/challenges,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tests/fixtures/bruker/F3697/1/pdata/1",dst=/fixtures/bruker/F3697/1/pdata/1,readonly \
		--mount type=bind,src="$$test_file",dst=/tests/test_published_challenges.py,readonly \
		--entrypoint python "$$package_image" \
		-P /tests/test_published_challenges.py -v "$(CHALLENGE_TEST)"

test/provider:
	@tests_dir=$$(realpath -e tests/provider)
	provider_image=$$($(MAKE) --no-print-directory provider/image)
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
		--mount type=bind,src="$(REPOSITORY_ROOT)/config/provider.toml.example",dst=/workspace/config/provider.toml.example,readonly \
		--mount type=bind,src="$$tests_dir",dst=/workspace/tests/provider,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/contracts",dst=/workspace/contracts,readonly \
		--entrypoint python "$$provider_image" \
		-m unittest discover -v -s /workspace/tests/provider -p 'test_*.py'

test/qualification-tools:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot run qualification tool tests as host UID 0.' >&2
		exit 2
	fi
	tests_dir=$$(realpath -e tests/qualification)
	tools_dir=$$(realpath -e tools)
	cpu_packages_image=$$($(MAKE) --no-print-directory packages/cpu/image)
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 2g --memory-swap 2g \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m \
		--env PYTHONPATH=/tools --env PYTHONDONTWRITEBYTECODE=1 \
		--mount type=bind,src="$$tools_dir",dst=/tools,readonly \
		--mount type=bind,src="$$tests_dir",dst=/tests,readonly \
		--entrypoint python "$$cpu_packages_image" \
		-P -m unittest discover -v -s /tests -p 'test_*.py'

.PHONY: interpreter/model-behavior
interpreter/model-behavior: private export LIVE_CONFIG_DIR_INPUT := $(value CONFIG_DIR)
interpreter/model-behavior: private export LIVE_KEY_FILE_INPUT := $(value KEY_FILE)
interpreter/model-behavior:
	@test "$(HOST_UID)" -ne 0
	test -z "$${SECS_WLAN_INTERFACE_INPUT:-}" || { \
		printf '%s\n' 'This live test uses direct HTTPS; WLAN_INTERFACE proxy routing is not supported.' >&2; exit 2; }
	test -n "$${LIVE_CONFIG_DIR_INPUT}" -a -n "$${LIVE_KEY_FILE_INPUT}" || { \
		printf '%s\n' 'Set CONFIG_DIR to the provider configuration directory and KEY_FILE to its interpreter key. This live test makes paid model requests.' >&2; exit 2; }
	config_dir=$$(realpath -e -- "$${LIVE_CONFIG_DIR_INPUT}")
	key_file=$$(realpath -e -- "$${LIVE_KEY_FILE_INPUT}")
	for path in "$$config_dir" "$$key_file" "$(REPOSITORY_ROOT)"; do
		[[ "$$path" != *,* && "$$path" != *$$'\n'* ]] || { printf '%s\n' 'Live test mount paths cannot contain commas or newlines.' >&2; exit 2; }
	done
	ca_mount=()
	if test -f "$$config_dir/interpreter-ca.crt"; then
		ca_mount=(--mount "type=bind,src=$$config_dir/interpreter-ca.crt,dst=/run/config/provider/interpreter-ca.crt,readonly")
	fi
	provider_image=$$($(MAKE) --no-print-directory provider/image)
	$(DOCKER) run --rm --init --pull never --network bridge --read-only \
		--user "$(HOST_UID):$(HOST_GID)" --cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 256m --memory-swap 256m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
		--mount "type=bind,src=$$config_dir/provider.toml,dst=/run/config/provider/provider.toml,readonly" \
		--mount "type=bind,src=$$key_file,dst=/run/secrets/provider/interpreter.key,readonly" \
		"$${ca_mount[@]}" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/tests/model_behavior,dst=/tests,readonly" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/tests/fixtures/jcamp/4-chlorobenzylamine/4-chlorobenzylamine.jdx,dst=/fixtures/proton.jdx,readonly" \
		--entrypoint python "$$provider_image" -P /tests/test_interpreter_live.py --failfast

.PHONY: test/provider/e2e
# A single retained log file has no rotated files to compress. Override the
# daemon's compression default so this bounded test logger works on either host.
test/provider/e2e:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot run the provider scenario as host UID 0.' >&2
		exit 2
	fi
	provider_image=$$($(MAKE) --no-print-directory provider/image)
	cpu_image=$$($(MAKE) --no-print-directory packages/cpu/image)
	stage=$$(mktemp -d /tmp/secs-provider-e2e.XXXXXXXX)
	worker_started=0
	controller_started=0
	cleanup() {
		status=$$?
		for role in controller worker; do
			started_variable=$${role}_started
			if test "$${!started_variable}" -eq 0; then continue; fi
			if ! test -s "$$stage/$$role.cid"; then
				printf '%s\n' "$$role container identity is unconfirmed; retaining $$stage" >&2
				exit 1
			fi
			read -r container_id < "$$stage/$$role.cid" || true
			[[ "$$container_id" =~ ^[a-f0-9]{64}$$ ]] || exit 1
			if test "$$status" -ne 0; then $(DOCKER) logs --tail 80 "$$container_id" >&2 || true; fi
			if ! $(DOCKER) rm --force "$$container_id" >/dev/null; then
				printf '%s\n' "$$role removal is unconfirmed; retaining $$stage" >&2
				exit 1
			fi
		done
		rm -rf -- "$$stage"
		exit "$$status"
	}
	trap cleanup EXIT
	mkdir -m 700 "$$stage/sources" "$$stage/socket" "$$stage/state"
	cache_dir=$$(realpath -e "$(MOLFORMER_CACHE)")
	checkpoint_dir=$$(realpath -e "$(CHECKPOINT_DIRECTORY)")
	worker_started=1
	$(DOCKER) run --cidfile "$$stage/worker.cid" --detach --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" --cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 3g --memory-swap 3g \
		--log-opt max-size=1m --log-opt max-file=1 --log-opt compress=false \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
		--tmpfs /modules:rw,nosuid,nodev,noexec,size=16m,mode=1777 \
		--env HF_HUB_CACHE=/cache/hub --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
		--env HF_MODULES_CACHE=/modules --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/tests \
		--env OMP_NUM_THREADS=2 --env OPENBLAS_NUM_THREADS=2 --env MKL_NUM_THREADS=2 \
		--env TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor \
		--mount type=bind,src="$(MOLFORMER_LOCK)",dst=/input/molformer.lock.toml,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
		--mount type=bind,src="$$cache_dir",dst=/cache,readonly \
		--mount type=bind,src="$$checkpoint_dir",dst=/checkpoint,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tests/e2e/worker_fixture.py",dst=/tests/worker_fixture.py,readonly \
		--mount type=bind,src="$$stage/sources",dst=/run/secs/sources \
		--mount type=bind,src="$$stage/socket",dst=/run/secs/worker \
		--entrypoint /bin/sh "$$cpu_image" \
		-c 'python -P /opt/materialize.py --verify-only --lock /input/molformer.lock.toml --output /cache && exec python -m worker_fixture' >/dev/null
	controller_started=1
	$(DOCKER) run --cidfile "$$stage/controller.cid" --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" --cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 1 --memory 512m --memory-swap 512m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777 \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tests/e2e/test_provider_e2e.py",dst=/tests/e2e/test_provider_e2e.py,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tests/provider/test_http.py",dst=/tests/e2e/tls_fixture.py,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tests/fixtures",dst=/fixtures,readonly \
		--mount type=bind,src="$$stage/sources",dst=/run/secs/sources \
		--mount type=bind,src="$$stage/socket",dst=/run/secs/worker,readonly \
		--mount type=bind,src="$$stage/state",dst=/state \
		--entrypoint python "$$provider_image" \
		-m unittest discover -v -s /tests/e2e -p test_provider_e2e.py
