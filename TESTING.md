# Verificação manual

Este projeto não mantém uma suíte ampla de regressão, cobertura, análise de
tipos ou um scanner adicional como `pip-audit` dentro da suíte. A decisão é deliberada:
hoje há um único usuário e desenvolvedor, e o custo de manter esses controles
é maior que seu benefício prático. O que permanece é uma suíte focada de alto
valor (`tests/`), mais o Ruff: acesso, cabeçalhos, CSRF, autorização, grafo de
migrations, falha sem segredo, isolamento do banco de teste e contrato de
bootstrap do Compose. A CI mínima executa essa sequência em pushes/PRs para
`main` e semanalmente; o Dependabot mantém atualizações minor/patch agrupadas
de `pip`, Docker e GitHub Actions.

Antes dos comandos Docker, provisione os arquivos secretos locais a partir do
arquivo de ambiente, sem imprimir seus valores:

```powershell
.\scripts\provision_compose_secrets.ps1
```

## Antes de alterar código ou schema

1. Faça backup do PostgreSQL pelo BackupRestore, projeto irmão:

   ```powershell
   python cli.py backup --projeto controle_bancario --tipos banco
   ```

2. Execute a aplicação e percorra manualmente a tela ou fluxo alterado.
3. Para qualquer mudança de model, gere e revise uma migration Django.
4. Confirme que um banco PostgreSQL vazio sobe com `manage.py migrate` antes de
   considerar uma nova baseline ou uma mudança ampla de schema.

## Verificações rápidas

```powershell
docker compose --env-file .env.docker run --rm web python manage.py check
```

A suíte mínima de segurança e fumaça, mais o Ruff, rodam no serviço dedicado
`quality` (perfil `quality`, estágio `quality` da imagem). `web`
(`compose.yaml`) usa o estágio `runtime`, sem `ruff`/`pytest`: ele serve para
comandos Django, como o `check` acima. `compose.dev.yaml` é opcional e nunca é
carregado automaticamente; use-o somente quando precisar de bind mount e
`runserver`. Use sempre o serviço `quality` para Ruff e testes:

```powershell
docker compose --env-file .env.docker --profile quality run --rm quality
```

Mudanças que tocam autenticação, autorização, CSRF ou sessão sempre executam
esse comando. A validação principal é o backup, o bootstrap do schema e a
verificação manual do fluxo modificado.
