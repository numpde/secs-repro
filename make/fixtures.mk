override FRONTEND_REFERENCE_LOCK := $(REPOSITORY_ROOT)/contracts/upstream/frontend_reference.json
FRONTEND_REFERENCE_REPOSITORY ?= $(abspath ../fork-of-elucidation.cheminfo.org)
override FRONTEND_BRUKER_REFERENCE_INPUT := tests/fixtures/bruker/F3697/1
override FRONTEND_BRUKER_REFERENCE_OUTPUT := tests/fixtures/frontend/F3697-1.json
override FRONTEND_JCAMP_REFERENCE_INPUT := tests/fixtures/jcamp/4-chlorobenzylamine
override FRONTEND_JCAMP_REFERENCE_OUTPUT := tests/fixtures/frontend/4-chlorobenzylamine.json
override FRONTEND_NTUPLES_REFERENCE_INPUT := tests/fixtures/jcamp/ethylvinylether
override FRONTEND_NTUPLES_REFERENCE_OUTPUT := tests/fixtures/frontend/ethylvinylether.json
override FRONTEND_REFERENCE_IMAGE_TAG := secs-repro/frontend-reference

.PHONY: fixtures/frontend-reference/base-image/pull
.PHONY: fixtures/frontend-reference/image fixtures/frontend-reference/write
.PHONY: fixtures/input/write

fixtures/input/write:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot write input fixtures as host UID 0.' >&2
		exit 2
	fi
	output_directory=$$(realpath -e -- tests/fixtures/input)
	stage=$$(mktemp -d --tmpdir="$(REPOSITORY_ROOT)/tests/fixtures" .input-reference.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	image=$$($(MAKE) --no-print-directory fixtures/frontend-reference/image)
	$(DOCKER) run --rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 2g --memory-swap 2g \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
		--mount "type=bind,src=$$stage,dst=/output" \
		--entrypoint node "$$image" /opt/reference/generate_input_fixtures.mjs
	# A failed reference read must not publish a partly generated corpus.
	for artifact in "$$stage"/*; do mv -f -- "$$artifact" "$$output_directory/"; done

fixtures/frontend-reference/base-image/pull:
	reference_output=$$(python3 tools/frontend_reference_lock.py values "$(FRONTEND_REFERENCE_LOCK)")
	mapfile -t reference_values <<< "$$reference_output"
	$(DOCKER) pull "$${reference_values[0]}"

fixtures/frontend-reference/image:
	@build_context=$$(mktemp -d)
	frontend_context=$$(mktemp -d)
	trap 'rm -rf "$$build_context" "$$frontend_context"' EXIT
	install -D -m 0644 "$(FRONTEND_REFERENCE_LOCK)" "$$build_context/contracts/upstream/frontend_reference.json"
	install -D -m 0644 containers/frontend-reference/Dockerfile "$$build_context/containers/frontend-reference/Dockerfile"
	install -D -m 0644 containers/frontend-reference/Dockerfile.dockerignore "$$build_context/containers/frontend-reference/Dockerfile.dockerignore"
	install -D -m 0644 tools/generate_frontend_reference.ts "$$build_context/tools/generate_frontend_reference.ts"
	install -D -m 0644 tools/generate_input_fixtures.mjs "$$build_context/tools/generate_input_fixtures.mjs"
	install -D -m 0644 tools/generate_nmrium_fixtures.mjs "$$build_context/tools/generate_nmrium_fixtures.mjs"
	reference_output=$$(python3 tools/frontend_reference_lock.py values "$$build_context/contracts/upstream/frontend_reference.json")
	mapfile -t reference_values <<< "$$reference_output"
	node_image=$${reference_values[0]}
	revision=$${reference_values[1]}
	reference_lock_id=$$(python3 tools/frontend_reference_lock.py id "$$build_context/contracts/upstream/frontend_reference.json")
	git -C "$(FRONTEND_REFERENCE_REPOSITORY)" archive \
		"$$revision" package.json package-lock.json src \
		| tar -x -C "$$frontend_context"
	frontend_reference_build_id=$$(python3 tools/frontend_reference_lock.py producer-id frontend "$$build_context")
	input_reference_build_id=$$(python3 tools/frontend_reference_lock.py producer-id input "$$build_context")
	image_id=$$(python3 tools/frontend_reference_lock.py image-id "$$build_context")
	$(DOCKER) build --quiet --network default --pull=false \
		--build-arg NODE_IMAGE="$$node_image" \
		--build-arg REFERENCE_BUILD_ID="$$frontend_reference_build_id" \
		--build-arg REFERENCE_LOCK_ID="$$reference_lock_id" \
		--build-arg INPUT_REFERENCE_BUILD_ID="$$input_reference_build_id" \
		--build-context "frontend=$$frontend_context" \
		--file "$$build_context/containers/frontend-reference/Dockerfile" \
		--tag "$(FRONTEND_REFERENCE_IMAGE_TAG):inputs-$${image_id#sha256:}" \
		"$$build_context"

fixtures/frontend-reference/write:
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Cannot write frontend fixtures as host UID 0.' >&2
		exit 2
	fi
	output_directory=$$(realpath -e -- "$(dir $(FRONTEND_BRUKER_REFERENCE_OUTPUT))")
	stage=$$(mktemp -d --tmpdir="$$output_directory" .frontend-reference.XXXXXXXX)
	trap 'rm -rf "$$stage"' EXIT
	stage=$$(realpath -e -- "$$stage")
	image=$$($(MAKE) --no-print-directory fixtures/frontend-reference/image)
	reference_container=(--rm --init --pull never --network none --read-only \
		--user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 2g --memory-swap 2g \
		--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
		--mount "type=bind,src=$$stage,dst=/output")
	$(DOCKER) run "$${reference_container[@]}" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(FRONTEND_BRUKER_REFERENCE_INPUT),dst=/input,readonly" \
		"$$image" \
		--input /input \
		--output "/output/$(notdir $(FRONTEND_BRUKER_REFERENCE_OUTPUT))" \
		--path-prefix F3697/1
	$(DOCKER) run "$${reference_container[@]}" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(FRONTEND_JCAMP_REFERENCE_INPUT),dst=/input,readonly" \
		"$$image" \
		--input /input \
		--output "/output/$(notdir $(FRONTEND_JCAMP_REFERENCE_OUTPUT))" \
		--path-prefix 4-chlorobenzylamine
	$(DOCKER) run "$${reference_container[@]}" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(FRONTEND_NTUPLES_REFERENCE_INPUT),dst=/input,readonly" \
		"$$image" \
		--input /input \
		--output "/output/$(notdir $(FRONTEND_NTUPLES_REFERENCE_OUTPUT))" \
		--path-prefix ethylvinylether
	# Every conversion must succeed before any pinned reference is published.
	mv -f "$$stage/$(notdir $(FRONTEND_BRUKER_REFERENCE_OUTPUT))" "$(FRONTEND_BRUKER_REFERENCE_OUTPUT)"
	mv -f "$$stage/$(notdir $(FRONTEND_JCAMP_REFERENCE_OUTPUT))" "$(FRONTEND_JCAMP_REFERENCE_OUTPUT)"
	mv -f "$$stage/$(notdir $(FRONTEND_NTUPLES_REFERENCE_OUTPUT))" "$(FRONTEND_NTUPLES_REFERENCE_OUTPUT)"
