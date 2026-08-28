FROM docker.io/library/python:3.11.14-slim-bookworm@sha256:83f339c1be6340ae1096010fdccf6552ac932d8f410d45d206014916bdf37e48

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONUNBUFFERED=1

COPY --chmod=0555 tools/extract_checkpoint.py /usr/local/libexec/extract_checkpoint.py

USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-P", "/usr/local/libexec/extract_checkpoint.py"]
