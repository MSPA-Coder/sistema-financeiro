# Controle Bancário

Aplicação web para controle bancário e fluxo de caixa operacional, construída
com Django, HTMX e PostgreSQL. O ambiente operacional usa Docker.

Permite administrar contas e titulares, registrar lançamentos simples,
parcelados e recorrentes, realizar transferências internas, importar e
conciliar extratos, acompanhar saldos e projeções, anexar comprovantes, fechar
períodos e controlar acesso por usuário.

## Recursos principais

- Dashboard, próximos movimentos, posição por conta, projeções e controle
  gerencial.
- Lançamentos, parcelas, recorrências e transferências internas.
- Titulares, instituições financeiras, contas, categorias, tags, projetos e
  orçamentos.
- Importação CSV/OFX/OFC/QFX, detecção de duplicidade e conciliação manual.
- Comprovantes locais, fechamento mensal e trilha de auditoria.
- Autenticação multiusuário, perfis, permissões funcionais e acesso por
  titular.
- Backup validado e manutenção do PostgreSQL.

O produto é um controle bancário operacional. Investimentos, tributação,
patrimônio completo ou ERP não fazem parte da implementação atual.

## Requisitos

Docker Desktop com WSL 2 e um editor. Python, PostgreSQL e as ferramentas de
qualidade rodam dentro dos contêineres — não é preciso instalá-los no host.

## Primeira execução

```powershell
Copy-Item .env.docker.example .env.docker
```

Edite `.env.docker` e defina, no mínimo, `POSTGRES_PASSWORD` e
`DJANGO_SECRET_KEY`. A chave é obrigatória: sem ela a aplicação recusa subir.
Gere uma assim:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Converta os dois valores em arquivos secretos locais antes de iniciar. O script
não exibe os valores, não altera o banco e não sobrescreve arquivos existentes
sem `-Force`:

```powershell
.\scripts\provision_compose_secrets.ps1
```

O Compose monta `.secrets/django_secret_key` e `.secrets/postgres_password`
somente nos serviços que precisam deles. A pasta já é ignorada pelo Git. Para
usar outro diretório local, defina `COMPOSE_SECRETS_DIRECTORY` ao executar os
comandos do Compose; em produção, os arquivos são obrigatórios e a subida falha
se estiverem ausentes, vazios ou ilegíveis.

Se algum software de segurança intercepta HTTPS durante o build, exporte antes
a CA local:

```powershell
.\scripts\export_local_ca.ps1
```

Construa e inicie o modo padrão imutável:

```powershell
docker compose --env-file .env.docker up --build -d
```

Para desenvolvimento com montagem do código e recarga automática, o arquivo
deve ser escolhido de forma explícita:

```powershell
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml up --build -d
```

A aplicação fica em `http://127.0.0.1:<APP_PORT>`. O exemplo de ambiente e o
fallback do Compose usam `5201`. O
Compose inicia o PostgreSQL, aguarda o health check, aplica as migrations,
gera os assets e só então libera a aplicação. Dados e arquivos operacionais
ficam em volumes Docker persistentes.

Acompanhe e pare:

```powershell
docker compose --env-file .env.docker logs -f web
```

```powershell
docker compose --env-file .env.docker down
```

Não use `down -v` sem intenção explícita: essa opção remove os volumes do
PostgreSQL e dos anexos.

## Serviços do Compose

| Serviço | Papel |
|---|---|
| `postgres` | banco operacional, com health check |
| `migrate` | etapa controlada: aplica migrations e roda `collectstatic`; sai ao terminar |
| `web` | Gunicorn, usuário não-root e filesystem raiz somente leitura; logs, assets e anexos usam volumes graváveis dedicados |
| `quality` | Ruff e suíte mínima de segurança/fumaça, sob o perfil `quality` |

`compose.yaml` é o caminho padrão e executa exatamente o que foi construído na
imagem. `compose.dev.yaml` é opcional: ajusta o `web` para desenvolvimento,
monta o repositório e usa o servidor de desenvolvimento. Ele não é carregado
automaticamente; selecione-o apenas quando precisar editar código:

```powershell
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml up --build -d
```

## Configuração

| Variável | Finalidade | Padrão |
|---|---|---|
| `DJANGO_SECRET_KEY` | origem usada pelo script de provisionamento; não entra no container operacional | sem valor |
| `DJANGO_SECRET_KEY_FILE` | arquivo montado para a aplicação; obrigatório no Compose | `/run/secrets/django_secret_key` |
| `DEBUG` | modo de depuração | `False` |
| `USE_HTTPS` | cookies `Secure`, redirect HTTPS, HSTS e `X-Forwarded-Proto` | `False` |
| `ALLOWED_HOSTS` | hosts aceitos pelo Django | `localhost,127.0.0.1` |
| `POSTGRES_HOST` | host do PostgreSQL | `postgres` no Compose |
| `POSTGRES_PORT` | porta do PostgreSQL | `5432` |
| `POSTGRES_USER` | usuário do PostgreSQL | `postgres` |
| `POSTGRES_PASSWORD` | origem usada pelo script de provisionamento; não entra no container operacional | sem valor |
| `POSTGRES_PASSWORD_FILE` | arquivo montado para Django e PostgreSQL; obrigatório no Compose | `/run/secrets/postgres_password` |
| `COMPOSE_SECRETS_DIRECTORY` | diretório local dos arquivos de segredo | `.secrets` |
| `POSTGRES_DB` | banco da aplicação | `controle_bancario` |
| `APP_PORT` | porta publicada do serviço `web` | `5201` |
| `SESSION_COOKIE_AGE` | duração da sessão em segundos | `86400` |
| `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | rotação do log em arquivo | `5242880` / `5` |

Limites de upload são ajustados por `MAX_BANK_STATEMENT_SIZE_BYTES`,
`MAX_BANK_STATEMENT_ROWS` e `MAX_ATTACHMENT_SIZE_BYTES`.

`DEBUG` e `USE_HTTPS` são independentes. A instalação padrão publica em
loopback sobre HTTP: `DEBUG=False` (sem vazar traceback) e `USE_HTTPS=False`
(cookies enviados normalmente). **Exposição pública exige `USE_HTTPS=True` com
um proxy TLS à frente** — sem o proxy, o redirect leva a uma porta que ninguém
atende.

Para o piloto HTTP no VPS, mantenha `USE_HTTPS=False`, defina
`ALLOWED_HOSTS=bancario-mspa.duckdns.org,127.0.0.1` e use o Nginx como proxy
reverso. O procedimento reproduzível está em `docs/deployment-vps.md`.

## Banco e migrações

O schema é versionado por migrations Django, uma pasta `migrations/` por app.
Bancos novos são criados exclusivamente por `manage.py migrate`.

```powershell
docker compose --env-file .env.docker exec web python manage.py showmigrations
```

Depois de alterar models, gere e revise a migration correspondente:

```powershell
docker compose --env-file .env.docker run --rm web python manage.py makemigrations
```

Revise manualmente toda migration gerada. Mudanças destrutivas e
transformações de dados exigem plano, backup validado e validação explícitos.

Antes de qualquer alteração em dados reais:

```powershell
.\scripts\backup_postgres.ps1 -OutputDirectory D:\Backups\ControleBancario
```

## Verificação

O projeto mantém CI mínima no GitHub Actions: ela valida a configuração Compose
e executa Ruff com a suíte focada de segurança e fumaça (autenticação, CSRF,
autorização, cabeçalhos e bootstrap de schema) em pushes/PRs para `main` e
semanalmente. O Dependabot monitora `pip`, Docker e GitHub Actions. Isso não é
uma suíte ampla de regressão, cobertura, análise de tipos ou auditoria total.
Antes de mudar dados ou schema, faça backup; depois percorra manualmente o
fluxo alterado.

```powershell
docker compose --env-file .env.docker --profile quality run --rm quality
docker compose --env-file .env.docker run --rm web python manage.py check
```

O processo completo está em `TESTING.md`.

## Scripts operacionais

- `scripts/backup_postgres.ps1`: dump PostgreSQL validado com
  `pg_restore --list`.
- `scripts/export_local_ca.ps1`: exporta a CA local usada no build quando
  necessária.
- `scripts/package_clean_zip.py`: pacote limpo, sem dados locais e artefatos de
  desenvolvimento.

## Documentação do projeto

- `CONTEXT.md`: produto, arquitetura, invariantes e limitações conhecidas.
- `TESTING.md`: estratégia e comandos de validação.
- `AGENTS.md`: regras operacionais para manutenção assistida por IA.

Esses documentos descrevem o estado atual. O histórico de decisões e versões
pertence ao Git, não à documentação operacional.
