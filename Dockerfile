# syntax=docker/dockerfile:1.7
# Base fixada por DIGEST do indice multi-arquitetura, e nao pela tag.
#
# `python:3.14-slim` e alvo movel: a tag e reapontada a cada republicacao, e
# como o `deploy.sh` reconstroi no VPS, a imagem servida podia nascer de uma
# base diferente da que a CI varreu. Mesmo raciocinio que ja fixa as actions
# por SHA e o Trivy por digest. Digest de INDICE, para valer em amd64 e arm64.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

RUN --mount=type=secret,id=local_ca,required=false \
    if [ -f /run/secrets/local_ca ]; then \
        cp /run/secrets/local_ca /usr/local/share/ca-certificates/local-root-ca.crt; \
        update-ca-certificates; \
    fi

# Correcoes de seguranca da base e das ferramentas de empacotamento.
#
# `apt-get upgrade` porque a `python:3.14-slim` publicada carrega pacotes do
# Debian com CVE ja corrigido a montante; sem isto a correcao so chega quando a
# imagem oficial for republicada. O `setuptools` que vem na base tambem fica
# para tras -- o 70.3.0 tinha CVE-2025-47273, travessia de caminho.
#
# A atualizacao mantem a imagem alinhada aos avisos verificados pela varredura
# Trivy executada no pipeline.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip setuptools

# builder: resolve as dependencias a partir do `uv.lock`, num venv isolado.
#
# POR QUE `uv` E NAO `pip install`: o `pip` resolvia as faixas do
# `pyproject.toml` no instante do build, entao dois builds do MESMO commit
# podiam produzir imagens diferentes -- e o `deploy.sh` reconstroi no VPS, de
# modo que a imagem servida nunca foi exatamente a que a CI testou. O
# `uv.lock` fixa versao e hash SHA-256 de cada dependencia.
#
# O FLAG E `--locked`, E A DIFERENCA IMPORTA. `--frozen` apenas usa o lock sem
# olhar o `pyproject.toml`: com o lock desatualizado ele sai com SUCESSO e
# instala as versoes antigas, em silencio. `--locked` confere e REPROVA quando
# alguem edita a declaracao e esquece de rodar `uv lock`.
#
# POR QUE ISSO IMPORTA MAIS AQUI: `sharedauth` vem de repositorio Git, e o lock
# o prende ao COMMIT, nao a tag.
#
# O VENV EM `/opt/venv` SUBSTITUI O `--prefix=/install`. O prefixo era um venv
# improvisado: instalava num diretorio e o estagio final fundia tudo em
# `/usr/local`, e era por isso que a remocao do `pip` precisava cacar dentro de
# `/usr/local/lib/python*/site-packages`. Com venv de verdade a separacao e
# natural, e os tres aplicativos da frota passam a ter o mesmo formato de
# Dockerfile.
#
# `git` fica so aqui: a imagem final copia o venv e nao herda nem o binario nem
# o `.gitconfig`. O que saiu foi o token -- o `sharedauth` e publico.
FROM base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Versao fixa: o instalador que garante reprodutibilidade nao pode ser ele
# proprio uma variavel. O binario e autocontido, e o estagio `quality` o copia
# daqui em vez de reinstala-lo.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade "uv==0.12.10"

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md ./
COPY accounts ./accounts
COPY bank_statements ./bank_statements
COPY banking ./banking
COPY core ./core
COPY dashboard ./dashboard
COPY financeiro ./financeiro
COPY management ./management
COPY reports ./reports
COPY transactions ./transactions
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable


# quality: Ruff e a suite minima de seguranca, mais o servidor de
# desenvolvimento montado por compose.dev.yaml. Nunca e o estagio
# publicado: `compose.yaml` usa `runtime` para migrate e web.
#
# Este projeto instala o pacote **sem** o extra `[flask]` do `sharedauth`: so o
# nucleo, que e Python puro (`security` e `formatting`). Pedir o extra traria
# Flask, Flask-WTF e Flask-Limiter para dentro de uma imagem Django -- e o lock
# registra essa escolha.
FROM base AS quality

# O `uv` vem pronto do `builder`: e binario autocontido, e copia-lo custa
# menos que reinstalar um gerenciador de pacotes.
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# O codigo inteiro vem antes da instalacao porque a suite exercita a aplicacao
# real, e nao so o pacote instalado.
COPY . .

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# `--extra dev` acrescenta as ferramentas de teste ao MESMO conjunto que o
# runtime instala: a suite tem de medir o que a imagem servida usa, e o lock
# garante que sejam as mesmas versoes.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --extra dev

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


# Estagio de producao: somente dependencias e arquivos de runtime. Codigo fica
# como root:root e legivel pelo usuario da aplicacao; apenas volumes declarados
# pelo Compose sao gravaveis em execucao.
FROM base AS runtime

# O venv vem do `builder`; sem isto, `python`, `gunicorn` e `manage.py`
# resolveriam para o interpretador do sistema, que nao tem as dependencias.
ENV PATH="/opt/venv/bin:${PATH}"

RUN groupadd --system app \
    && useradd --system --gid app --no-create-home --home-dir /workspace app \
    && mkdir -p /workspace/staticfiles /workspace/logs /workspace/media \
    && chown app:app /workspace/staticfiles /workspace/logs /workspace/media

COPY --from=builder /opt/venv /opt/venv
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

# Tira `pip` e `setuptools` da imagem SERVIDA.
#
# Sao ferramenta de build e nao tem uso aqui -- e o mesmo raciocinio que ja
# mantem `gcc`, `make` e `wget` fora do runtime, o que os testes de contrato
# deste projeto verificam.
#
# A remocao reduz a superficie de vulnerabilidades do runtime, inclusive a de
# pacotes vendorizados por `pip`, sem retirar ferramentas usadas pela aplicacao.
#
# A ultima linha e a propria verificacao: se `pip` continuar no PATH, o build
# falha aqui em vez de entregar uma imagem que so parece limpa.
#
# O `python -m pip check` que abria este bloco saiu com a adocao do `uv`: ele
# perguntava se as dependencias instaladas sao mutuamente compativeis, e o venv
# criado pelo `uv` nem tem `pip` para responder. A pergunta tambem deixou de
# fazer sentido -- o conjunto vem resolvido do lock, entao a coerencia e
# garantida na resolucao, e nao conferida depois da instalacao.
RUN set -eu; \
    for raiz in /usr/local/lib/python*/site-packages /opt/venv/lib/python*/site-packages; do \
      [ -d "$raiz" ] || continue; \
      rm -rf "$raiz"/pip "$raiz"/pip-*.dist-info \
             "$raiz"/setuptools "$raiz"/setuptools-*.dist-info \
             "$raiz"/pkg_resources "$raiz"/_distutils_hack \
             "$raiz"/distutils-precedence.pth \
             "$raiz"/wheel "$raiz"/wheel-*.dist-info; \
    done; \
    rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
          /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.*; \
    ! command -v pip

USER app

ENV DJANGO_SETTINGS_MODULE=financeiro.settings \
    PYTHONPATH=/workspace

EXPOSE 8000

# O health check e declarado no compose.yaml, nao aqui: ele depende do
# cabecalho X-Forwarded-Proto que o proxy reverso injeta, contexto que a
# imagem nao tem como conhecer. Os outros tres projetos seguem a mesma regra.

# Default command for production (can be overridden by docker-compose)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--worker-class", "gthread", "--timeout", "60", "--no-control-socket", "financeiro.wsgi:application"]
