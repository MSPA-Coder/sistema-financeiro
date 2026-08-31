# Controle Bancário — orientações de manutenção

## Escopo e fontes de verdade

Este é um controle bancário em Django, HTMX e PostgreSQL. Docker Compose é a
interface operacional; não instale no host ferramentas ou dependências do
projeto para contornar uma falha.

Antes de alterar, leia a fonte pertinente:

- `README.md`: entrada e preparação do ambiente;
- `docs/architecture.md`: componentes e responsabilidades;
- `docs/domain.md`: invariantes financeiras;
- `docs/development.md`: comandos e critérios de validação;
- `docs/operations.md`: configuração, volumes, backup e VPS;
- `compose.yaml`, `compose.dev.yaml` e `Dockerfile`: execução efetiva;
- models, migrations e testes: schema e controles automatizados.

Preserve alterações locais não relacionadas. O fluxo usual é
`urls -> views -> services -> models`; não introduza uma camada de repositories.
Consultas vivem nos services e, quando triviais, na view.

## Comandos válidos

Prepare `.env.docker` e os arquivos ignorados de `.secrets/` conforme o README.
Nunca imprima seus valores.

```powershell
# Imagem operacional.
docker compose --env-file .env.docker -f compose.yaml up --build -d

# Desenvolvimento com bind mount e runserver.
docker compose --env-file .env.docker -f compose.yaml -f compose.dev.yaml up --build -d

# Verificação Django.
docker compose --env-file .env.docker -f compose.yaml run --rm web python manage.py check

# Ruff e pytest.
docker compose --env-file .env.docker -f compose.yaml --profile quality run --build --rm quality
```

`--build` faz parte do comando: o serviço `quality` não monta o código do
host e `docker compose run` só reconstrói quando a imagem não existe. Sem
ele, a validação roda a versão anterior do código e passa em verde.

`web` é o único serviço de aplicação. `migrate` aplica migrations e executa
`collectstatic` antes de `web`; `quality` executa as verificações. Não há
serviço `app`.

## Dados e ações destrutivas

PostgreSQL é a fonte de verdade relacional. Bancos novos nascem por
`manage.py migrate`, e toda alteração de schema exige migration Django revisada.

Antes de mudança destrutiva, conversão de dados ou manutenção de dados reais,
faça backup validado pelo BackupRestore:

```powershell
python cli.py backup --projeto controle_bancario --tipos banco
```

Esse backup não cobre `media_volume`. Se a operação puder afetar comprovantes,
preserve também a mídia por um procedimento separado e ensaiado; a lacuna está
descrita em `docs/operations.md`.

Restauração é administrativa, com a aplicação parada e destino conferido. Não
execute `docker compose down -v`, não mova volumes ou anexos reais e não apague
backups sem autorização inequívoca.

## Invariantes essenciais

- valores financeiros usam `Decimal`;
- lançamentos armazenam valores positivos, e o tipo define o efeito no saldo;
- status são `a_vencer`, `vencidos` e `realizado`;
- transferências internas mantêm contrapartes e saldos consistentes, mas não
  entram como receita ou despesa gerencial;
- fechamento mensal bloqueia mutações e conciliações do período; reabertura é
  explícita e auditável;
- operações compostas são atômicas e services delimitam transações;
- datas/horas persistidas usam timezone;
- autorização por titular e permissões são controles de servidor;
- preferências de visibilidade não são permissões;
- a projeção recorrente automática é disparada pelo middleware em requisições
  autenticadas, uma vez no mês a partir do dia configurado; o botão manual
  permanece disponível e a rotina é idempotente.

Consulte `docs/domain.md` antes de alterar saldos, recorrências, transferências,
fechamentos, importação ou conciliação.

## Segurança

Autenticação é obrigatória; autorização permanece no servidor; escritas usam
CSRF. Preserve CSP sem código inline, validação de uploads, logs sem conteúdo
sensível, cookies `HttpOnly`/`SameSite=Lax` e `Secure` quando `USE_HTTPS=True`.
Exposição pública exige proxy TLS, hosts permitidos e origens CSRF configurados.

O piso de senha de oito caracteres aparece em
`accounts/password_validators.py` e `core/services.py`; alterações nessa regra
precisam manter os dois pontos coerentes.

SharedAuth fornece constantes de segurança, formatação numérica e o sorteio da
senha temporária (`gerar_senha_temporaria`, política compartilhada com os três
apps Flask). Ele não aplica middleware, não autentica usuários e não define
permissões neste projeto — a trava de troca pendente é nativa
(`accounts/middleware.py`).

Senha redefinida por um administrador vale até o primeiro acesso:
`must_change_password` é ligada pela criação de conta e pela redefinição, e
`MustChangePasswordMiddleware` desvia **toda** requisição para
`/change-password/` enquanto ela estiver ligada — não só o login. O tamanho da
senha sorteada vem da política em Configurações > Parâmetros, nunca do padrão
da biblioteca. O token de leitura usado no build é secret do BuildKit e nunca deve
entrar em imagem, log ou commit.

## Validação proporcional

Mudança documental exige `git diff --check`, verificação de links/caminhos e
busca por referências obsoletas. Para template, HTMX ou JavaScript, percorra a
tela afetada. Para regra, rota ou service, execute testes focados e o fluxo
completo. Para autenticação, autorização, sessão ou CSRF, execute `quality`.
Para schema, valide backup, migration e bootstrap em PostgreSQL vazio. Para
dependências, Dockerfile ou Compose, reconstrua a imagem, valide o Compose e
faça smoke test da pilha.

A CI valida Compose, Ruff, pytest, dependências Python, imagem operacional e
fronteira de escrita do runtime. O Dependabot acompanha `pip`, Docker e GitHub
Actions. Não enfraqueça verificações de vulnerabilidade para fazer uma falha
passar; corrija a dependência/base ou registre uma exceção específica e
justificada quando não houver correção.

O scanner da imagem roda como contêiner com `docker save` e `--input`. Não
monte o socket Docker em contêineres e não troque esse desenho por uma action
incompatível com a política do repositório.

## Produção e versões

O VPS e seus volumes são independentes do ambiente local. O código no servidor
é espelho do `main`; desenvolvimento, commit e push ocorrem localmente. Não
edite, faça commit ou merge no VPS. Consulte `docs/operations.md` antes de
qualquer operação de produção.

As versões suportadas são Python 3.14, PostgreSQL 17 e Django 5.2; faixas
completas ficam em `requirements*.txt`. Não há lock de dependências nem patch
de imagem fixado, portanto um build pode resolver patches mais novos.

Ao atualizar dependências, alargue o teto e preserve o piso compatível já
verificado. Só eleve o piso quando uma incompatibilidade for comprovada e a
nova base mínima tiver sido validada. Reconstrua do zero, execute `quality` e
valide o fluxo afetado.

Ao concluir uma tarefa, informe comandos executados no host e nos contêineres,
resultados e validações omitidas com o motivo.
