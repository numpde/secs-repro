.PHONY: test/integration test/integration/bruker-reference test/provider
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

test/provider:
	@tests_dir=$$(realpath -e tests/provider)
	provider_image=$$($(MAKE) --no-print-directory provider/image)
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 512m --memory-swap 512m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--mount type=bind,src="$(REPOSITORY_ROOT)/config/provider.toml.example",dst=/workspace/config/provider.toml.example,readonly \
		--mount type=bind,src="$$tests_dir",dst=/workspace/tests/provider,readonly \
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
