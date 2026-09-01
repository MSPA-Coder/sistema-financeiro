# Arquitetura

## Plataforma e execução

O sistema é uma aplicação Django síncrona, renderizada no servidor, com HTMX
para atualizações parciais e Chart.js servido localmente. PostgreSQL é o único
banco. Gunicorn atende o ambiente operacional e WhiteNoise entrega os arquivos
estáticos produzidos por `collectstatic`.

Não há API REST, fila, broker, cache externo ou provedor de login social.

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
projeto não usa uma camada de repositories.

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

Uma transferência interna pode apontar para uma conta de destino fora do
escopo habitual do usuário. Nesse caso, apenas o par da transferência é
autorizado pela conta de origem; isso não concede leitura ou acesso geral à
conta de destino.

## Dependência compartilhada

SharedAuth fornece constantes de cabeçalhos defensivos/CSP e formatação de
números em pt-BR. A aplicação dos cabeçalhos, a autenticação, o modelo de
usuário e as permissões continuam pertencendo a este projeto. O build acessa a
dependência privada com um secret do BuildKit, sem incorporar a credencial à
imagem final.
