FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d AS python-base

WORKDIR /srv

# Bootstrap the approved pip wheel by its published SHA-256 before any dependency download.
RUN python -m pip install "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -c "import pip; assert pip.__version__ == '26.2.2'"

COPY requirements.lock /srv/requirements.lock
RUN python -m pip install --require-hashes --only-binary=:all: -r /srv/requirements.lock \
    && python -m pip check

COPY --chown=65532:65532 apps/api/carepulse/ /srv/apps/api/carepulse/
COPY --chown=65532:65532 apps/worker/ /srv/apps/worker/
RUN mkdir -p /srv/private-files && chown 65532:65532 /srv/private-files

ENV PYTHONPATH=/srv/apps/api:/srv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532

FROM python-base AS api
CMD ["python", "-m", "uvicorn", "carepulse.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Build Nodemailer's already locked closure separately. The final worker does
# not inherit Next, Better Auth, pnpm or any other web runtime dependency.
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS mail-node
WORKDIR /workspace
RUN npm install --global --ignore-scripts --no-audit --no-fund pnpm@10.34.7
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile

FROM python-base AS worker
COPY --from=mail-node /usr/local/bin/node /usr/local/bin/node
COPY --from=mail-node /workspace/node_modules/.pnpm/nodemailer@10.1.7/node_modules/nodemailer /srv/apps/web/mail/node_modules/nodemailer
COPY --chown=65532:65532 apps/web/mail/config.ts apps/web/mail/transport.ts apps/web/mail/templates.ts apps/web/mail/delivery-material.ts apps/web/mail/send-job.ts /srv/apps/web/mail/
RUN node --version
CMD ["python", "apps/worker/main.py"]

FROM worker AS sender
ENTRYPOINT ["python", "-m", "carepulse.notifications.email_sender"]
CMD []

FROM python-base AS operator
ENTRYPOINT ["python", "-m", "carepulse.provisioning.cli"]
CMD []

FROM python-base AS test
# T009 adds the integration suite; this target keeps the same locked Python image.
COPY --from=mail-node /usr/local/bin/node /usr/local/bin/node
COPY --chown=65532:65532 . /srv/
CMD ["python", "-m", "pytest", "tests/integration"]
