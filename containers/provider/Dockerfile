ARG PYTHON_BASE
FROM ${PYTHON_BASE}

ARG PROVIDER_INPUT_ID
LABEL io.secs-repro.provider.input-id="${PROVIDER_INPUT_ID}"

COPY requirements/provider.lock /opt/provider/requirements.lock
COPY --from=wheelhouse . /opt/provider/wheelhouse/
RUN cd /opt/provider/wheelhouse \
    && sha256sum --check .complete \
    && python -m pip install --no-index --require-hashes \
        --find-links /opt/provider/wheelhouse \
        --no-cache-dir -r /opt/provider/requirements.lock \
    && rm -rf /opt/provider/wheelhouse

COPY src/secs_inference/__init__.py /opt/secs_inference/__init__.py
COPY src/secs_inference/provider/ /opt/secs_inference/provider/
ENV PYTHONPATH=/opt
RUN python -P -c 'import secs_inference.provider.main'

RUN useradd --no-create-home --uid 65532 provider
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-P", "-m", "secs_inference.provider.main"]
