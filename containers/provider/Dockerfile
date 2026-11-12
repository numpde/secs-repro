ARG PYTHON_BASE
FROM ${PYTHON_BASE} AS installer
ENV NMR_PROVIDER_TOOLS_IN_CONTAINER=1
COPY tools/bootstrap/acquisition/acquisition_tool/installer/tools/ /opt/provider-tools/installer/tools/
COPY tools/bootstrap/acquisition/acquisition_tool/installer/requirements/installer-release.json /opt/provider-tools/installer/release.json
COPY tools/bootstrap/acquisition/acquisition_tool/installer/wheels/ /opt/provider-tools/installer/wheels/
RUN cd /opt/provider-tools/installer \
    && python -B -m tools.installer --record release.json --pool wheels

FROM scratch AS provider_wheels
COPY provider_wheels/ /

FROM installer

ARG PROVIDER_INPUT_ID
LABEL io.secs-repro.provider.input-id="${PROVIDER_INPUT_ID}" \
      io.secs-repro.provider.mock-index="1" \
      io.secs-repro.provider.mock-runner="1"

COPY requirements/provider.lock /opt/provider/requirements.lock
RUN --mount=type=bind,from=provider_wheels,target=/opt/provider/wheelhouse \
    cd /opt/provider/wheelhouse \
    && sha256sum --check .complete \
    && python -m pip install --no-index --require-hashes \
        --find-links /opt/provider/wheelhouse \
        --no-cache-dir -r /opt/provider/requirements.lock \
    && python -m pip check \
    && rm /opt/provider/requirements.lock

COPY tools/copy_deployment_secrets.py /opt/provider/copy_deployment_secrets.py
COPY src/secs_inference/__init__.py /opt/secs_inference/__init__.py
COPY src/secs_inference/model_failure.py /opt/secs_inference/model_failure.py
COPY src/secs_inference/provider/ /opt/secs_inference/provider/
RUN python -c 'import pathlib, site; (pathlib.Path(site.getsitepackages()[0]) / "secs-provider.pth").write_text("/opt\n")'
ENV PYTHONDONTWRITEBYTECODE=1 \
    PROVIDER_CORE_IN_CONTAINER=1 \
    PYTHONPATH=/opt \
    PYTHONSAFEPATH=1
RUN python -c 'import provider_core.controller; import secs_inference.provider.main'

RUN useradd --no-create-home --uid 65532 provider
ENV HOME=/tmp \
    PYTHONUNBUFFERED=1
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "secs_inference.provider.main"]
