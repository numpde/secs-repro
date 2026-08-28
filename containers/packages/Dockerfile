ARG PYTHON_BASE
FROM ${PYTHON_BASE}

ARG PACKAGES_INPUT_ID
ARG PACKAGES_VARIANT

LABEL io.secs-repro.packages.input-id="${PACKAGES_INPUT_ID}" \
      io.secs-repro.packages.variant="${PACKAGES_VARIANT}"

COPY requirements/packages-${PACKAGES_VARIANT}.lock /opt/packages/requirements.lock
COPY --from=wheelhouse . /opt/packages/wheelhouse/

RUN cd /opt/packages/wheelhouse \
    && sha256sum --check .complete \
    && python -m pip install --no-index --require-hashes \
        --find-links /opt/packages/wheelhouse \
        --no-cache-dir -r /opt/packages/requirements.lock \
    && rm -rf /opt/packages/wheelhouse

COPY secs/ /opt/source/secs/
COPY pyproject.toml /opt/source/secs-inference/pyproject.toml
COPY src/ /opt/source/secs-inference/src/

RUN python -m pip install --no-index --no-deps --no-build-isolation \
        /opt/source/secs /opt/source/secs-inference \
    && rm -rf /opt/source \
    && python -m pip check

RUN useradd --no-create-home --uid 65532 secs
USER 65532:65532
