MOLFORMER_CACHE := cache/molformer

.PHONY: molformer/cache

molformer/cache:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot materialize the MolFormer cache as host UID 0.' >&2
		exit 2
	fi
	$(MAKE) --no-print-directory packages/cpu/image
	cache_path="$(REPOSITORY_ROOT)/$(MOLFORMER_CACHE)"
	cache_parent="$(REPOSITORY_ROOT)/$(dir $(MOLFORMER_CACHE))"
	mkdir -p "$$cache_parent"
	# Stage beside the destination so publication is a same-filesystem rename.
	stage=$$(mktemp -d --tmpdir="$$cache_parent" .molformer.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	$(DOCKER) run --rm --init --pull never --network bridge --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 256m --memory-swap 256m \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
		--env HF_HUB_DISABLE_TELEMETRY=1 \
		--mount type=bind,src="$(REPOSITORY_ROOT)/molformer.lock.toml",dst=/input/molformer.lock.toml,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
		--mount type=bind,src="$$stage",dst=/output \
		--entrypoint python secs-repro/packages-cpu:local -P /opt/materialize.py \
		--lock /input/molformer.lock.toml --output /output
	rm -rf "$$cache_path"
	mv -T "$$stage" "$$cache_path"
	trap - EXIT
