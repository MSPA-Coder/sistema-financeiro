# Controle Bancário

Aplicação web de controle bancário e fluxo de caixa, usada atualmente pelo
mantenedor e preparada para múltiplos usuários e titulares. O sistema usa
Django, HTMX e PostgreSQL e é executado por Docker Compose.

O produto administra contas, lançamentos simples, parcelados e recorrentes,
transferências internas, extratos e conciliações, comprovantes, fechamentos
mensais, projeções, relatórios e permissões funcionais. Investimentos,
tributação, patrimônio completo e funções de ERP não fazem parte do escopo
atual.

## Início rápido

Requisitos no host: Docker Desktop com WSL 2, Git e um editor. Python,
PostgreSQL e as ferramentas de qualidade rodam nos contêineres.

```powershell
Copy-Item .env.docker.example .env.docker
```

Defina `POSTGRES_PASSWORD` e `DJANGO_SECRET_KEY` em `.env.docker` e gere os
arquivos secretos consumidos pelo Compose:

```powershell
.\scripts\provision_compose_secrets.ps1
```

O build também requer `.secrets/github_token.txt`, contendo uma credencial de
leitura do repositório privado SharedAuth. Não exiba nem versione esse arquivo.
O arquivo `.certs/local-root-ca.crt` deve existir; deixe-o vazio quando não
houver uma autoridade certificadora local a acrescentar ou gere-o com
`.\scripts\export_local_ca.ps1` quando houver interceptação HTTPS compatível
com o script.

Suba a pilha operacional:

```powershell
docker compose --env-file .env.docker -f compose.yaml up --build -d
```

A aplicação fica em `http://127.0.0.1:5201` por padrão. O Compose aguarda o
PostgreSQL, aplica migrations, coleta os arquivos estáticos e então inicia o
Gunicorn.

Para desenvolvimento com bind mount e recarga automática:

```powershell
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml up --build -d
```

Não use `docker compose down -v` sem intenção explícita: essa opção remove os
volumes persistentes do banco e dos comprovantes.

Quem perde a senha é atendido por um administrador em **Segurança >
Permissões**: o botão **Redefinir senha** sorteia uma senha temporária,
mostrada uma única vez na tela de quem redefiniu, para ser entregue fora do
sistema. Contas criadas por essa tela recebem o mesmo tratamento. Enquanto a
troca estiver pendente, toda requisição da pessoa cai em `/change-password/` —
só o logout, o login, o `/health` e os arquivos estáticos escapam. A troca
exige a senha atual e recusa repetir a senha temporária. **Segurança > Alterar
senha** também está sempre disponível, sem obrigação. O `createsuperuser` é a
exceção: quem o roda escolheu a própria senha e não fica com troca pendente.

## Documentação

- [Arquitetura](docs/architecture.md): componentes, responsabilidades e
  controles de segurança.
- [Regras de domínio](docs/domain.md): invariantes financeiras e automação de
  recorrências.
- [Desenvolvimento e validação](docs/development.md): ambiente, comandos e
  critérios de verificação.
- [Relatório de planejamento anual](docs/annual-planning-report.md): contrato
  de contexto da tela analítica por titular.
- [Operação, dados e backup](docs/operations.md): configuração, persistência,
  implantação, backup e restauração.
- `AGENTS.md`: regras operacionais para quem mantém o repositório.
