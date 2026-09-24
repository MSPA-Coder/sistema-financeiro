# Controle Bancário — orientações de manutenção

> **A frota é este projeto, o MegaSena e o ControleRendaVariavel.** Os três
> compartilham o `SharedAuth`, o mesmo formato de Compose e Dockerfile e o mesmo
> portão `quality`, e servem de referência uns aos outros. Divergir é permitido
> quando for uma escolha consciente: experimente num deles e, se der certo, leve
> aos outros. Este é o único dos três em Django — os outros dois são Flask —,
> então a semelhança que se busca é de **operação e contrato**, não de
> framework.
>
> **O ConfortoTermico não está na frota** e segue trilha própria desde
> 07/09/2026: a arquitetura dele é livre, e diferença em relação a ele **não é
> débito**. O que ele preserva é o contrato operacional — VPS, `deploy.sh`,
> vigia, autocura, alerta, backup e `SharedAuth`. Ver o ADR 008 daquele
> repositório.

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

Preserve alterações locais não relacionadas. O fluxo padrão é
`urls -> views -> services -> models`; consultas vivem nos services e, quando
triviais, na view. Não crie repositories ou query objects pass-through por
convenção, mas eles são permitidos quando encapsularem consultas reutilizadas,
cache, isolamento de persistência ou uma fronteira de teste que simplifique o
código de verdade — registre o motivo em `docs/architecture.md`.

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

### Loop rápido no host

O portão `quality` custa dezenas de segundos por rodada -- caro demais para o
ciclo de edição. Para isso existe um venv por projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

$env:DJANGO_SECRET_KEY = "dev-only-nao-usada-em-producao"
$env:POSTGRES_PASSWORD = "dev-only"
.\.venv\Scripts\python.exe manage.py collectstatic --noinput

.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

O que merece teste, em que camada e em que forma está em `docs/TESTES.md`,
comum aos repositórios; leia antes de escrever ou remover um teste. Diante de
vermelho, decida de quem é o defeito antes de mexer: teste que mede texto
literal reprova mudança legítima, e nesse caso quem se corrige é a asserção.
Nunca escreva código para o teste passar.

A suíte tem duas camadas (ver o docstring de `tests/conftest.py`). A maior
parte não toca o banco e roda no host sem PostgreSQL algum; os testes marcados
com `django_db` precisam de banco. O marcador é a fonte da verdade sobre quem
está em cada camada. Os testes marcados com `sentinela_front` leem JS ou CSS
porque nenhum teste executa o navegador (T6 em `docs/TESTES.md`).

No venv, portanto, o laço rápido exclui essa camada:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not django_db"
```

Rodar `pytest` sem o filtro no venv falha, e a falha é do ambiente, não do
código: não há PostgreSQL ali. A camada com banco roda no `quality`, que sobe o
serviço `postgres-teste` junto. **Não desmarque um teste `django_db` para fazer
o laço rápido passar** — foi exatamente essa camada que revelou que
`@transaction.atomic` nunca havia sido exercitado.

As duas variáveis são exigidas mesmo assim: `financeiro/settings.py` recusa
subir sem elas, e é ele que o `conftest.py` importa. Os valores são de
desenvolvimento, como os que o `Dockerfile` usa no `collectstatic` do build.

O `collectstatic` é necessário uma vez (e de novo a cada mudança em `static/`):
sem o manifesto, qualquer template com `{% static %}` estoura com "Missing
staticfiles manifest entry" e a tela de login fica sem como ser exercitada.
`staticfiles/` já é ignorado pelo Git.

O `.venv/` é uma pasta do projeto, já ignorada pelo Git: não altera o Python
do sistema nem o PATH, e apagar a pasta desfaz a instalação por inteiro. A
proibição que vale é outra, e continua de pé -- nada de instalar dependências
do projeto no Python global do Windows.

`sharedauth` é instalado direto do GitHub, na tag que `pyproject.toml` fixa e
no commit que o `uv.lock` registra. O repositório é **público**: o build precisa
só de `git` no PATH, nenhuma credencial.

Aqui o pacote entra **sem** o extra `[flask]`, de propósito: só o núcleo, que é
Python puro. O extra traria Flask, Flask-WTF e Flask-Limiter para dentro de uma
imagem Django, e o `uv.lock` registra essa escolha.

O laço rápido acima continua usando `pip install -e ".[dev]"`, e não o lock:
ele troca reprodutibilidade por velocidade de propósito. Quem garante versões
exatas é o `quality`, com `uv sync --locked`. Se o venv se comportar diferente
do contêiner numa questão de versão, o contêiner é quem está certo.

Os dois ambientes acham defeitos diferentes, então nenhum substitui o outro.
O venv é Windows; o contêiner é Linux e é a fonte oficial das versões de
`ruff` e `pip-audit` usadas pela CI. Itere no venv e passe pelo `quality` antes
de commitar; um runner alternativo só é aceitável se reproduzir o mesmo lock,
as mesmas verificações e o mesmo isolamento.

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
- a moeda pertence à conta, e conta com lançamento não muda de moeda; o que não
  tem conta está na moeda base (`BRL`). Converter moeda não é assunto deste
  sistema, e somar contas de moedas diferentes levanta `MixedCurrencyError` em
  vez de produzir um total. As telas não chegam a esse erro: elas mostram um
  bloco de totais por moeda (`banking.services.currency_blocks`), e com uma
  moeda só ficam idênticas ao que sempre foram;
- lançamentos armazenam valores positivos, e o tipo define o efeito no saldo;
- status são `a_vencer`, `vencidos` e `realizado`;
- transferências internas mantêm contrapartes e saldos consistentes, mas não
  entram como receita ou despesa gerencial; as duas pontas são espelhadas
  **apenas** na mesma moeda — entre moedas diferentes cada ponta guarda o seu
  próprio valor, sem parcelamento nem recorrência, e a taxa efetiva é derivada,
  nunca gravada;
- fechamento mensal bloqueia mutações e conciliações do período; reabertura é
  explícita e auditável;
- operações compostas são atômicas e services delimitam transações;
- datas/horas persistidas usam timezone;
- autorização por titular e permissões são controles de servidor;
- a projeção recorrente automática é disparada hoje pelo middleware em
  requisições autenticadas, uma vez no mês a partir do dia configurado; o
  botão manual permanece disponível e a rotina é idempotente. Um worker,
  scheduler ou fila pode substituir o gatilho se preservar idempotência,
  auditoria, retry seguro e execução coordenada entre processos.

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
da biblioteca.

## Validação proporcional

Mudança documental exige `git diff --check`, verificação de links/caminhos e
busca por referências obsoletas. Para template, HTMX ou JavaScript, percorra a
tela afetada. Para regra, rota ou service, execute testes focados e o fluxo
completo. Para autenticação, autorização, sessão ou CSRF, execute `quality`.
Para schema, valide backup e revise a migration; o bootstrap em PostgreSQL
vazio não é passo manual, porque o `quality` aplica a cadeia inteira de
migrações a um banco vazio a cada execução. Para dependências, Dockerfile ou
Compose, reconstrua a imagem, valide o Compose e faça smoke test da pilha.

A CI valida Compose, Ruff, pytest, dependências Python, imagem operacional e
fronteira de escrita do runtime. O Dependabot acompanha o `uv.lock`, a imagem
base e as GitHub Actions. Não enfraqueça verificações de vulnerabilidade para fazer uma falha
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

As versões suportadas são Python 3.14, PostgreSQL 17 e **Django 6.1**; faixas
completas ficam em `pyproject.toml` e as versões exatas em `uv.lock`.

O conjunto Python é reprodutível: as dependências vêm do `uv.lock`, com versão
e hash SHA-256 fixados, o instalador `uv`/ferramentas de empacotamento são
fixados no `Dockerfile` e a imagem base está presa por digest. A camada de
pacotes do sistema ainda pode flutuar, de propósito, para receber correções;
se for necessário reproduzir um artefato byte a byte, use uma imagem de
pacotes atualizada e versionada, ou registre os digests gerados.

O projeto subiu do Django 5.2 para o 6.1 em 08/09/2026, com a suíte inteira
passando. Ao mexer em algo que dependa de comportamento do framework, confira
as notas de versão do Django 6 em vez de assumir a semântica da 5.2.

Ao atualizar dependências, alargue o teto e preserve o piso compatível já
verificado; só eleve o piso quando uma incompatibilidade for comprovada. Depois
rode `uv lock` e commite o resultado — o build usa `uv sync --locked`, que
**reprova** se o lock não corresponder ao `pyproject.toml`. O Dependabot
acompanha o `uv.lock` pelo ecossistema `uv`. Reconstrua do zero, execute
`quality` e valide o fluxo afetado.

Ao concluir uma tarefa, informe comandos executados no host e nos contêineres,
resultados e validações omitidas com o motivo.
