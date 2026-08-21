# syntax=docker/dockerfile:1.7
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

RUN python -m pip install --no-cache-dir --upgrade pip

# quality: Ruff e a suite minima de seguranca, mais o servidor de
# desenvolvimento montado por compose.dev.yaml. Nunca e o estagio
# publicado: `compose.yaml` usa `runtime` para migrate e web.
FROM base AS quality

# `requirements.txt` inclui `sharedauth` de um repositorio Git privado
# (github.com/MSPA-Coder/SharedAuth) -- pip precisa de `git` no PATH e de
# credencial para HTTPS. O secret `github_token` (BuildKit, nunca vira camada
# da imagem) autentica so para o RUN que instala; `git config --unset` na
# mesma instrucao remove o token do `.gitconfig` antes de commitar a camada.
#
# Este projeto instala o pacote **sem** o extra `[flask]`: so o nucleo, que e
# Python puro (`security` e `formatting`). Pedir o extra aqui traria Flask,
# Flask-WTF e Flask-Limiter para dentro de uma imagem Django.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./

RUN --mount=type=secret,id=local_ca,required=false \
    if [ -f /run/secrets/local_ca ]; then \
        cp /run/secrets/local_ca /usr/local/share/ca-certificates/local-root-ca.crt; \
        update-ca-certificates; \
    fi
RUN --mount=type=secret,id=github_token \
    git config --global url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf "https://github.com/" \
    && python -m pip install --no-cache-dir -r requirements-dev.txt \
    && git config --global --unset url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf
COPY . .

# `logs/` e estado local e fica fora do contexto de build. O estagio de
# qualidade ainda precisa do diretorio para configurar o handler Django.
RUN mkdir -p /workspace/logs

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


# Instala as dependencias de producao fora da imagem final. O certificado
# opcional atende apenas ao download durante o build e nao e copiado ao runtime.
FROM base AS runtime-dependencies

# `git` fica so neste estagio intermediario: a imagem final copia `/install` e
# nao herda nem o binario nem o `.gitconfig`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN --mount=type=secret,id=local_ca,required=false \
    if [ -f /run/secrets/local_ca ]; then \
        cp /run/secrets/local_ca /usr/local/share/ca-certificates/local-root-ca.crt; \
        update-ca-certificates; \
    fi
RUN --mount=type=secret,id=github_token \
    git config --global url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf "https://github.com/" \
    && python -m pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && git config --global --unset url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf


# Estagio de producao: somente dependencias e arquivos de runtime. Codigo fica
# como root:root e legivel pelo usuario da aplicacao; apenas volumes declarados
# pelo Compose sao gravaveis em execucao.
FROM base AS runtime

RUN groupadd --system app \
    && useradd --system --gid app --no-create-home --home-dir /workspace app \
    && mkdir -p /workspace/staticfiles /workspace/logs /workspace/media \
    && chown app:app /workspace/staticfiles /workspace/logs /workspace/media

COPY --from=runtime-dependencies /install /usr/local
COPY --chmod=755 manage.py ./manage.py
COPY accounts ./accounts
COPY bank_statements ./bank_statements
COPY banking ./banking
COPY core ./core
COPY dashboard ./dashboard
COPY financeiro ./financeiro
COPY management ./management
COPY reports ./reports
COPY transactions ./transactions
COPY templates ./templates
COPY static ./static

USER app

ENV DJANGO_SETTINGS_MODULE=financeiro.settings \
    PYTHONPATH=/workspace

EXPOSE 8000

# O health check e declarado no compose.yaml, nao aqui: ele depende do
# cabecalho X-Forwarded-Proto que o proxy reverso injeta, contexto que a
# imagem nao tem como conhecer. Os outros tres projetos seguem a mesma regra.

# Default command for production (can be overridden by docker-compose)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--worker-class", "gthread", "--timeout", "60", "--no-control-socket", "financeiro.wsgi:application"]
