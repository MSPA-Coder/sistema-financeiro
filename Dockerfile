# syntax=docker/dockerfile:1.7
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

RUN --mount=type=secret,id=local_ca,required=false \
    if [ -f /run/secrets/local_ca ]; then \
        cp /run/secrets/local_ca /usr/local/share/ca-certificates/local-root-ca.crt; \
    fi \
    && apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client curl \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./


# quality: Ruff e a suite minima de seguranca, mais o servidor de
# desenvolvimento montado por compose.override.yaml. Nunca e o estagio
# publicado: `compose.yaml` usa `runtime` para migrate e web.
FROM base AS quality

RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY . .

ENV DJANGO_SETTINGS_MODULE=financeiro.settings \
    PYTHONPATH=/workspace \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"

# Sem o manifesto, renderizar qualquer template que use `{% static %}` estoura
# com "Missing staticfiles manifest entry" -- a suite ficaria sem conseguir
# exercitar a tela de login. As credenciais sao de build, nao de execucao.
RUN DJANGO_SECRET_KEY=build-only-nao-usada-em-execucao \
    POSTGRES_PASSWORD=build-only \
    python manage.py collectstatic --noinput --clear >/dev/null

EXPOSE 8000

CMD ["sh", "-c", "ruff check . && pytest"]


# Estagio de producao (default quando nenhum --target/target e informado):
# somente dependencias de runtime (requirements.txt) e usuario nao-root.
FROM base AS runtime

RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --no-create-home --home-dir /workspace app \
    && mkdir -p /workspace/staticfiles /workspace/logs \
    && chown -R app:app /workspace

COPY --chown=app:app . .

USER app

ENV DJANGO_SETTINGS_MODULE=financeiro.settings \
    PYTHONPATH=/workspace

EXPOSE 8000

# Health check endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/', timeout=5)" || exit 1

# Default command for production (can be overridden by docker-compose)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--worker-class", "gthread", "--timeout", "60", "financeiro.wsgi:application"]
