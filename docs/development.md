# Desenvolvimento e validação

## Ambiente local

Use Docker Compose como interface do projeto. Não instale Python, PostgreSQL,
Ruff, pytest ou dependências da aplicação no host.

Prepare `.env.docker`, provisione os secrets de Django e PostgreSQL e forneça o
token de leitura de SharedAuth conforme o [README](../README.md). Para iniciar
o modo de desenvolvimento:

```powershell
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml up --build -d
```

O override monta o código do host em `/workspace` e executa `runserver`. Para
validar exatamente a imagem operacional, use apenas `compose.yaml`.

## Comandos reproduzíveis

Verificação da configuração Django:

```powershell
docker compose --env-file .env.docker -f compose.yaml run --rm web python manage.py check
```

Ruff e suíte automatizada:

```powershell
docker compose --env-file .env.docker -f compose.yaml --profile quality run --rm quality
```

Inspeção e geração de migrations:

```powershell
docker compose --env-file .env.docker -f compose.yaml run --rm web python manage.py showmigrations
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml run --rm web python manage.py makemigrations
```

Revise toda migration gerada. Uma mudança de schema só está pronta depois de
validar `manage.py migrate` em PostgreSQL vazio e no caminho de atualização
aplicável.

## Validação proporcional

- documentação: `git diff --check`, links/caminhos e comandos documentados;
- template, HTMX ou JavaScript: fluxo afetado no navegador;
- regra, view ou service: testes focados e fluxo completo com dados
  representativos;
- autenticação, autorização, sessão ou CSRF: serviço `quality` completo;
- schema ou transformação de dados: backup validado, revisão da migration e
  bootstrap em PostgreSQL vazio;
- dependências, Dockerfile ou Compose: build sem cache quando pertinente,
  `docker compose config`, serviço `quality` e smoke test da pilha.

A CI valida o Compose, executa Ruff e pytest, audita as dependências Python,
varre a imagem operacional e confere a fronteira de escrita do runtime. Isso
não substitui a validação manual do fluxo alterado nem um ensaio de restauração.

## Antes de alterar dados reais

Leia [Operação, dados e backup](operations.md), faça o backup correspondente e
confirme que ele é restaurável. Não use `down -v`, não remova volumes e não
substitua arquivos de mídia sem autorização explícita.
