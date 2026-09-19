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
