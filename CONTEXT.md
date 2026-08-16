# Contexto técnico

Referência do estado atual do Controle Bancário. Registra conceitos e contratos
duráveis; detalhes de implementação devem ser confirmados no código.

## Produto

Controle bancário e fluxo de caixa operacional para múltiplos usuários e
titulares. O domínio implementado inclui:

- instituições financeiras, contas, titulares e categorias;
- lançamentos simples, parcelados e recorrentes;
- transferências internas e operações compostas;
- dashboard, saldos, próximos movimentos, projeções e controle gerencial;
- tags, projetos/centros de custo e orçamentos;
- importação de extratos, conciliação e comprovantes;
- fechamento mensal, manutenção do banco e trilha de auditoria;
- autenticação, permissões funcionais e escopo de acesso por titular.

Investimentos, tributação, patrimônio amplo e funções de ERP não são domínios
próprios do sistema. Isso descreve o escopo atual, não proíbe evolução.

## Tecnologia e versões

- Python 3.14, Django 5.2 e templates Django.
- PostgreSQL 17 como único banco, em produção e desenvolvimento.
- HTMX para interações incrementais; Chart.js servido localmente.
- Gunicorn como servidor operacional; WhiteNoise para assets.
- Ruff opcional para erros simples.

Compatibilidade mínima suportada: Python 3.14, PostgreSQL 17 e Django 5.2;
as faixas de todas as bibliotecas ficam em `requirements*.txt`. Atualmente são
testadas as famílias `python:3.14-slim` e `postgres:17-alpine`, com versões
resolvidas dentro dessas faixas. Não há lock de dependências nem patch de imagem
registrado, portanto atualizações são deliberadas: avaliar compatibilidade,
atualizar imagem/faixas e documentação, reconstruir do zero, rodar `quality` e
validar o fluxo afetado.

Não há fila de tarefas, broker, cache externo, API REST nem provedor de login
social: a aplicação é síncrona e o navegador conversa apenas com o Django.

`manage.py` delega aos comandos do Django. `financeiro/settings.py` monta a
aplicação, a segurança HTTP e o contexto global.

## Configuração

Configuração vem do ambiente, sem padrões permissivos:

- `DJANGO_SECRET_KEY` é obrigatória. No Compose operacional ela e
  `POSTGRES_PASSWORD` são lidas de arquivos em `/run/secrets`; ausência, vazio
  ou erro de leitura faz a aplicação recusar subir. O script
  `scripts/provision_compose_secrets.ps1` migra os valores já existentes de
  `.env.docker` para `.secrets/` sem exibi-los. Variáveis diretas só são
  aceitas fora do contrato Compose, para comandos locais explícitos.
- `DEBUG` tem padrão `False`. Ligar é uma decisão explícita de máquina de
  desenvolvimento.
- `USE_HTTPS` (padrão `False`) controla o endurecimento de transporte —
  cookies `Secure`, redirect para HTTPS, HSTS e confiança em
  `X-Forwarded-Proto`. É propriedade da implantação, não do modo de depuração:
  a instalação padrão publica em loopback sobre HTTP, onde cookies `Secure`
  não seriam enviados.
- O banco é configurado por `POSTGRES_HOST/PORT/USER/PASSWORD/DB`.
  `POSTGRES_USER` e `POSTGRES_DB` têm defaults no Compose; a senha vem do
  arquivo secreto montado. Os comandos operacionais sempre usam Compose.

## Arquitetura

Fluxo de referência:

```text
urls -> views (HTTP) -> services (regra + transação) -> models (ORM/PostgreSQL)
```

- `core/`, `accounts/`, `banking/`, `bank_statements/`, `transactions/`,
  `management/`, `reports/` e `dashboard/`: apps Django por domínio.
- `core/domain/`: vocabulário financeiro, dinheiro, normalização e chaves de
  configuração, independentes de Django.
- `templates/` e `static/`: apresentação e comportamento no navegador.
- Cada app versiona seu schema em `<app>/migrations/`.

Não existe camada de repositories, e ela não é desejada: consultas vivem nos
services e, quando triviais, na própria view. Views tratam HTTP, autenticação,
autorização e composição da resposta; a regra financeira e o limite
transacional ficam nos services. Templates apresentam dados e não concentram
regra financeira.

Consultas usam a API moderna do ORM. Eager loading (`select_related`,
`prefetch_related`) é escolhido conforme as relações realmente consumidas,
sobretudo em tabelas; não se aplica mecanicamente.

## Persistência e evolução do schema

PostgreSQL é a fonte de verdade. Bancos novos são criados exclusivamente por
`manage.py migrate`; bancos existentes recebem as migrations pendentes na etapa
controlada de inicialização (serviço `migrate` do Compose), antes de a
aplicação aceitar tráfego.

Toda alteração de schema tem migration Django revisada manualmente. Migrations
preservam dados por padrão. Alterações destrutivas ou conversões de dados reais
exigem backup validado, procedimento explícito e consentimento.

Operações HTTP e CLI que escrevem compartilham limites transacionais
consistentes. Services não concluem transações por conta própria; falhas em
operações compostas revertem o conjunto completo.

## Domínio financeiro

### Valores e tipos

- `Decimal` é a fonte de cálculo monetário.
- Conversão para `float` ocorre somente em apresentação, JSON ou gráficos.
- Lançamentos armazenam valor positivo; `entry_type` (`receita` ou `despesa`)
  define o efeito no saldo.
- Status oficiais: `a_vencer`, `vencidos`, `realizado` e `cancelado`.

### Saldos

- Visão `realizado`: considera movimentos realizados.
- Visão `vencidos`: combina realizados e abertos vencidos.
- Visão `a_vencer`: combina realizados e futuros a vencer.
- Nas visões `a_vencer` e `vencidos`, o saldo inicial do intervalo incorpora
  realizados do próprio período filtrado.
- O saldo inicial cadastrado na conta compõe a base de cálculo.

### Operações compostas

`BankOperation` agrupa entradas relacionadas. Criação, edição, exclusão,
realização e conciliação mantêm o agrupamento, o tipo de operação e as relações
entre entradas consistentes.

Transferências internas:

- usam categoria interna;
- criam origem e contraparte vinculadas (`source_entry`);
- afetam os saldos das contas envolvidas;
- não compõem receitas ou despesas gerenciais;
- são realizadas e alteradas de forma coerente entre as duas pontas;
- podem ter como destino uma conta fora do escopo normal do usuário que
  iniciou a operação. Nesse caso a autorização do par é resolvida
  exclusivamente pela conta de origem (`transactions/access.py`). Essa exceção
  não concede leitura, relatórios ou acesso geral à conta de destino.

Parcelas e recorrências preservam identidade e escopo de edição/exclusão. O
serviço de transações é organizado por casos de uso em `transactions/`; views
consomem sua API pública.

### Fechamento mensal

O fechamento é por conta e período. Um mês fechado bloqueia criação, edição,
exclusão, realização e conciliação de movimentos abrangidos. Reabertura é uma
ação explícita e auditável.

## Extratos e comprovantes

`bank_statements/` concentra adaptação de arquivos, importação, detecção de
duplicidade, candidatos, conciliação e comprovantes; `banking/` mantém contas e
instituições.

- Adapters convertem CSV/OFX/OFC/QFX em uma representação normalizada.
- Uploads passam por validação de tamanho, quantidade, extensão e assinatura.
- Conciliação verifica conta, tipo, valor, status, período fechado e
  duplicidade.
- Anexos ficam no diretório operacional e não são expostos sem autorização.
- Mudanças que afetam lançamentos agrupados sincronizam a operação bancária
  correspondente.

## Projeções e gestão

Projeções, relatórios e dashboard compartilham as regras financeiras, os
filtros de acesso e a exclusão de transferências internas das análises
gerenciais.

Configurações > Contas em análises grava duas preferências independentes por
usuário e conta: ocultar no Dashboard e ocultar em Projeções. É preferência
pessoal, não permissão — não retira acesso à conta. A ocultação vale para a
visão agregada: contas ocultas saem dos totais, mas continuam listadas nos
seletores, e escolher a conta explicitamente no filtro vence a preferência
(caso contrário a tela ficaria vazia sem explicar o motivo). Próximos
movimentos, posição por conta e controle gerencial não têm preferência
própria e não filtram.

A projeção de recorrências é disparada manualmente pela tela de parâmetros. A
execução é idempotente para o mesmo período e respeita ocorrências já
materializadas; um controle de "última execução no mês" evita repetição
acidental.

## Autenticação, permissões e auditoria

- Autenticação usa `CaseInsensitiveUsernameBackend`: o nome de usuário é
  aceito em qualquer caixa, com unicidade garantida no banco.
- `AppPermissionBackend` resolve as permissões funcionais do projeto (chaves
  com múltiplos pontos, como `tables.owners.manage`) contra o catálogo
  `AppPermission`/`UserPermission`, incluindo o bypass de administrador.
- Política de senha configurável, troca obrigatória e lockout por falhas.
- Permissões funcionais e acesso por titular são verificações do servidor,
  aplicadas também a mutações e a identificadores recebidos em POST.
- Perfis são conjuntos convenientes de permissões, não substitutos das
  verificações efetivas.
- Um usuário não pode remover de si próprio a capacidade de gerir permissões.
- Menu e controles visuais refletem o acesso, mas não são barreiras.
- Eventos relevantes são auditáveis sem registrar segredos nos logs.

## Interface e segurança web

O menu é declarado em `core/context_processors.py` e filtrado por permissão no
servidor.

A Content Security Policy aceita scripts e estilos da própria aplicação e
rejeita código inline (`core/security.py`). Não há `<script>` inline, atributo
`style` nem handler `on*` nos templates; dados para gráficos passam por
`json_script`. Comportamentos globais ficam em `static/js/core/`;
comportamentos de página ficam em assets próprios.

CSRF protege as escritas, inclusive as vindas de HTMX — `HX-Request` é sinal de
apresentação, nunca prova de origem. Cookies usam opções seguras conforme
`USE_HTTPS`.

Em produção os assets são servidos pelo WhiteNoise a partir do `collectstatic`,
com nome versionado por hash e compressão. Em desenvolvimento os arquivos são
servidos direto de `static/`, sem hash.

Filtros de data usam `<input type="date">`; filtros de mês/ano usam
`<input type="month">`. Ambos são controles nativos do navegador, com `min`
igual à data inicial do sistema quando configurada
(`core.services.system_start_date`), e sem widget próprio — decisão
deliberada, não pendência. `<input type="month">` exibe o nome do mês no
idioma configurado no *navegador* do usuário, não no idioma da página: em
navegador configurado em inglês aparece "August" em vez de "Agosto". Isso não
é corrigível via `lang`, `<meta>` ou CSS (testado e confirmado) — é como
qualquer site com esse tipo de input se comporta. Não reintroduza um widget de
mês/ano em JavaScript para "corrigir" isso.

## Observabilidade e operação

Logs vão para console e para um arquivo rotacionado em `logs/`
(`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`). Não há correlação por `X-Request-ID`.

Backups do PostgreSQL usam `pg_dump` em formato customizado com validação por
`pg_restore --list` (`scripts/backup_postgres.ps1`). A restauração é
administrativa e ocorre com a aplicação parada. Anexos ficam em volume
separado do volume do banco.

## Notas de manutenção

- **Todas as colunas de data/hora são `timestamptz`.** O schema herdado usava
  `timestamp without time zone` com `USE_TZ=True`, o que deslocava o lookup
  `__date` e arquivava no dia seguinte os eventos de auditoria gravados entre
  18h e 21h. A migration `core.0002` converteu as 52 colunas afetadas
  interpretando os valores como UTC — que era o que já eram. Não reintroduza
  colunas ingênuas: o defeito é silencioso e só aparece em parte do dia.
- **`BankOperation.legacy_operation_id`** guarda o identificador de operação
  do sistema anterior. É rastro histórico, não chave de agrupamento — o
  agrupamento é sempre pela FK `bank_operation`.
