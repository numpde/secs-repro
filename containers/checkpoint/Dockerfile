ARG PYTHON_BASE
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONUNBUFFERED=1

COPY --chmod=0555 tools/extract_checkpoint.py /usr/local/libexec/extract_checkpoint.py

USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-P", "/usr/local/libexec/extract_checkpoint.py"]
