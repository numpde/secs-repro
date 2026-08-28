.PHONY: test/integration

test/integration:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot run integration tests as host UID 0.' >&2
		exit 2
	fi
	cache_dir=$$(realpath -e "$(MOLFORMER_CACHE)")
	tests_dir=$$(realpath -e tests/integration)
	$(MAKE) --no-print-directory packages/cpu/image
	# Verify cached Python before the integration test imports it.
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 1g --memory-swap 1g \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,uid=$(HOST_UID),gid=$(HOST_GID) \
		--tmpfs /modules:rw,nosuid,nodev,noexec,size=16m,uid=$(HOST_UID),gid=$(HOST_GID) \
		--env HF_HUB_CACHE=/cache/hub \
		--env HF_HUB_OFFLINE=1 \
		--env TRANSFORMERS_OFFLINE=1 \
		--env HF_MODULES_CACHE=/modules \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--mount type=bind,src="$(REPOSITORY_ROOT)/molformer.lock.toml",dst=/input/molformer.lock.toml,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
		--mount type=bind,src="$$cache_dir",dst=/cache,readonly \
		--mount type=bind,src="$$tests_dir",dst=/tests,readonly \
		--entrypoint /bin/sh secs-repro/packages-cpu:local \
		-c 'python -P /opt/materialize.py --verify-only --lock /input/molformer.lock.toml --output /cache && python -m unittest discover -v -s /tests -p "test_*.py"'
