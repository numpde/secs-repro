QUALIFICATION_SPEC := $(CHECKPOINT_DIRECTORY)/candidate-qualification.toml
QUALIFICATION_OUTPUT_ROOT := qualification/$(notdir $(CHECKPOINT_DIRECTORY))
QUALIFICATION_FRONTEND_SPECTRUM := tests/fixtures/frontend/F3697-1.json
override QUALIFICATION_EXTRACT_TIMEOUT_SECONDS := 1800
override QUALIFICATION_SAMPLE_TIMEOUT_SECONDS := 1800
override QUALIFICATION_FUNCTIONAL_TIMEOUT_SECONDS := 1800
override QUALIFICATION_SCALE_TIMEOUT_SECONDS := 7200
override QUALIFICATION_VERIFY_TIMEOUT_SECONDS := 2700
override QUALIFICATION_RECEIPT_TIMEOUT_SECONDS := 120
override QUALIFICATION_KILL_GRACE_SECONDS := 30

.PHONY: candidates/qualification
candidates/qualification: private export ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE))
candidates/qualification: private export QUALIFICATION_GPU_INPUT := $(value CANDIDATE_GPU)
candidates/qualification: private export QUALIFICATION_CPUS_INPUT := $(value CANDIDATE_CPUS)
candidates/qualification: private export QUALIFICATION_MEMORY_INPUT := $(value CANDIDATE_MEMORY)
candidates/qualification: private export QUALIFICATION_DTYPE_INPUT := $(value CANDIDATE_DTYPE)
candidates/qualification: checkpoint/image packages/gpu/image
	@if test "$(HOST_UID)" -eq 0; then
		printf '%s\n' 'Run make candidates/qualification as a non-root host user.' >&2
		exit 2
	fi
	if test -z "$${ARCHIVE_INPUT}"; then
		printf '%s\n' 'Cannot qualify the candidate builder without ARCHIVE=/absolute/path/zenodo_secs_v3.tar.gz.' >&2
		exit 2
	fi
	if test ! -f "$${ARCHIVE_INPUT}"; then
		printf 'Cannot qualify the candidate builder because ARCHIVE is not an existing regular file: %s\n' "$${ARCHIVE_INPUT}" >&2
		exit 2
	fi
	checkout_changes=$$(git status --short --untracked-files=all -- . ':(exclude).idea')
	if test -n "$$checkout_changes"; then
		printf '%s\n' 'Cannot qualify the candidate builder while repository changes outside .idea prevent the receipt from naming an exact revision.' >&2
		exit 2
	fi
	command -v nvidia-smi >/dev/null || {
		printf '%s\n' 'Cannot qualify the candidate builder because nvidia-smi is unavailable for GPU isolation and measurement.' >&2
		exit 2
	}
	command -v timeout >/dev/null || {
		printf '%s\n' 'Cannot qualify the candidate builder because timeout is unavailable for bounded phase execution.' >&2
		exit 2
	}
	[[ "$${QUALIFICATION_GPU_INPUT}" =~ ^[0-9]+$$ ]] || {
		printf '%s\n' 'CANDIDATE_GPU must name one GPU by its nonnegative integer index.' >&2
		exit 2
	}
	[[ "$${QUALIFICATION_CPUS_INPUT}" =~ ^[1-9][0-9]*$$ ]] || {
		printf '%s\n' 'CANDIDATE_CPUS must be a positive integer.' >&2
		exit 2
	}
	[[ "$${QUALIFICATION_MEMORY_INPUT}" =~ ^[1-9][0-9]*[mg]$$ ]] || {
		printf '%s\n' 'CANDIDATE_MEMORY must be a positive whole number of megabytes or gigabytes, such as 32000m or 32g.' >&2
		exit 2
	}
	case "$${QUALIFICATION_DTYPE_INPUT}" in
		float32|bfloat16) ;;
		*) printf 'Cannot qualify candidates with CANDIDATE_DTYPE=%q; select float32 or bfloat16 forward compute.\n' \
			"$${QUALIFICATION_DTYPE_INPUT}" >&2; exit 2 ;;
	esac
	busy_gpu_processes=$$(nvidia-smi -i "$${QUALIFICATION_GPU_INPUT}" --query-compute-apps=pid --format=csv,noheader,nounits)
	if test -n "$$busy_gpu_processes"; then
		printf 'Cannot qualify the candidate builder while GPU %s has compute processes: %s\n' \
			"$${QUALIFICATION_GPU_INPUT}" "$$busy_gpu_processes" >&2
		exit 2
	fi

	archive=$$(realpath -e -- "$${ARCHIVE_INPUT}")
	checkpoint_directory=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	candidate_spec=$$(realpath -e -- "$(CANDIDATE_SPEC)")
	qualification_spec=$$(realpath -e -- "$(QUALIFICATION_SPEC)")
	cache_directory=$$(realpath -e -- "$(MOLFORMER_CACHE)")
	molformer_lock=$$(realpath -e -- "$(MOLFORMER_LOCK)")
	frontend_spectrum=$$(realpath -e -- "$(QUALIFICATION_FRONTEND_SPECTRUM)")
	builder=$$(realpath -e -- tools/build_candidate_index.py)
	qualifier=$$(realpath -e -- tools/qualify_candidate_index.py)
	package_image=$$($(DOCKER) image inspect --format '{{.Id}}' "$(call packages_image_tag,gpu)")
	repository_revision=$$(git rev-parse HEAD)
	require_unchanged_checkout() {
		local checkout_changes
		checkout_changes=$$(git status --short --untracked-files=all -- . ':(exclude).idea')
		if test -n "$$checkout_changes" || test "$$(git rev-parse HEAD)" != "$$repository_revision"; then
			printf '%s\n' 'Cannot publish qualification evidence because the checkout changed during the run.' >&2
			return 2
		fi
	}
	run_started=$$(date -u +%Y%m%dT%H%M%SZ)
	output_root=$$(realpath -m -- "$(QUALIFICATION_OUTPUT_ROOT)")
	mkdir -p -- "$$output_root"
	stage=$$(mktemp -d "$$output_root/.candidate-qualification.XXXXXX")
	stage=$$(realpath -e -- "$$stage")
	stage_nonce=$${stage##*.candidate-qualification.}
	run_id="$$run_started-$$(git rev-parse --short=12 HEAD)-$$stage_nonce"
	final_directory="$$output_root/$$run_id"
	chmod 2770 "$$stage"
	evidence="$$stage/evidence"
	mkdir "$$evidence"
	chmod 2770 "$$evidence"
	container_prefix="secs-candidate-qualification-$$run_id"
	owned_containers=(
		"$$container_prefix-extractor"
		"$$container_prefix-sampler"
		"$$container_prefix-functional-builder"
		"$$container_prefix-functional-verifier"
		"$$container_prefix-scale-builder"
		"$$container_prefix-scale-verifier"
		"$$container_prefix-receipt"
	)
	cleanup_qualification() {
		local original_status=$$?
		local cleanup_failed=0
		local inspection
		trap - EXIT
		for container in "$${owned_containers[@]}"; do
			if inspection=$$($(DOCKER) container inspect "$$container" 2>&1); then
				if ! $(DOCKER) rm -f "$$container" >/dev/null; then
					printf 'Failed to stop qualification container during cleanup: %s\n' "$$container" >&2
					cleanup_failed=1
				fi
			else
				case "$$inspection" in
					*'No such object'*|*'No such container'*) ;;
					*) printf 'Could not confirm qualification container cleanup for %s: %s\n' \
						"$$container" "$$inspection" >&2; cleanup_failed=1 ;;
				esac
			fi
		done
		if ! rm -rf -- "$$stage"; then
			printf 'Failed to remove qualification staging directory: %s\n' "$$stage" >&2
			cleanup_failed=1
		fi
		if test "$$original_status" -eq 0 && test "$$cleanup_failed" -ne 0; then
			original_status=1
		fi
		exit "$$original_status"
	}
	trap cleanup_qualification EXIT
	trap 'exit 129' HUP
	trap 'exit 130' INT
	trap 'exit 143' TERM

	# The extractor admits the complete pinned archive before exposing the
	# member. Keeping that full member in this private stage lets both profiles
	# share one archive scan without creating a second candidate source.
	timeout --foreground --signal=TERM --kill-after="$(QUALIFICATION_KILL_GRACE_SECONDS)s" "$(QUALIFICATION_EXTRACT_TIMEOUT_SECONDS)s" \
	$(DOCKER) run --rm --name "$$container_prefix-extractor" --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 \
		--cpus 1 --memory 2304m --memory-swap 2304m \
		--tmpfs /scratch:rw,noexec,nosuid,nodev,size=2g \
		--mount type=bind,src="$$archive",dst=/archive/input.tar.gz,readonly \
		--mount type=bind,src="$$checkpoint_directory/checkpoint.toml",dst=/input/checkpoint.toml,readonly \
		--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
		--mount type=bind,src="$(CURDIR)/tools/extract_checkpoint.py",dst=/opt/extract.py,readonly \
		--entrypoint python3 "$(CHECKPOINT_IMAGE_TAG)" \
		-P /opt/extract.py --archive /archive/input.tar.gz --spec /input/checkpoint.toml \
		--member-spec /input/candidates.toml --scratch-directory /scratch \
		> "$$stage/source.parquet"

	timeout --foreground --signal=TERM --kill-after="$(QUALIFICATION_KILL_GRACE_SECONDS)s" "$(QUALIFICATION_SAMPLE_TIMEOUT_SECONDS)s" \
	$(DOCKER) run --rm --name "$$container_prefix-sampler" --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 \
		--cpus 8 --memory 16g --memory-swap 16g --group-add "$$(id -g)" \
		--tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
		--mount type=bind,src="$$stage",dst=/stage \
		--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
		--mount type=bind,src="$$qualification_spec",dst=/input/candidate-qualification.toml,readonly \
		--mount type=bind,src="$$qualifier",dst=/opt/qualify.py,readonly \
		--entrypoint python "$$package_image" \
		-P /opt/qualify.py sample-source --source /stage/source.parquet \
		--candidate-spec /input/candidates.toml \
		--qualification-spec /input/candidate-qualification.toml \
		--output-directory /stage
	rm -- "$$stage/source.parquet"

	timestamp_stream() {
		local log=$$1
		while IFS= read -r line; do
			printf '%s|%s\n' "$$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)" "$$line" | tee -a "$$log" >&2
		done
	}
	monitor_gpu() {
		local process_id=$$1
		local log=$$2
		local container=$$3
		local container_id=""
		local compute_pids
		local measurement
		local process_cgroup
		while kill -0 "$$process_id" 2>/dev/null; do
			if test -z "$$container_id"; then
				container_id=$$($(DOCKER) container inspect --format '{{.Id}}' "$$container" 2>/dev/null || true)
				if test -z "$$container_id"; then
					sleep 1
					continue
				fi
			fi
			if ! compute_pids=$$(nvidia-smi -i "$${QUALIFICATION_GPU_INPUT}" \
				--query-compute-apps=pid --format=csv,noheader,nounits); then
				printf 'Failed to inspect compute-process ownership on GPU %s; stopping qualification container %s.\n' \
					"$${QUALIFICATION_GPU_INPUT}" "$$container" >&2
				$(DOCKER) stop --time "$(QUALIFICATION_KILL_GRACE_SECONDS)" "$$container" >/dev/null || \
					printf 'Failed to stop qualification container after GPU inspection failed: %s\n' "$$container" >&2
				return 1
			fi
			while IFS= read -r compute_pid; do
				test -z "$$compute_pid" && continue
				if { process_cgroup=$$(< "/proc/$$compute_pid/cgroup"); } 2>/dev/null; then
					if [[ "$$process_cgroup" == *"$$container_id"* ]]; then
						continue
					fi
				elif test ! -e "/proc/$$compute_pid"; then
					# A process may finish between NVIDIA's PID snapshot and this
					# ownership read. Its disappearance is not foreign GPU use.
					continue
				fi
				printf 'GPU %s gained a compute process whose ownership is not the qualification container %s: pid %s\n' \
					"$${QUALIFICATION_GPU_INPUT}" "$$container" "$$compute_pid" >&2
				$(DOCKER) stop --time "$(QUALIFICATION_KILL_GRACE_SECONDS)" "$$container" >/dev/null || \
					printf 'Failed to stop contaminated qualification container: %s\n' "$$container" >&2
				return 1
			done <<< "$$compute_pids"
			if ! measurement=$$(nvidia-smi -i "$${QUALIFICATION_GPU_INPUT}" \
				--query-gpu=uuid,name,memory.total,memory.used,utilization.gpu \
				--format=csv,noheader,nounits); then
				printf 'Failed to measure GPU %s; stopping qualification container %s.\n' \
					"$${QUALIFICATION_GPU_INPUT}" "$$container" >&2
				$(DOCKER) stop --time "$(QUALIFICATION_KILL_GRACE_SECONDS)" "$$container" >/dev/null || \
					printf 'Failed to stop qualification container after GPU measurement failed: %s\n' "$$container" >&2
				return 1
			fi
			printf '%s|%s\n' "$$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)" "$$measurement" >> "$$log"
			sleep 5
		done
		if test -z "$$container_id"; then
			printf 'Candidate builder exited before GPU measurement could identify its container: %s\n' "$$container" >&2
			return 1
		fi
	}
	run_profile() {
		local profile=$$1
		local sample="$$stage/$$profile.parquet"
		local bundle="$$stage/$$profile-bundle"
		local builder_log="$$evidence/$$profile-builder.log"
		local gpu_log="$$evidence/$$profile-gpu.csv"
		local report="$$stage/$$profile-report.json"
		local deadline_seconds
		local sample_sha256
		local builder_sha256
		sample_sha256=$$(sha256sum "$$sample" | cut -d' ' -f1)
		builder_sha256=$$(sha256sum "$$builder" | cut -d' ' -f1)
		case "$$profile" in
			functional) deadline_seconds="$(QUALIFICATION_FUNCTIONAL_TIMEOUT_SECONDS)" ;;
			scale) deadline_seconds="$(QUALIFICATION_SCALE_TIMEOUT_SECONDS)" ;;
		esac
		mkdir "$$bundle"
		chmod 2770 "$$bundle"

		(
			set +e
			timeout --foreground --signal=TERM --kill-after="$(QUALIFICATION_KILL_GRACE_SECONDS)s" "$${deadline_seconds}s" \
			$(DOCKER) run --rm -i --name "$$container_prefix-$$profile-builder" --pull never --network none --read-only \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 512 \
			--cpus "$${QUALIFICATION_CPUS_INPUT}" --memory "$${QUALIFICATION_MEMORY_INPUT}" \
			--memory-swap "$${QUALIFICATION_MEMORY_INPUT}" --gpus "device=$${QUALIFICATION_GPU_INPUT}" \
			--group-add "$$(id -g)" \
			--tmpfs /scratch:rw,noexec,nosuid,nodev,size=2g \
			--tmpfs /modules:rw,noexec,nosuid,nodev,size=128m \
			--tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
			--env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 --env HF_HUB_CACHE=/cache/hub \
			--env HF_MODULES_CACHE=/modules --env PYTHONDONTWRITEBYTECODE=1 \
			--mount type=bind,src="$$checkpoint_directory",dst=/checkpoint,readonly \
			--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
			--mount type=bind,src="$$molformer_lock",dst=/input/molformer.lock.toml,readonly \
			--mount type=bind,src="$$cache_directory",dst=/cache,readonly \
			--mount type=bind,src="$(CURDIR)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
			--mount type=bind,src="$$builder",dst=/opt/build.py,readonly \
			--mount type=bind,src="$$bundle",dst=/output \
			--entrypoint /bin/sh "$$package_image" \
			-c 'umask 0022
				started_at=$$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)
				started_ns=$$(date +%s%N)
				status=0
				python -P /opt/materialize.py --verify-only --lock /input/molformer.lock.toml --output /cache \
					&& python -P /opt/build.py --archive-spec /checkpoint/checkpoint.toml \
						--candidate-spec /input/candidates.toml --checkpoint-manifest /checkpoint/manifest.json \
						--molformer-lock /input/molformer.lock.toml --scratch-directory /scratch \
						--output-directory /output --source-kind local --device cuda:0 \
						--compute-dtype "$$6" --threads "$$7" --package-image-id "$$5" \
					|| status=$$?
				finished_ns=$$(date +%s%N)
				finished_at=$$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)
				manifest_sha256=""
				if test -f /output/manifest.json; then
					manifest_sha256=$$(sha256sum /output/manifest.json | cut -d" " -f1)
				fi
				printf "{\"run_id\":\"%s\",\"profile\":\"%s\",\"sample_sha256\":\"%s\",\"builder_sha256\":\"%s\",\"package_image_id\":\"%s\",\"compute_dtype\":\"%s\",\"threads\":%s,\"deadline_seconds\":%s,\"builder_manifest_sha256\":\"%s\",\"started_at\":\"%s\",\"finished_at\":\"%s\",\"elapsed_nanoseconds\":%s,\"memory_peak_bytes\":%s,\"memory_limit_bytes\":%s,\"exit_status\":%s}\n" \
					"$$1" "$$2" "$$3" "$$4" "$$5" "$$6" "$$7" "$$8" "$$manifest_sha256" "$$started_at" "$$finished_at" \
					"$$((finished_ns - started_ns))" "$$(cat /sys/fs/cgroup/memory.peak)" \
					"$$(cat /sys/fs/cgroup/memory.max)" "$$status" > /output/run-metrics.json
				exit "$$status"' \
			qualification-builder "$$run_id" "$$profile" "$$sample_sha256" "$$builder_sha256" "$$package_image" \
			"$${QUALIFICATION_DTYPE_INPUT}" "$${QUALIFICATION_CPUS_INPUT}" "$$deadline_seconds" \
			< "$$sample" 2>&1 | timestamp_stream "$$builder_log"
			pipeline_status=("$${PIPESTATUS[@]}")
			if test "$${pipeline_status[0]}" -ne 0; then exit "$${pipeline_status[0]}"; fi
			exit "$${pipeline_status[1]}"
		) &
		local builder_process=$$!
		monitor_gpu "$$builder_process" "$$gpu_log" "$$container_prefix-$$profile-builder" &
		local monitor_process=$$!
		local builder_status=0
		local monitor_status=0
		if wait "$$builder_process"; then :; else builder_status=$$?; fi
		if wait "$$monitor_process"; then :; else monitor_status=$$?; fi
		if test "$$builder_status" -ne 0; then
			printf 'Candidate qualification profile %s failed in the production builder with status %s.\n' \
				"$$profile" "$$builder_status" >&2
			return "$$builder_status"
		fi
		if test "$$monitor_status" -ne 0; then
			printf 'Candidate qualification profile %s completed, but GPU measurement failed with status %s; refusing an unmeasured receipt.\n' \
				"$$profile" "$$monitor_status" >&2
			return "$$monitor_status"
		fi

		timeout --foreground --signal=TERM --kill-after="$(QUALIFICATION_KILL_GRACE_SECONDS)s" "$(QUALIFICATION_VERIFY_TIMEOUT_SECONDS)s" \
		$(DOCKER) run --rm --name "$$container_prefix-$$profile-verifier" --pull never --network none --read-only \
			--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 256 \
			--cpus 4 --memory 16g --memory-swap 16g --gpus "device=$${QUALIFICATION_GPU_INPUT}" \
			--group-add "$$(id -g)" \
			--tmpfs /modules:rw,noexec,nosuid,nodev,size=128m \
			--tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
			--env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 --env HF_HUB_CACHE=/cache/hub \
			--env HF_MODULES_CACHE=/modules --env PYTHONDONTWRITEBYTECODE=1 \
			--mount type=bind,src="$$checkpoint_directory",dst=/checkpoint,readonly \
			--mount type=bind,src="$$candidate_spec",dst=/input/candidates.toml,readonly \
			--mount type=bind,src="$$qualification_spec",dst=/input/candidate-qualification.toml,readonly \
			--mount type=bind,src="$$molformer_lock",dst=/input/molformer.lock.toml,readonly \
			--mount type=bind,src="$$cache_directory",dst=/cache,readonly \
			--mount type=bind,src="$$frontend_spectrum",dst=/input/frontend-spectrum.json,readonly \
			--mount type=bind,src="$(CURDIR)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
			--mount type=bind,src="$$builder",dst=/opt/build.py,readonly \
			--mount type=bind,src="$$qualifier",dst=/opt/qualify.py,readonly \
			--mount type=bind,src="$$stage",dst=/stage \
			--entrypoint /bin/sh "$$package_image" \
			-c 'python -P /opt/materialize.py --verify-only --lock /input/molformer.lock.toml --output /cache \
				&& exec python -P /opt/qualify.py verify-profile --profile "$$1" \
					--qualification-spec /input/candidate-qualification.toml --samples-receipt /stage/samples.json \
					--candidate-spec /input/candidates.toml --checkpoint-manifest /checkpoint/manifest.json \
					--molformer-lock /input/molformer.lock.toml --builder /opt/build.py \
					--bundle "/stage/$$1-bundle" --frontend-spectrum /input/frontend-spectrum.json \
					--metrics "/stage/$$1-bundle/run-metrics.json" \
					--builder-log "/stage/evidence/$$1-builder.log" \
					--gpu-log "/stage/evidence/$$1-gpu.csv" --compute-dtype "$$5" --threads "$$6" \
					--package-image-id "$$2" --repository-revision "$$3" --run-id "$$4" --deadline-seconds "$$7" \
					--output "/stage/$$1-report.json"' \
			qualification-verifier "$$profile" "$$package_image" "$$repository_revision" "$$run_id" \
			"$${QUALIFICATION_DTYPE_INPUT}" "$${QUALIFICATION_CPUS_INPUT}" "$$deadline_seconds"
		# The final composer re-hashes these retained files. Make accidental
		# mutation fail at its source as well as at that final proof boundary.
		chmod a=r "$$builder_log" "$$gpu_log"
		rm -rf -- "$$bundle"
		rm -- "$$sample"
	}

	run_profile functional
	run_profile scale

	timeout --foreground --signal=TERM --kill-after="$(QUALIFICATION_KILL_GRACE_SECONDS)s" "$(QUALIFICATION_RECEIPT_TIMEOUT_SECONDS)s" \
	$(DOCKER) run --rm --name "$$container_prefix-receipt" --pull never --network none --read-only \
		--cap-drop ALL --security-opt no-new-privileges:true --pids-limit 32 \
		--cpus 1 --memory 1g --memory-swap 1g --group-add "$$(id -g)" \
		--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
		--mount type=bind,src="$$checkpoint_directory",dst=/checkpoint,readonly \
		--mount type=bind,src="$$molformer_lock",dst=/input/molformer.lock.toml,readonly \
		--mount type=bind,src="$$qualification_spec",dst=/input/candidate-qualification.toml,readonly \
		--mount type=bind,src="$$qualifier",dst=/opt/qualify.py,readonly \
		--mount type=bind,src="$$stage",dst=/stage \
		--entrypoint python "$$package_image" \
		-P /opt/qualify.py write-receipt \
		--qualification-spec /input/candidate-qualification.toml --samples-receipt /stage/samples.json \
		--functional-report /stage/functional-report.json --scale-report /stage/scale-report.json \
		--evidence-directory /stage/evidence \
		--checkpoint-manifest /checkpoint/manifest.json --molformer-lock /input/molformer.lock.toml \
		--production-output-directory /checkpoint \
		--repository-revision "$$repository_revision" --run-id "$$run_id" \
		--discarded-path /stage/source.parquet --discarded-path /stage/functional.parquet \
		--discarded-path /stage/scale.parquet --discarded-path /stage/functional-bundle \
		--discarded-path /stage/scale-bundle --output /stage/evidence/receipt.json
	require_unchanged_checkout
	chmod g-s,u=rwx,go=rx "$$evidence"
	mv -T -- "$$evidence" "$$final_directory"
	printf 'Candidate builder qualification passed; evidence: %s\n' "$$final_directory"
