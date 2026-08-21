# Controle Bancário — orientações de manutenção

## Escopo e fontes de verdade

Este é um controle bancário multiusuário em Django, HTMX e PostgreSQL. Docker
Compose é a interface operacional: não instale Python, PostgreSQL, linters ou
test runners no host para contornar uma falha.

Antes de alterar, leia a fonte pertinente e confirme o comportamento no código:

- `CONTEXT.md`: arquitetura, domínio, segurança e decisões de produto.
- `README.md`: instalação, operação e variáveis de ambiente.
- `TESTING.md`: comandos e validação proporcional.
- `compose.yaml`, `compose.dev.yaml` e `Dockerfile`: serviços e imagem
  efetivamente executados.
- Migrations e testes: fonte de verdade para schema e controles automatizados.

Não crie uma camada de repositories: consultas vivem nos services e, quando
triviais, na view. O fluxo usual é `urls -> views -> services -> models`.
Preserve alterações locais não relacionadas.

## Operação e comandos válidos

Copie `.env.docker.example` para `.env.docker`, mantenha-o fora do Git e nunca
imprima seus valores. Em seguida execute
`.\scripts\provision_compose_secrets.ps1`: ele cria os arquivos ignorados em
`.secrets/` a partir do ambiente, sem mostrar conteúdo e sem sobrescrever sem
`-Force`. O Compose operacional exige esses arquivos para `DJANGO_SECRET_KEY` e
`POSTGRES_PASSWORD`, montados em `/run/secrets`; ausência, vazio ou erro de
leitura falham ao subir. `POSTGRES_USER` e `POSTGRES_DB` têm defaults no
Compose. Execução Django fora do Compose pode fornecer segredos diretamente de
forma explícita, mas não é o caminho operacional.

```powershell
# Caminho padrão: imagem construída, imutável e mais próximo da operação.
docker compose --env-file .env.docker -f compose.yaml up --build -d

# Desenvolvimento explícito: monta o código e usa runserver.
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml up --build -d

# Verificação Django e geração de migration no runtime construído.
docker compose --env-file .env.docker -f compose.yaml run --rm web python manage.py check
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml run --rm web python manage.py makemigrations

# Ruff e suíte mínima; o serviço quality é explícito e não depende do override.
docker compose --env-file .env.docker -f compose.yaml --profile quality run --rm quality
```

`web` é o único serviço de aplicação; `migrate` aplica migrations e
`collectstatic` antes de o `web` aceitar tráfego; `quality` executa Ruff e
pytest. Não há serviço `app`.

## Dados, segurança e riscos destrutivos

PostgreSQL é a fonte de verdade. Bancos novos nascem por `manage.py migrate`;
não use `create_all`, SQLite ou dump como bootstrap. Cada alteração de schema
exige migration Django revisada. Antes de mudança destrutiva, conversão de
dados, adoção de schema ou manutenção que possa afetar dados reais, faça backup
validado e obtenha autorização explícita, pelo BackupRestore (projeto irmão):

```powershell
python cli.py backup --projeto controle_bancario --tipos banco
```

O BackupRestore produz dump customizado e confere `pg_restore --list` antes de
catalogar; isso não prova uma restauração completa — para isso existe
`cli.py ensaio`. Restauração é administrativa, com a aplicação parada e
procedimento testado. Não execute `docker compose down -v`, não mova volumes
ou anexos reais e não apague backups sem autorização inequívoca.

Autenticação é obrigatória; autorização fica no servidor; escritas exigem CSRF.
Preserve CSP sem código inline, validação de uploads, logs sem dados sensíveis,
cookies `HttpOnly`/`SameSite=Lax` e `Secure` quando `USE_HTTPS=True`. Exposição
pública requer proxy TLS e configuração deliberada de `USE_HTTPS` e hosts. O
piso de senha (mínimo 8 caracteres, duplicado em
`accounts/password_validators.py` e `core/services.py` — os dois precisam
mudar juntos) segue a mesma política dos três apps Flask do mantenedor.

Este projeto **compartilha código** com eles em dois pontos, desde a v0.2.0 do
[SharedAuth](https://github.com/MSPA-Coder/SharedAuth): os valores dos
cabeçalhos defensivos e da CSP (`core/security.py`) e a formatação de números
em pt-BR (`core/templatetags/money_filters.py`). Instala **só o núcleo** do
pacote, sem o extra `[flask]` — o núcleo é Python puro e não arrasta um
framework web que este projeto não usa; pedir o extra aqui seria erro. A
biblioteca não aplica nada: quem aplica os cabeçalhos continua sendo o
middleware deste projeto. Autenticação, permissões e o modelo de usuário
seguem inteiramente próprios — Django, sem nada em comum com os apps Flask.

## Invariantes essenciais

- Valores financeiros usam `Decimal`; lançamentos armazenam valor positivo e o
  tipo define o efeito no saldo.
- Status são `a_vencer`, `vencidos`, `realizado` e `cancelado`.
- Transferências internas mantêm contrapartes e saldos consistentes, mas não
  entram como receita ou despesa gerencial.
- Fechamento mensal bloqueia mutações e conciliações do período; reabertura é
  explícita e auditável.
- Operações compostas são atômicas; services delimitam transações.
- Toda data/hora persistida é timezone-aware (`timestamptz`).
- Acesso por titular e permissões são controles de servidor; preferências de
  visibilidade não são permissões.

Os detalhes de domínio, inclusive extratos, projeções e peculiaridades de
interface, permanecem em `CONTEXT.md`.

## Validação proporcional

Mudança documental dispensa testes salvo se afetar automação. Para template,
HTMX ou JavaScript, percorra a tela afetada. Para regra, rota ou serviço,
percorra o fluxo completo com dados representativos. Para autenticação,
autorização, sessão ou CSRF, execute integralmente `quality`. Para schema,
backup validado, revisão da migration e bootstrap em PostgreSQL vazio são
obrigatórios. Para dependências, Dockerfile ou Compose, reconstrua a imagem e
suba a pilha completa.

A suíte focada em `tests/` cobre cabeçalhos/CSP, autenticação, CSRF,
autorização e grafo de migrations. A CI mínima executa essa mesma sequência
em pushes/PRs para `main` e semanalmente; o Dependabot acompanha `pip`, Docker
e GitHub Actions em atualizações minor/patch agrupadas. Isso não substitui o
bootstrap real ou a verificação manual, nem constitui cobertura ampla, análise
de tipos ou auditoria total. Ao concluir, informe comandos executados,
resultado e controles omitidos com o motivo.

## Implantação em produção

O sistema roda em um VPS Oracle atrás de Nginx com TLS, em
`https://bancario-mspa.duckdns.org`, a partir de
`/home/ubuntu/apps/controle-bancario`.

O código do servidor é espelho do `main`, em sentido único: desenvolvimento na
máquina local, commit, push ao GitHub, e só então implantação. **Não edite
código, não commite e não faça merge no VPS** — `~/deploy.sh bancario` aborta ao
encontrar árvore suja, e a *deploy key* do servidor é somente leitura, então um
push de lá falharia de qualquer forma.

`.secrets/` (`postgres_password`, `django_secret_key`) e `.certs/` não são
versionados e vivem apenas no servidor; um reclone precisa restaurá-los, ou o
build falha e o banco fica inacessível. Os dados ficam nos volumes
`controle-bancario_postgres_data` e `controle-bancario_media_volume`, fora da
pasta do código: substituir o diretório do projeto não os afeta. A base do VPS é
independente da local. Consulte `docs/deployment-vps.md` antes de qualquer
operação no VPS.

## Política de versões

**Faixas de dependência: alargue o teto, mantenha o piso.** O Dependabot roda
com `versioning-strategy: widen`. Quando ele propuser elevar o mínimo, aproveite
apenas a parte que alarga o teto e recuse a que sobe o piso. O piso registra a
compatibilidade mínima efetivamente verificada, não a versão mais nova
disponível: elevá-lo declara uma incompatibilidade que ninguém comprovou e não
muda nada do que é instalado, porque o pip já resolve para a versão mais nova
permitida pela faixa.


Compatibilidade mínima suportada: Python 3.14, PostgreSQL 17 e Django 5.2;
as faixas completas de bibliotecas estão em `requirements*.txt`. Atualmente
testa-se a família das imagens `python:3.14-slim` e `postgres:17-alpine` com as
versões resolvidas dentro dessas faixas. Não há lock de dependências nem patch
de imagem registrado: uma reconstrução pode resolver patches mais novos.

Evolua versões deliberadamente: avalie compatibilidade e notas de migração,
atualize imagem/faixas e documentação como uma única mudança, reconstrua do
zero, rode `quality` e valide o fluxo afetado. Não trate uma versão observada
num contêiner local como nova mínima suportada sem registrá-la e testá-la.
