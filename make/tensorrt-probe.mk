TENSORRT_GPU ?= 0
TENSORRT_MEMORY ?= 40g
override TENSORRT_DOWNLOAD_LOCK := $(REPOSITORY_ROOT)/tensorrt-probe.lock.json
override TENSORRT_OUTPUT_ROOT := $(REPOSITORY_ROOT)/qualification/tensorrt-probe
override TENSORRT_CACHE := $(REPOSITORY_ROOT)/cache/tensorrt-probe
override TENSORRT_CANDIDATE_SPEC := $(abspath $(CHECKPOINT_DIRECTORY)/candidates.toml)
override TENSORRT_CHECKPOINT_IMAGE_TAG := secs-repro/checkpoint-extractor:local

.PHONY: tensorrt/probe
tensorrt/probe: private export TENSORRT_ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE))
tensorrt/probe: private export TENSORRT_GPU_INPUT := $(value TENSORRT_GPU)
tensorrt/probe: private export TENSORRT_MEMORY_INPUT := $(value TENSORRT_MEMORY)
tensorrt/probe: checkpoint/image packages/gpu/image
	@test "$$(id -u)" -ne 0 || { \
		printf '%s\n' 'Run make tensorrt/probe as a non-root host user.' >&2; exit 2; }
	@test -n "$${SECS_WLAN_INTERFACE_INPUT:-}" || { \
		printf '%s\n' 'Set WLAN_INTERFACE so downloads cannot use the wired route.' >&2; exit 2; }
	@test -n "$${TENSORRT_ARCHIVE_INPUT:-}" || { \
		printf '%s\n' 'ARCHIVE must name the already-downloaded SECS archive.' >&2; exit 2; }
	@test "$${TENSORRT_GPU_INPUT:-}" = 0 || { \
		printf '%s\n' 'TENSORRT_GPU must be 0 so timing uses the production build GPU.' >&2; exit 2; }
	@[[ "$${TENSORRT_MEMORY_INPUT:-}" =~ ^[1-9][0-9]*(m|g)$$ ]] || { \
		printf '%s\n' 'TENSORRT_MEMORY must be a positive integer followed by m or g.' >&2; exit 2; }
	@dirty=$$(git status --porcelain --untracked-files=all | sed '/^?? \.idea\//d')
	@test -z "$$dirty" || { \
		printf '%s\n%s\n' 'Commit or remove checkout changes before producing TensorRT evidence:' "$$dirty" >&2; exit 2; }
	@archive=$$(realpath -e -- "$${TENSORRT_ARCHIVE_INPUT}")
	mkdir -p "$(TENSORRT_CACHE)"
	download_lock_sha256=$$(sha256sum "$(TENSORRT_DOWNLOAD_LOCK)" | cut -d' ' -f1)
	download_cache="$(TENSORRT_CACHE)/downloads-$$download_lock_sha256"
	mkdir -p "$$download_cache"
	exec 8>"$(TENSORRT_CACHE)/.lock"
	flock -n 8 || { \
		printf '%s\n' 'Another TensorRT probe owns the dependency cache.' >&2; exit 2; }
	required_cache_bytes=$$(python3 -P "$(REPOSITORY_ROOT)/tools/stage_tensorrt_probe.py" \
		--lock "$(TENSORRT_DOWNLOAD_LOCK)" --output "$$download_cache" \
		--print-required-cache-bytes)
	available_cache_bytes=$$(df --output=avail -B1 "$(TENSORRT_CACHE)" | tail -n 1)
	test "$$available_cache_bytes" -ge "$$required_cache_bytes" || { \
		printf 'TensorRT caching needs %s free bytes on the cache filesystem; only %s are available.\n' \
			"$$required_cache_bytes" "$$available_cache_bytes" >&2; exit 2; }
	required_tmp_bytes=$$(python3 -P "$(REPOSITORY_ROOT)/tools/stage_tensorrt_probe.py" \
		--lock "$(TENSORRT_DOWNLOAD_LOCK)" --print-required-transient-bytes)
	available_tmp_bytes=$$(df --output=avail -B1 /tmp | tail -n 1)
	test "$$available_tmp_bytes" -ge "$$required_tmp_bytes" || { \
		printf 'TensorRT staging needs %s free bytes under /tmp; only %s are available.\n' \
			"$$required_tmp_bytes" "$$available_tmp_bytes" >&2; exit 2; }
	repository_revision=$$(git rev-parse HEAD)
	launcher_sha256=$$(sha256sum "$(REPOSITORY_ROOT)/make/tensorrt-probe.mk" | cut -d' ' -f1)
	require_unchanged_checkout() {
		if ! current_revision=$$(git rev-parse HEAD); then
			printf '%s\n' 'Git could not read the repository revision for publication.' >&2
			return 2
		fi
		test "$$current_revision" = "$$repository_revision" || {
			printf 'Repository revision changed during the TensorRT probe: %s -> %s.\n' \
				"$$repository_revision" "$$current_revision" >&2; return 2; }
		if ! current_dirty=$$(git status --porcelain --untracked-files=all); then
			printf '%s\n' 'Git could not inspect the checkout for publication.' >&2
			return 2
		fi
		current_dirty=$$(sed '/^?? \.idea\//d' <<< "$$current_dirty")
		test -z "$$current_dirty" || {
			printf '%s\n%s\n' 'Checkout changed during the TensorRT probe:' \
				"$$current_dirty" >&2; return 2; }
		if ! current_launcher_sha256=$$(sha256sum \
			"$(REPOSITORY_ROOT)/make/tensorrt-probe.mk" | cut -d' ' -f1); then
			printf '%s\n' 'The TensorRT launcher could not be hashed for publication.' >&2
			return 2
		fi
		test "$$current_launcher_sha256" = "$$launcher_sha256" || {
			printf '%s\n' 'The TensorRT launcher changed during the probe.' >&2; return 2; }
	}
	package_image=$$($(DOCKER) image inspect --format '{{.Id}}' "$(call packages_image_tag,gpu)")
	gpu_uuid=$$(nvidia-smi --id="$${TENSORRT_GPU_INPUT}" --query-gpu=uuid --format=csv,noheader)
	test -n "$$gpu_uuid" || { printf '%s\n' 'GPU 0 has no NVIDIA UUID.' >&2; exit 2; }
	compute_pids=$$(nvidia-smi --id="$${TENSORRT_GPU_INPUT}" \
		--query-compute-apps=pid --format=csv,noheader,nounits)
	test -z "$$compute_pids" || { \
		printf 'GPU 0 already has compute processes: %s\n' "$$compute_pids" >&2; exit 2; }
	stage=$$(mktemp -d /tmp/secs-tensorrt-probe.XXXXXXXX)
	run_nonce=$${stage##*.}
	download_name="secs-tensorrt-download-$$run_nonce"
	builder_name="secs-tensorrt-wheel-builder-$$run_nonce"
	extractor_name="secs-tensorrt-extractor-$$run_nonce"
	probe_name="secs-tensorrt-probe-$$run_nonce"
	container_label="secs-repro.tensorrt-probe.run=$$run_nonce"
	download_cidfile="$$stage/download.cid"
	builder_cidfile="$$stage/builder.cid"
	extractor_cidfile="$$stage/extractor.cid"
	probe_cidfile="$$stage/probe.cid"
	monitor_pid=
	active_client_pids=()
	tee_pid=
	evidence_published=
	containers_cleaned=
	final=
	close_tensorrt_log() {
		if test -z "$${tee_pid:-}"; then
			return 0
		fi
		if ! exec 1>&3 2>&4; then
			return 1
		fi
		log_status=0
		wait "$$tee_pid" || log_status=$$?
		tee_pid=
		return "$$log_status"
	}
	publish_tensorrt_evidence() {
		lane_status=$$1
		if test -n "$${evidence_published:-}" || ! compgen -G "$$stage/output/*" >/dev/null; then
			return 0
		fi
		checkout_valid=1
		if { test -f "$$stage/output/receipt.json" || \
			test -f "$$stage/output/base-reference.json"; } && \
			! require_unchanged_checkout; then
			checkout_valid=0
			if ! touch "$$stage/output/checkout-provenance-failed"; then
				printf '%s\n' 'Could not record failed checkout provenance.' >&2
				return 1
			fi
		fi
		# This file owns the outer result. A model receipt is evidence only when
		# extraction, GPU isolation, checkout provenance, and publication agree.
		if test "$$lane_status" -eq 0 && test "$$checkout_valid" -eq 1; then
			lane_result=passed
		else
			lane_result="failed exit_status=$$lane_status checkout_valid=$$checkout_valid"
		fi
		if ! printf '%s\n' "$$lane_result" > "$$stage/output/lane.status"; then
			printf '%s\n' 'Could not record the TensorRT lane outcome.' >&2
			return 1
		fi
		output_root="$(TENSORRT_OUTPUT_ROOT)"
		if ! mkdir -p "$$output_root"; then
			printf 'Could not create the TensorRT evidence root: %s\n' "$$output_root" >&2
			return 1
		fi
		if ! publish=$$(mktemp -d --tmpdir="$$output_root" .tensorrt-probe.XXXXXXXX); then
			printf '%s\n' 'Could not create a private TensorRT publication directory.' >&2
			return 1
		fi
		for artifact in run.log lane.status base-reference.json receipt.json wheelhouse.complete \
			gpu-contaminated gpu-monitor-failed checkout-provenance-failed \
			cleanup-failed logger-failed; do
			if { test "$$checkout_valid" -eq 0 || test "$$lane_status" -ne 0; } && \
				{ test "$$artifact" = receipt.json || test "$$artifact" = base-reference.json; }; then
				continue
			fi
			if test -f "$$stage/output/$$artifact"; then
				if ! install -m 0644 "$$stage/output/$$artifact" "$$publish/$$artifact"; then
					printf 'Could not stage TensorRT evidence artifact: %s\n' "$$artifact" >&2
					rm -rf -- "$$publish"
					return 1
				fi
			fi
		done
		if ! run_timestamp=$$(date -u +%Y%m%dT%H%M%SZ); then
			printf '%s\n' 'Could not timestamp TensorRT evidence.' >&2
			rm -rf -- "$$publish"
			return 1
		fi
		run_id="$$run_timestamp-$${publish##*.tensorrt-probe.}"
		final="$$output_root/$$run_id"
		if ! mv -T -- "$$publish" "$$final"; then
			printf 'Could not publish TensorRT evidence at %s.\n' "$$final" >&2
			rm -rf -- "$$publish"
			return 1
		fi
		evidence_published=1
		printf 'TensorRT probe evidence: %s\n' "$$final"
		test "$$checkout_valid" -eq 1
	}
	remove_owned_containers() {
		cleanup_failed=0
		cidfiles=("$$download_cidfile" "$$builder_cidfile" "$$extractor_cidfile" "$$probe_cidfile")
		container_names=("$$download_name" "$$builder_name" "$$extractor_name" "$$probe_name")
		for index in "$${!cidfiles[@]}"; do
			cidfile="$${cidfiles[$$index]}"
			container_name="$${container_names[$$index]}"
			container_target=
			if test -s "$$cidfile"; then
				container_id=$$(< "$$cidfile")
				if [[ "$$container_id" =~ ^[0-9a-f]{64}$$ ]]; then
					container_target="$$container_id"
				else
					printf 'Malformed container ID in %s: %s\n' "$$cidfile" "$$container_id" >&2
					printf 'malformed %s %s\n' "$$cidfile" "$$container_id" \
						>> "$$stage/output/cleanup-failed"
					cleanup_failed=1
				fi
			fi
			if test -z "$$container_target"; then
				# Docker may create the named container immediately before an
				# interrupted client writes its cidfile. The per-run label proves
				# ownership; resolving the name once to an ID keeps deletion bound
				# to that object if another container later acquires the name.
				inspection_status=0
				inspection=$$(timeout --signal=TERM --kill-after=2s 5s \
					$(DOCKER) inspect --format \
					'{{.Id}} {{index .Config.Labels "secs-repro.tensorrt-probe.run"}}' \
					"$$container_name" 2>&1) || inspection_status=$$?
				if test "$$inspection_status" -ne 0; then
					case "$$inspection" in
						*'No such object'*|*'No such container'*) continue ;;
						*)
							printf 'Could not inspect cleanup candidate %s: %s\n' \
								"$$container_name" "$$inspection" >&2
							printf 'inspect %s status=%s %s\n' "$$container_name" \
								"$$inspection_status" "$$inspection" \
								>> "$$stage/output/cleanup-failed"
							cleanup_failed=1
							continue
							;;
					esac
				fi
				read -r inspected_id owner trailing <<< "$$inspection"
				if [[ ! "$$inspected_id" =~ ^[0-9a-f]{64}$$ ]] || \
					test "$$owner" != "$$run_nonce" || test -n "$$trailing" || \
					test "$$inspected_id $$owner" != "$$inspection"; then
					printf 'Container %s did not resolve to this run ownership.\n' \
						"$$container_name" >&2
					printf 'ownership %s %s\n' "$$container_name" "$$inspection" \
						>> "$$stage/output/cleanup-failed"
					cleanup_failed=1
					continue
				fi
				container_target="$$inspected_id"
			fi
			removal_status=0
			removal_output=$$(timeout --signal=TERM --kill-after=2s 5s \
				$(DOCKER) rm --force "$$container_target" 2>&1) || removal_status=$$?
			if test "$$removal_status" -ne 0; then
				case "$$removal_output" in
					*'No such object'*|*'No such container'*) ;;
					*)
						printf 'Could not confirm cleanup of container %s: %s\n' \
							"$$container_target" "$$removal_output" >&2
						printf '%s status=%s %s\n' "$$container_target" \
							"$$removal_status" "$$removal_output" \
							>> "$$stage/output/cleanup-failed"
						cleanup_failed=1
						;;
				esac
			fi
		done
		if test "$$cleanup_failed" -eq 0; then
			containers_cleaned=1
		fi
		return "$$cleanup_failed"
	}
	cleanup_tensorrt_probe() {
		status=$$?
		trap - EXIT HUP INT TERM
		for client_pid in "$${active_client_pids[@]}"; do
			kill "$$client_pid" 2>/dev/null || true
		done
		for client_pid in "$${active_client_pids[@]}"; do
			wait "$$client_pid" 2>/dev/null || true
		done
		active_client_pids=()
		if test -n "$${monitor_pid:-}"; then
			kill "$$monitor_pid" 2>/dev/null || true
			wait "$$monitor_pid" 2>/dev/null || true
		fi
		cleanup_status=0
		if test -z "$${containers_cleaned:-}"; then
			remove_owned_containers || cleanup_status=$$?
		fi
		if test "$$status" -eq 0 && test "$$cleanup_status" -ne 0; then
			status="$$cleanup_status"
		fi
		log_status=0
		close_tensorrt_log || log_status=$$?
		if test "$$log_status" -ne 0; then
			printf 'logger_status=%s\n' "$$log_status" > "$$stage/output/logger-failed"
		fi
		if test "$$status" -eq 0 && test "$$log_status" -ne 0; then
			status="$$log_status"
		fi
		publication_status=0
		publish_tensorrt_evidence "$$status" || publication_status=$$?
		if test "$$status" -eq 0 && test "$$publication_status" -ne 0; then
			status="$$publication_status"
		fi
		rm -rf -- "$$stage"
		exit "$$status"
	}
	trap cleanup_tensorrt_probe EXIT
	trap 'exit 129' HUP
	trap 'exit 130' INT
	trap 'exit 143' TERM
	mkdir "$$stage/wheelhouse" "$$stage/output"
	chmod 2770 "$$stage/output"
	exec 3>&1 4>&2
	mkfifo "$$stage/output/run.fifo"
	tee -a "$$stage/output/run.log" < "$$stage/output/run.fifo" >&3 2>&4 &
	tee_pid=$$!
	exec > "$$stage/output/run.fifo" 2>&1
	rm "$$stage/output/run.fifo"
	# The networked phase receives only the immutable lock and its downloader.
	# Downloaded bytes are hash-checked before any downloaded code can run.
	timeout --signal=TERM --kill-after=30s 30m \
		$(DOCKER) run --init --name "$$download_name" --label "$$container_label" --pull never \
		--cidfile "$$download_cidfile" \
		--network bridge --read-only --user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 32 --cpus 1 --memory 1g --memory-swap 1g \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
		--mount type=bind,src="$(TENSORRT_DOWNLOAD_LOCK)",dst=/input/lock.json,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/stage_tensorrt_probe.py",dst=/opt/stage.py,readonly \
		--mount type=bind,src="$$download_cache",dst=/output \
		--entrypoint python "$(PYTHON_BASE)" \
		-P /opt/stage.py --lock /input/lock.json --output /output &
	active_client_pids=("$$!")
	download_status=0
	wait "$${active_client_pids[0]}" || download_status=$$?
	active_client_pids=()
	test "$$download_status" -eq 0 || exit "$$download_status"
	# TensorRT publishes its two meta-packages only as source archives. Build
	# them after hash admission, without network, model data, or GPU authority.
	timeout --signal=TERM --kill-after=30s 10m \
		$(DOCKER) run --init --name "$$builder_name" --label "$$container_label" --pull never \
		--cidfile "$$builder_cidfile" \
		--network none --read-only --user "$(HOST_UID):$(HOST_GID)" \
		--cap-drop ALL --security-opt no-new-privileges:true \
		--pids-limit 64 --cpus 2 --memory 2g --memory-swap 2g \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--tmpfs /tmp:rw,exec,nosuid,nodev,size=512m \
		--mount type=bind,src="$(TENSORRT_DOWNLOAD_LOCK)",dst=/input/lock.json,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/stage_tensorrt_probe.py",dst=/opt/stage.py,readonly \
		--mount type=bind,src="$$download_cache",dst=/downloads,readonly \
		--mount type=bind,src="$$stage/wheelhouse",dst=/wheelhouse \
		--entrypoint /bin/sh "$$package_image" -c '\
			python -P /opt/stage.py --verify-only --lock /input/lock.json --output /downloads && \
			cp /downloads/*.whl /wheelhouse/ && \
			python -m pip wheel --no-index --no-deps --no-build-isolation \
				--wheel-dir /wheelhouse /downloads/tensorrt-*.tar.gz \
				/downloads/tensorrt_cu13-*.tar.gz && \
			cd /wheelhouse && sha256sum *.whl | LC_ALL=C sort > .complete' &
	active_client_pids=("$$!")
	builder_status=0
	wait "$${active_client_pids[0]}" || builder_status=$$?
	active_client_pids=()
	test "$$builder_status" -eq 0 || exit "$$builder_status"
	probe_install_bytes=$$(python3 -P "$(REPOSITORY_ROOT)/tools/stage_tensorrt_probe.py" \
		--lock "$(TENSORRT_DOWNLOAD_LOCK)" --output "$$stage/wheelhouse" \
		--print-required-install-bytes)
	checkpoint_directory=$$(realpath -e -- "$(CHECKPOINT_DIRECTORY)")
	cache_directory=$$(realpath -e -- "$(MOLFORMER_CACHE)")
	frontend_spectrum=$$(realpath -e -- tests/fixtures/frontend/F3697-1.json)
	monitor_gpu() {
		probe_id=
		monitor_failure() {
			printf '%s\n' "$$1" >&2
			touch "$$stage/output/gpu-monitor-failed" || true
			if [[ "$${probe_id:-}" =~ ^[0-9a-f]{64}$$ ]]; then
				timeout --signal=TERM --kill-after=2s 5s \
					$(DOCKER) stop --time 3 "$$probe_id" >/dev/null 2>&1 || true
			fi
			return 2
		}
		read_probe_state() {
			if ! inspection=$$(timeout --signal=TERM --kill-after=2s 5s \
				$(DOCKER) inspect --format '{{.State.Running}}' "$$probe_id" 2>&1); then
				case "$$inspection" in
					*'No such object'*|*'No such container'*) probe_state=missing ;;
					*) monitor_failure \
						"Docker lost probe inspection authority: $$inspection" || return ;;
				esac
				return
			fi
			case "$$inspection" in
				true) probe_state=running ;;
				false) probe_state=stopped ;;
				*) monitor_failure \
					"Docker returned a malformed probe running state: $$inspection" || return ;;
			esac
		}
		while :; do
				if test -z "$$probe_id"; then
					if test ! -s "$$probe_cidfile"; then
						if test -e "$$stage/docker-client-finished"; then
							monitor_failure \
								'The probe client finished without admitting a container ID.' || return
					fi
					sleep 0.1
					continue
				fi
				probe_id=$$(< "$$probe_cidfile")
				[[ "$$probe_id" =~ ^[0-9a-f]{64}$$ ]] || \
					monitor_failure 'The probe cidfile did not contain an admitted container ID.' || return
			fi
			read_probe_state || return
			if test "$$probe_state" != running; then
				test -e "$$stage/docker-client-finished" && return 0
				sleep 0.1
				continue
			fi
			if ! container_pids=$$(timeout --signal=TERM --kill-after=2s 5s \
				$(DOCKER) top "$$probe_id" -eo pid | tail -n +2); then
				read_probe_state || return
				if test "$$probe_state" != running; then
					test -e "$$stage/docker-client-finished" && return 0
					sleep 0.1
					continue
				fi
				monitor_failure 'Docker could not inspect the probe process set.' || return
			fi
			if ! gpu_pids=$$(timeout --signal=TERM --kill-after=2s 5s \
				nvidia-smi --id="$${TENSORRT_GPU_INPUT}" \
				--query-compute-apps=pid --format=csv,noheader,nounits); then
				monitor_failure 'nvidia-smi failed during GPU isolation monitoring.' || return
			fi
			for pid in $$gpu_pids; do
				if ! grep -qx "$$pid" <<< "$$container_pids"; then
					printf 'Foreign GPU process %s contaminated GPU 0 timing.\n' "$$pid" >&2
					touch "$$stage/output/gpu-contaminated"
					timeout --signal=TERM --kill-after=2s 5s \
						$(DOCKER) stop --time 3 "$$probe_id" >/dev/null 2>&1 || true
					return 2
				fi
			done
			sleep 0.1
		done
	}
	# The FIFO lets both Docker clients be tracked by PID while the extractor
	# streams the admitted archive member without retaining another 1 GiB copy.
	mkfifo "$$stage/source.fifo"
	probe_status=0
	timeout --signal=TERM --kill-after=60s 4h \
		$(DOCKER) run --init -i --name "$$probe_name" --label "$$container_label" --pull never \
			--cidfile "$$probe_cidfile" \
			--network none --read-only --cap-drop ALL \
			--security-opt no-new-privileges:true --pids-limit 512 \
			--cpus 8 --memory "$${TENSORRT_MEMORY_INPUT}" \
			--memory-swap "$${TENSORRT_MEMORY_INPUT}" \
			--gpus "device=$${TENSORRT_GPU_INPUT}" --group-add "$(HOST_GID)" \
			--tmpfs /probe:rw,exec,nosuid,nodev,size="$$probe_install_bytes" \
			--tmpfs /tmp:rw,exec,nosuid,nodev,size="$$probe_install_bytes" \
			--tmpfs /scratch:rw,noexec,nosuid,nodev,size=8g \
			--tmpfs /derived-cache:rw,noexec,nosuid,nodev,size=512m \
			--tmpfs /modules:rw,noexec,nosuid,nodev,size=128m \
			--env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
			--env HF_MODULES_CACHE=/modules --env PYTHONDONTWRITEBYTECODE=1 \
			--mount type=bind,src="$$download_cache",dst=/downloads,readonly \
			--mount type=bind,src="$$stage/wheelhouse",dst=/wheelhouse,readonly \
			--mount type=bind,src="$$checkpoint_directory",dst=/checkpoint,readonly \
			--mount type=bind,src="$$cache_directory",dst=/base-cache,readonly \
			--mount type=bind,src="$(MOLFORMER_LOCK)",dst=/input/molformer.lock.toml,readonly \
			--mount type=bind,src="$(TENSORRT_CANDIDATE_SPEC)",dst=/input/candidates.toml,readonly \
			--mount type=bind,src="$(TENSORRT_DOWNLOAD_LOCK)",dst=/input/download-lock.json,readonly \
			--mount type=bind,src="$$frontend_spectrum",dst=/input/frontend-spectrum.json,readonly \
			--mount type=bind,src="$(REPOSITORY_ROOT)/tools/materialize_molformer_cache.py",dst=/opt/materialize.py,readonly \
			--mount type=bind,src="$(REPOSITORY_ROOT)/tools/stage_tensorrt_probe.py",dst=/opt/stage.py,readonly \
			--mount type=bind,src="$(REPOSITORY_ROOT)/tools/probe_tensorrt.py",dst=/opt/probe.py,readonly \
			--mount type=bind,src="$$stage/output",dst=/output \
			--entrypoint /bin/sh "$$package_image" -c '\
				python -P /opt/stage.py --verify-only \
					--lock /input/download-lock.json --output /downloads && \
				cd /wheelhouse && sha256sum --check .complete && \
				python -m pip install --no-index --no-deps --no-build-isolation \
					--target /probe /wheelhouse/*.whl && \
				test "$$(PYTHONPATH=/probe python -c "import torch; print(torch.__version__)")" \
					= 2.10.0+cu130 && \
				PYTHONPATH=/probe python -m pip check && \
				cp /wheelhouse/.complete /output/wheelhouse.complete && \
				PYTHONPATH=/probe python -P /opt/materialize.py --verify-only \
					--lock /input/molformer.lock.toml --output /base-cache && \
				cat > /scratch/filtered_pubchem.parquet && \
				export PYTHONPATH=/probe && \
				env HF_HUB_CACHE=/base-cache/hub HF_MODULES_CACHE=/modules/base \
					python -P /opt/probe.py "$$@" \
					--mode base-reference --report /output/base-reference.json && \
				env HF_HUB_CACHE=/derived-cache/hub HF_MODULES_CACHE=/modules/fixed \
					python -P /opt/probe.py "$$@" \
					--mode probe --report /output/receipt.json' \
			probe \
				--source /scratch/filtered_pubchem.parquet \
				--checkpoint-manifest /checkpoint/manifest.json \
				--candidate-spec /input/candidates.toml \
				--molformer-lock /input/molformer.lock.toml \
				--base-cache /base-cache --derived-cache /derived-cache \
				--fixed-model-code /downloads/modeling_molformer.py \
				--base-reference /output/base-reference.npz \
				--base-reference-report /output/base-reference.json \
				--frontend-spectrum /input/frontend-spectrum.json \
				--dependency-manifest /input/download-lock.json \
				--wheelhouse-manifest /output/wheelhouse.complete \
				--package-image-id "$$package_image" \
				--host-gpu-index "$${TENSORRT_GPU_INPUT}" \
				--host-gpu-uuid "$$gpu_uuid" \
				--repository-revision "$$repository_revision" \
			--launcher-sha256 "$$launcher_sha256" \
			--gpu-monitor-interval-seconds 0.1 < "$$stage/source.fifo" &
	probe_client_pid=$$!
	active_client_pids=("$$probe_client_pid")
	monitor_gpu &
	monitor_pid=$$!
	timeout --signal=TERM --kill-after=30s 30m \
		$(DOCKER) run --init --name "$$extractor_name" --label "$$container_label" --pull never \
		--cidfile "$$extractor_cidfile" \
		--network none --read-only --cap-drop ALL \
		--security-opt no-new-privileges:true --pids-limit 64 \
		--cpus 1 --memory 2304m --memory-swap 2304m \
		--tmpfs /scratch:rw,noexec,nosuid,nodev,size=8g \
		--mount type=bind,src="$$archive",dst=/archive/input.tar.gz,readonly \
		--mount type=bind,src="$$checkpoint_directory/checkpoint.toml",dst=/input/checkpoint.toml,readonly \
		--mount type=bind,src="$(TENSORRT_CANDIDATE_SPEC)",dst=/input/candidates.toml,readonly \
		--mount type=bind,src="$(REPOSITORY_ROOT)/tools/extract_checkpoint.py",dst=/opt/extract.py,readonly \
		--entrypoint python3 "$(TENSORRT_CHECKPOINT_IMAGE_TAG)" \
		-P /opt/extract.py --archive /archive/input.tar.gz \
		--spec /input/checkpoint.toml --member-spec /input/candidates.toml \
		--scratch-directory /scratch > "$$stage/source.fifo" &
	extractor_client_pid=$$!
	active_client_pids+=("$$extractor_client_pid")
	extractor_status=0
	wait "$$extractor_client_pid" || extractor_status=$$?
	wait "$$probe_client_pid" || probe_status=$$?
	active_client_pids=()
	if ! touch "$$stage/docker-client-finished"; then
		printf '%s\n' 'Could not signal probe-client completion to the GPU monitor.' >&2
		probe_status=125
	fi
	monitor_status=0
	wait "$$monitor_pid" || monitor_status=$$?
	monitor_pid=
	if test "$$extractor_status" -ne 0; then
		probe_status="$$extractor_status"
	fi
	if test "$$monitor_status" -ne 0 || \
		test -e "$$stage/output/gpu-contaminated" || \
		test -e "$$stage/output/gpu-monitor-failed"; then
		probe_status=125
	fi
	cleanup_status=0
	remove_owned_containers || cleanup_status=$$?
	if test "$$cleanup_status" -ne 0 && test "$$probe_status" -eq 0; then
		probe_status="$$cleanup_status"
	fi
	log_status=0
	close_tensorrt_log || log_status=$$?
	if test "$$log_status" -ne 0; then
		printf 'logger_status=%s\n' "$$log_status" > "$$stage/output/logger-failed"
		if test "$$probe_status" -eq 0; then
			probe_status="$$log_status"
		fi
	fi
	publication_status=0
	publish_tensorrt_evidence "$$probe_status" || publication_status=$$?
	if test "$$publication_status" -ne 0; then
		printf 'TensorRT evidence publication failed with status %s.\n' \
			"$$publication_status" >&2
		if test "$$probe_status" -eq 0; then
			probe_status="$$publication_status"
		fi
	fi
	if test "$$probe_status" -ne 0; then
		exit "$$probe_status"
	fi
	test -f "$$final/receipt.json" || { \
		printf '%s\n' 'TensorRT probe exited without its final receipt.' >&2; exit 2; }
	printf '%s\n' 'TensorRT probe passed.'
