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

O `compose.yaml` define quatro serviços:

| Serviço | Responsabilidade |
|---|---|
| `postgres` | persistência relacional e health check |
| `migrate` | migrations e `collectstatic`, antes da aplicação |
| `web` | aplicação Gunicorn, com filesystem raiz somente leitura |
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

O fluxo de referência é:

```text
urls -> views -> services -> models -> PostgreSQL
```

- `accounts/`: usuários, titulares, perfis e permissões funcionais;
- `banking/`: instituições e contas financeiras;
- `transactions/`: lançamentos, operações compostas, transferências,
  recorrências e fechamento mensal;
- `bank_statements/`: leitura de extratos, importação, conciliação e
  comprovantes;
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

## Publicação patrimonial v2

`GET /patrimonio/v2/resumo` convive com a v1 e reutiliza o mesmo Bearer
(`PATRIMONIO_TOKEN`), sem sessão ou escopo por titular. O envelope patrimonial
da v1 permanece; a v2 apenas troca `contrato` para `patrimonio/v2` e acrescenta
`periodo_dos_fluxos` (`inicio`/`fim`) e `fluxos`.

`data` é o fim inclusivo da foto e `inicio` é o começo inclusivo dos fluxos.
Sem `data`, usa-se o dia local; sem `inicio`, o período tem um dia. O intervalo
aceita no máximo 3.654 dias (dez anos calendáricos, cobrindo os recortes de cinco anos e
"Tudo" do Dashboard) e não há migration: a consulta lê o schema corrente.
Cada fluxo é agregado por data, moeda e natureza (`gerencial`, `transferencia`,
`movimentacao` ou `ajuste_de_base`), com valores monetários serializados como
texto. Só lançamentos `realizado` com `realized_date` e `realized_amount` entram;
lançamentos abertos ou projetados são deliberadamente omitidos. O saldo
inicial cuja data cair no intervalo vira `ajuste_de_base`. Os grupos nunca
misturam moedas e não expõem movimentos individuais.
Foto e fluxos são lidos sob o mesmo snapshot `REPEATABLE READ`; a agregação
dos lançamentos ocorre no PostgreSQL antes de os grupos chegarem à aplicação.

### Extensão somente leitura v3 para o shell

As rotas `/patrimonio/v3/activities`, `/categories` e `/metadata` ampliam a
publicação sem alterar v1 ou v2. Atividades são lançamentos persistidos, com
paginação, filtros e IDs opacos; categorias e metadados permitem ao consumidor
montar filtros e links sem copiar tabelas.

O metadata declara também as capacidades analíticas da fonte. `fluxos` é
verdadeiro porque o caixa agregado já é publicado em v2. `renda`,
`performance` e `eventos` são falsos: este sistema não mantém um livro de
investimentos, uma série de retorno de carteira ou uma entidade de eventos do
shell. O consumidor deve exibir o estado indisponível correspondente, sem
reconstruir essas métricas a partir de saldos ou inventar registros.

Todas as rotas v3 são GET-only, usam o Bearer de patrimônio e não oferecem
qualquer caminho de escrita.
