ARG NODE_IMAGE
FROM ${NODE_IMAGE}

WORKDIR /opt/frontend

COPY --from=frontend package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev \
    && npm cache clean --force

COPY --from=frontend src/ ./src/
COPY tools/generate_frontend_reference.ts /opt/reference/generate.ts

USER node
ENTRYPOINT ["node", "/opt/reference/generate.ts"]
