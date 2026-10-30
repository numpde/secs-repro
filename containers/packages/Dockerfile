ARG PYTHON_BASE
FROM ${PYTHON_BASE}

ARG PACKAGES_INPUT_ID
ARG PACKAGES_VARIANT

LABEL io.secs-repro.packages.input-id="${PACKAGES_INPUT_ID}" \
      io.secs-repro.packages.variant="${PACKAGES_VARIANT}" \
      io.secs-repro.worker.device-contract="explicit-v1" \
      io.secs-repro.worker.mock-index="1"

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

COPY requirements/native.lock /opt/native-install/requirements.lock
COPY --from=native_wheelhouse . /opt/native-install/wheelhouse/
RUN cd /opt/native-install/wheelhouse \
    && sha256sum --check .complete \
    && python -I -m pip install --no-index --require-hashes \
        --find-links /opt/native-install/wheelhouse \
        --no-cache-dir -r /opt/native-install/requirements.lock \
    && python -I -m pip check \
    && rm -rf /opt/native-install

ENV PROVIDER_CORE_IN_CONTAINER=1

RUN useradd --no-create-home --uid 65532 secs
USER 65532:65532
