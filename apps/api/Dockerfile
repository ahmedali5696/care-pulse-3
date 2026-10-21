FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d AS python-base

WORKDIR /srv

# Bootstrap the approved pip wheel by its published SHA-256 before any dependency download.
RUN python -m pip install "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -c "import pip; assert pip.__version__ == '26.2.2'"

COPY requirements.lock /srv/requirements.lock
RUN python -m pip install --require-hashes --only-binary=:all: -r /srv/requirements.lock \
    && python -m pip check

COPY --chown=65532:65532 apps/api/carepulse/ /srv/apps/api/carepulse/
COPY --chown=65532:65532 apps/worker/main.py /srv/apps/worker/main.py
RUN mkdir -p /srv/private-files && chown 65532:65532 /srv/private-files

ENV PYTHONPATH=/srv/apps/api \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532

FROM python-base AS api
CMD ["python", "-m", "uvicorn", "carepulse.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python-base AS worker
CMD ["python", "apps/worker/main.py"]

FROM python-base AS test
# T009 adds the integration suite; this target keeps the same locked Python image.
COPY --chown=65532:65532 . /srv/
CMD ["python", "-m", "pytest", "tests/integration"]
