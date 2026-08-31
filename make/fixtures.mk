override FRONTEND_REFERENCE_NODE_IMAGE := docker.io/library/node:24-alpine@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf
override FRONTEND_REFERENCE_REVISION := 5ab78f61e9fb679f3f0b9823be5217ae250e213f
FRONTEND_REFERENCE_REPOSITORY ?= $(abspath ../fork-of-elucidation.cheminfo.org)
override FRONTEND_BRUKER_REFERENCE_INPUT := tests/fixtures/bruker/F3697/1
override FRONTEND_BRUKER_REFERENCE_OUTPUT := tests/fixtures/frontend/F3697-1.json
override FRONTEND_JCAMP_REFERENCE_INPUT := tests/fixtures/jcamp/4-chlorobenzylamine
override FRONTEND_JCAMP_REFERENCE_OUTPUT := tests/fixtures/frontend/4-chlorobenzylamine.json
override FRONTEND_REFERENCE_IMAGE_TAG := secs-repro/frontend-reference

.PHONY: fixtures/frontend-reference/base-image/pull
.PHONY: fixtures/frontend-reference/image fixtures/frontend-reference/write

fixtures/frontend-reference/base-image/pull:
	$(DOCKER) pull "$(FRONTEND_REFERENCE_NODE_IMAGE)"

fixtures/frontend-reference/image:
	@frontend_context=$$(mktemp -d)
	trap 'rm -rf "$$frontend_context"' EXIT
	git -C "$(FRONTEND_REFERENCE_REPOSITORY)" archive \
		"$(FRONTEND_REFERENCE_REVISION)" package.json package-lock.json src \
		| tar -x -C "$$frontend_context"
	input_id=$$( {
		printf '%s\n' "$(FRONTEND_REFERENCE_NODE_IMAGE)" "$(FRONTEND_REFERENCE_REVISION)"
		cat containers/frontend-reference/Dockerfile \
			containers/frontend-reference/Dockerfile.dockerignore \
			tools/generate_frontend_reference.ts
	} | sha256sum | cut -d' ' -f1 )
	$(DOCKER) build --quiet --network default --pull=false \
		--build-arg NODE_IMAGE="$(FRONTEND_REFERENCE_NODE_IMAGE)" \
		--build-context "frontend=$$frontend_context" \
		--file containers/frontend-reference/Dockerfile \
		--tag "$(FRONTEND_REFERENCE_IMAGE_TAG):inputs-$$input_id" \
		.

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
		--path-prefix F3697/1 \
		--frontend-revision "$(FRONTEND_REFERENCE_REVISION)"
	$(DOCKER) run "$${reference_container[@]}" \
		--mount "type=bind,src=$(REPOSITORY_ROOT)/$(FRONTEND_JCAMP_REFERENCE_INPUT),dst=/input,readonly" \
		"$$image" \
		--input /input \
		--output "/output/$(notdir $(FRONTEND_JCAMP_REFERENCE_OUTPUT))" \
		--path-prefix 4-chlorobenzylamine \
		--frontend-revision "$(FRONTEND_REFERENCE_REVISION)"
	# Both conversions must succeed before either pinned reference is published.
	mv -f "$$stage/$(notdir $(FRONTEND_BRUKER_REFERENCE_OUTPUT))" "$(FRONTEND_BRUKER_REFERENCE_OUTPUT)"
	mv -f "$$stage/$(notdir $(FRONTEND_JCAMP_REFERENCE_OUTPUT))" "$(FRONTEND_JCAMP_REFERENCE_OUTPUT)"
