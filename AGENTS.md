# Controle Bancário — orientações de manutenção

## Escopo e fontes de verdade

Este é um controle bancário multiusuário em Django, HTMX e PostgreSQL. Docker
Compose é a interface operacional: não instale Python, PostgreSQL, linters ou
test runners no host para contornar uma falha.

Antes de alterar, leia a fonte pertinente e confirme o comportamento no código:

- `CONTEXT.md`: arquitetura, domínio, segurança e decisões de produto.
- `README.md`: instalação, operação e variáveis de ambiente.
- `TESTING.md`: comandos e validação proporcional.
- `compose.yaml`, `compose.override.yaml` e `Dockerfile`: serviços e imagem
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
# Desenvolvimento: o override monta o código e usa runserver.
docker compose --env-file .env.docker up --build -d

# Caminho construído, sem override de desenvolvimento.
docker compose --env-file .env.docker -f compose.yaml up --build -d

# Verificação Django e geração de migration, com o código montado pelo override.
docker compose --env-file .env.docker run --rm web python manage.py check
docker compose --env-file .env.docker run --rm web python manage.py makemigrations

# Ruff e suíte mínima; o serviço quality é explícito e não depende do override.
docker compose --env-file .env.docker --profile quality run --rm quality
```

`web` é o único serviço de aplicação; `migrate` aplica migrations e
`collectstatic` antes de o `web` aceitar tráfego; `quality` executa Ruff e
pytest. Não há serviço `app`.

## Dados, segurança e riscos destrutivos

PostgreSQL é a fonte de verdade. Bancos novos nascem por `manage.py migrate`;
não use `create_all`, SQLite ou dump como bootstrap. Cada alteração de schema
exige migration Django revisada. Antes de mudança destrutiva, conversão de
dados, adoção de schema ou manutenção que possa afetar dados reais, faça backup
validado e obtenha autorização explícita:

```powershell
.\scripts\backup_postgres.ps1 -OutputDirectory D:\Backups\ControleBancario
```

O script produz dump customizado e confere `pg_restore --list`; isso não prova
uma restauração completa. Restauração é administrativa, com a aplicação parada
e procedimento testado. Não execute `docker compose down -v`, não mova volumes
ou anexos reais e não apague backups sem autorização inequívoca.

Autenticação é obrigatória; autorização fica no servidor; escritas exigem CSRF.
Preserve CSP sem código inline, validação de uploads, logs sem dados sensíveis,
cookies `HttpOnly`/`SameSite=Lax` e `Secure` quando `USE_HTTPS=True`. Exposição
pública requer proxy TLS e configuração deliberada de `USE_HTTPS` e hosts.

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

A suíte mínima em `tests/` cobre cabeçalhos/CSP, autenticação, CSRF,
autorização e grafo de migrations; ela não substitui o bootstrap real nem a
verificação manual. Ao concluir, informe comandos executados, resultado e
controles omitidos com o motivo.

## Política de versões

Compatibilidade mínima suportada: Python 3.14, PostgreSQL 17 e Django 5.2;
as faixas completas de bibliotecas estão em `requirements*.txt`. Atualmente
testa-se a família das imagens `python:3.14-slim` e `postgres:17-alpine` com as
versões resolvidas dentro dessas faixas. Não há lock de dependências nem patch
de imagem registrado: uma reconstrução pode resolver patches mais novos.

Evolua versões deliberadamente: avalie compatibilidade e notas de migração,
atualize imagem/faixas e documentação como uma única mudança, reconstrua do
zero, rode `quality` e valide o fluxo afetado. Não trate uma versão observada
num contêiner local como nova mínima suportada sem registrá-la e testá-la.
