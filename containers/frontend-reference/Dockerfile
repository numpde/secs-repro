ARG NODE_IMAGE
FROM ${NODE_IMAGE}

ARG REFERENCE_BUILD_ID
ARG REFERENCE_LOCK_ID
ARG INPUT_REFERENCE_BUILD_ID
ENV SECS_FRONTEND_REFERENCE_BUILD_ID=${REFERENCE_BUILD_ID}
ENV SECS_FRONTEND_REFERENCE_LOCK_ID=${REFERENCE_LOCK_ID}
ENV SECS_INPUT_REFERENCE_BUILD_ID=${INPUT_REFERENCE_BUILD_ID}

WORKDIR /opt/frontend

COPY --from=frontend package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev \
    && npm cache clean --force

COPY --from=frontend src/ ./src/
COPY tools/generate_frontend_reference.ts /opt/reference/generate.ts
COPY tools/generate_input_fixtures.mjs tools/generate_nmrium_fixtures.mjs /opt/reference/
COPY contracts/upstream/frontend_reference.json /opt/reference/frontend_reference.json

USER node
ENTRYPOINT ["node", "/opt/reference/generate.ts"]
