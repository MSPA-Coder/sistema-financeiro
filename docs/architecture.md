# Arquitetura

## Plataforma e execução

O sistema é uma aplicação Django síncrona, renderizada no servidor, com HTMX
para atualizações parciais e Chart.js servido localmente. PostgreSQL é o único
banco. Gunicorn atende o ambiente operacional e WhiteNoise entrega os arquivos
estáticos produzidos por `collectstatic`.

Hoje não há API REST geral, fila, broker, cache externo ou provedor de login
social. Existem endpoints JSON de publicação patrimonial; esses contratos são
tratados separadamente. A adoção futura de fila, cache ou outro adaptador é
permitida quando preservar as invariantes de domínio e tiver decisão
arquitetural registrada.

O `compose.yaml` define estes serviços (a lista completa sai de
`docker compose --profile quality config --services`):

| Serviço | Responsabilidade |
|---|---|
| `postgres` | persistência relacional e health check |
| `db-provision` | cria ou atualiza o papel restrito da aplicação (só DML), a cada subida e antes do `migrate` |
| `migrate` | migrations e `collectstatic`, antes da aplicação; o único serviço de aplicação com a credencial administrativa do banco |
| `web` | aplicação Gunicorn, com filesystem raiz somente leitura, conectada com o papel restrito |
| `postgres-teste` | banco efêmero (em `tmpfs`) da suíte, no perfil `quality`: nunca é o `postgres` com dados reais |
| `quality` | Ruff, pytest e ferramentas de auditoria, no perfil `quality` |

`compose.dev.yaml` altera somente o desenvolvimento: monta o repositório no
serviço `web`, habilita `DEBUG` e usa `runserver`.

## Organização do código

### Filtro global de moeda

As telas financeiras leem `currency=BRL|USD|ALL` da query string por meio de
`core.currency_filter.parse_currency_filter`. O padrão é `BRL`; valores
desconhecidos respondem 400. O valor é exposto como `global_currency` pelo
context processor e permanece exclusivamente no request: não é salvo em
sessão, perfil ou banco. Isso permite que abas concorrentes tenham filtros
independentes e impede que uma resposta antiga altere o estado de outra.
As opções específicas restringem os dados antes dos cálculos; `ALL` preserva
os blocos por moeda, sem conversão ou totais que misturem BRL e USD.

No menu a moeda é uma seleção múltipla (Real, Dólar), mas a URL continua com
um valor só: as duas marcadas viajam como `ALL`, e `BRL,USD` é aceito como
sinônimo. Com duas moedas as formas são equivalentes, e o valor único mantém
favoritos e links válidos.

### Filtro global de grupos de conta

`grupos=bancos,corretoras,cartoes,aplicacoes` segue o mesmo contrato da moeda:
lido da query string por `core.account_group_filter.parse_account_groups`,
ausente vale todos, valor desconhecido responde 400, nada é gravado. A regra de
partição está em `docs/domain.md`. O filtro entra num lugar só,
`reports.services.selected_context`/`context_options`, e por isso vale em toda
tela que monta as contas por ali: Lançamentos, dashboard, Projeções, Próximos
movimentos, Posição por conta e Controle gerencial.

Os seletores de Instituição e Conta dessas telas respeitam os dois filtros
globais: listam só as contas dos grupos e da moeda marcados, e só as
instituições que têm uma delas. A conta e a instituição já escolhidas
continuam listadas mesmo fora do filtro, para o seletor mostrar o que vale. O
Planejamento anual, que tem seletor próprio, aplica os mesmos dois filtros às
suas opções.

No menu, marcar uma caixa não aplica nada: moeda e grupos mudam juntos no
botão Aplicar, para se mexer em várias caixas sem reabrir o menu. A última
caixa marcada de cada bloco não desmarca.

No navegador, `static/js/core/application.js` repassa `currency` e `grupos` da
URL atual para links, formulários GET e requisições HTMX, completando só o que
o destino não traz. O menu lateral e a faixa "Mostrando só..." ficam fora de
`#appMain`, porque os filtros globais trocam de página e não de fragmento.

### Dashboard

O painel não tem cálculo próprio: meses, receitas, despesas, geração e saldo
saem de `reports.services.projection_months_between`, e o saldo diário do
mesmo cálculo do extrato. Só categorias gerenciais entram em receita e
despesa; o saldo é o real. Assim a fatia ou a barra clicada é a soma da tela de
Lançamentos que ela abre.

O fluxo de referência é:

```text
urls -> views -> services -> models -> PostgreSQL
```

- `accounts/`: usuários, titulares, perfis e permissões funcionais;
- `banking/`: instituições e contas financeiras;
- `transactions/`: lançamentos, operações compostas, transferências,
  recorrências e fechamento mensal;
- `bank_statements/`: leitura de extratos, importação, conciliação e
  comprovantes. `classificacao.py` aprende a categoria pelo histórico (usado
  também pela fatura), `extrato.py` decide o que cada linha pendente vira
  (conciliar, criar, transferência pareada), `regras.py` aplica as regras
  explícitas, `saldo.py` é o Atualizar saldo, e `pares_proprios.py` e
  `familias.py` são as manutenções de dados com simulação;
- `management/`: tags, projetos e orçamentos;
- `reports/` e `dashboard/`: consultas e apresentação analítica;
- `core/`: configuração da aplicação, auditoria, segurança e serviços comuns;
- `core/domain/`: vocabulário financeiro e normalização independentes do ORM;
- `templates/` e `static/`: interface renderizada e comportamento do navegador.

Views tratam HTTP, autenticação, autorização e composição da resposta. Services
concentram regras financeiras e limites transacionais. Models representam o
schema e suas restrições. Consultas triviais podem permanecer na view; o
projeto evita repositories pass-through, mas permite query objects ou
repositories quando reduzirem duplicação, acoplamento ou custo de consulta.

### Leituras memorizadas por requisição

Não há cache de processo nem externo. Duas leituras repetidas em quase toda
tela são guardadas **só durante a requisição**, porque um cache de processo
deixaria cada worker do Gunicorn com uma cópia que os outros não veem mudar:

- a data inicial do sistema (`core.services.system_start_date`), pelo memo de
  `core/memo_requisicao.py`, aberto por `MemoRequisicaoMiddleware`; fora de
  requisição, comandos e testes leem direto do banco;
  `update_system_start_date` descarta o valor guardado;
- as permissões funcionais do usuário (`accounts.services.has_function_permission`),
  guardadas no próprio objeto do usuário, como o `_perm_cache` do Django;
  `save_function_permissions` as descarta.

Um novo valor só entra nesse memo se quem o grava o descartar em seguida.

### Extrato: recorte, cálculo e tela

`transactions.services` separa o extrato em três passos:
`resolve_statement_request` lê período, sessão, contas e filtros;
`compute_statement` calcula linhas, saldo corrente e blocos por moeda para um
`view_mode`; e `build_transactions_view_context` acrescenta o que só a tela
Lançamentos usa (edição inline e opções de filtro/formulário). O detalhe da
conta resolve o recorte uma vez e calcula previsto e realizado sobre ele, sem
tocar a sessão.

### Custo em consultas: o que foi medido e o que ficou de fora

`tests/test_teto_consultas.py` guarda Lançamentos e dashboard de dois jeitos:
um teto por tela e um teste de escala, que compara a mesma tela com uma e com
cinco rodadas de lançamentos (simples, parcelado e transferência). Em
22/09/2026 as duas telas não cresciam com as linhas -- não havia N+1.

No mesmo dia, `EXPLAIN ANALYZE` sobre a cópia local do banco (742 lançamentos,
552 kB; a conta maior com 206) deu entre 0,1 e 0,2 ms para a listagem e para o
saldo de abertura nos modos realizado, todos e a vencer, sempre por índice.
Por isso ficaram de fora, de propósito:

- índices novos, inclusive o funcional sobre a data de saldo
  (`CASE status WHEN realizado THEN realized_date ELSE due_date`), que o modo
  todos filtra depois do índice por conta. Ele só passa a valer com dezenas de
  milhares de linhas por conta;
- paginação do extrato: o saldo corrente precisa de todas as linhas do período,
  que já é de um mês e tem o teto `REPORT_MAX_ENTRIES`;
- reaproveitar em `compute_statement` as linhas que `list_transactions_for_view`
  e `entries_for_period` leem do mesmo período. Economiza uma consulta
  submilissegundo e mexe no saldo corrente.

Se o volume mudar de ordem de grandeza, refaça a medição antes de otimizar.

Cada app versiona seu schema em `<app>/migrations/`. Bancos novos e existentes
são atualizados exclusivamente por `manage.py migrate`; o serviço `migrate`
termina com sucesso antes de `web` iniciar.

## Segurança e acesso

- autenticação é obrigatória nas telas operacionais;
- nomes de usuário são autenticados sem distinção de maiúsculas e minúsculas;
- permissões funcionais e acesso por titular são verificados no servidor;
- perfis agrupam permissões, mas não substituem as verificações efetivas;
- o **Modo discreto** mascara valores e esconde gráficos para quem está vendo
  a tela compartilhada; é uma preferência visual local, não retira acesso nem
  remove dados já presentes no DOM;
- escritas usam proteção CSRF, inclusive quando iniciadas por HTMX;
- a política CSP não permite scripts, estilos ou handlers inline;
- cookies são `HttpOnly` e `SameSite=Lax`; `USE_HTTPS=True` ativa cookies
  `Secure`, redirecionamento HTTPS e HSTS para implantação atrás de proxy TLS;
- eventos relevantes são registrados em `AuditLog`; segredos não devem entrar
  em código, logs ou commits.

Transferências internas exigem uma concessão explícita do usuário para a conta
de destino, além da permissão funcional e do acesso à origem. A concessão não
permite consultar a conta destino. O par histórico permanece visível pela
origem, mas a revogação bloqueia novas mutações financeiras e recorrências.

## Dependência compartilhada

SharedAuth fornece constantes de cabeçalhos defensivos/CSP e formatação de
números em pt-BR. A aplicação dos cabeçalhos, a autenticação, o modelo de
usuário e as permissões continuam pertencendo a este projeto. O repositório do
SharedAuth é público: o build o instala por Git, na tag fixada no
`pyproject.toml` e no commit registrado no `uv.lock`, sem credencial.

## Publicação patrimonial

Este sistema publica o caixa para quem consolida o patrimônio (hoje, o
Wealthfolio) por duas rotas somente leitura, sem sessão e sem escopo por titular:

| Rota | Token | Para quê |
|---|---|---|
| `/patrimonio/v4/metadata`, `/snapshot`, `/changes` | `PATRIMONIO_INTEGRATION_TOKEN` | o contrato do consolidador (abaixo) |
| `/patrimonio/v3/projection` | `PATRIMONIO_TOKEN` | a projeção de caixa, reservada para a fase de projeção consolidada |

Os contratos `v1/resumo`, `v2/resumo`, `v3/activities`, `v3/categories` e
`v3/metadata` serviam ao NetWorth, aposentado em 29/09/2026, e foram retirados em
03/10/2026: o nginx não registra acesso a eles desde 28/09, o Wealthfolio só lê o
v4 (o patch dele recusa qualquer outro caminho), e o histórico está no Git.
Todas as rotas respondem só a `GET`, e o nginx fecha `/patrimonio/` para fora.

### Snapshot de integração v4

Desde 02/10/2026 a conta publica `purpose` (`pessoal` ou `administrada`) e a
categoria publica `group` (nome do grupo ou `null`), como acréscimos opcionais
que não mudam a versão do contrato; quem não os conhece os ignora. Renomear um
grupo toca as categorias dele, para o feed de mudanças pedir um novo snapshot.

Desde 03/10/2026 o lançamento (`cash_entry`) publica também `effective_status`:
o status que vale em `snapshot_as_of`, derivado da data. `realizado` continua
`realizado`; em aberto com vencimento anterior a `snapshot_as_of` é `vencidos`,
e o resto é `a_vencer`, qualquer que seja o `status` gravado (que só se
normaliza quando alguém grava o lançamento). É a mesma regra que as telas
aplicam na leitura, publicada para que nenhum consumidor a recalcule. Ele muda
com o dia sem que o lançamento mude, então um consumidor que guarda a foto pelo
`high_watermark` precisa pedir uma nova a cada dia.

`GET /patrimonio/v4/metadata`, `GET /patrimonio/v4/snapshot` e
`GET /patrimonio/v4/changes` são a integração com o Wealthfolio. Usam o Bearer
exclusivo `PATRIMONIO_INTEGRATION_TOKEN`; a projeção v3 continua com
`PATRIMONIO_TOKEN`. Todas são somente leitura. O snapshot lê contas, categorias,
lançamentos de caixa e agrupadores de transferência sob `REPEATABLE READ`,
com valores decimais em texto e IDs opacos e estáveis derivados da identidade
da fonte e da chave persistida.

O v4 mantém uma outbox de invalidação transacional. Uma única linha de contador
é travada e incrementada na transação que altera o registro de origem, para que
os cursores respeitem a ordem de commit. Cada mudança pede um novo snapshot
consistente; o feed não recria lançamentos financeiros no consumidor. Os
cursores são assinados pelo token v4, de modo que sua rotação também invalida
cursores emitidos com o segredo anterior.
Exclusões publicam tombstones, e os cursores são assinados com o segredo do
contrato. Transferências sem operação bancária associada permanecem visíveis
como lançamentos de caixa e são contadas como não vinculadas na cobertura, sem
inventar uma contraparte.
