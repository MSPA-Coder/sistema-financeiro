# Operação, dados e backup

## Configuração e serviços

O Compose exige os arquivos secretos `django_secret_key`, `postgres_password` e
`patrimonio_token`. Por padrão ficam em `.secrets/`;
`COMPOSE_SECRETS_DIRECTORY` altera esse diretório. Os três são provisionados
por `.\scripts\provision_compose_secrets.ps1`, que **gera** o
`patrimonio_token` quando o arquivo de ambiente não traz um — ele é a única
credencial daqui que ninguém precisa escolher.

Certificados locais opcionais entram no build por `.certs/local-root-ca.crt`.
Esses caminhos não são versionados.

Variáveis principais:

| Variável | Função | Padrão no Compose |
|---|---|---|
| `POSTGRES_DB` | banco da aplicação | `controle_bancario` |
| `POSTGRES_USER` | usuário do banco | `postgres` |
| `POSTGRES_PORT` | porta local publicada | `5202` |
| `APP_PORT` | porta local da aplicação | `5201` |
| `ALLOWED_HOSTS` | hosts aceitos pelo Django | `localhost,127.0.0.1` |
| `CSRF_TRUSTED_ORIGINS` | origens públicas autorizadas para CSRF | vazio |
| `DEBUG` | depuração | `False` |
| `USE_HTTPS` | endurecimento para proxy TLS | `False` |

`USE_HTTPS=True` exige `CSRF_TRUSTED_ORIGINS` com origens HTTPS e um proxy TLS
que envie `X-Forwarded-Proto`. `DEBUG` é independente dessa opção.

Limites de upload podem ser ajustados por
`MAX_BANK_STATEMENT_SIZE_BYTES`, `MAX_BANK_STATEMENT_ROWS` e
`MAX_ATTACHMENT_SIZE_BYTES`. A duração da sessão usa `SESSION_COOKIE_AGE`.
`LOG_MAX_BYTES` e `LOG_BACKUP_COUNT` controlam a rotação do arquivo de log.

## Persistência

O estado operacional está separado em volumes Docker:

| Volume | Conteúdo | Fonte de verdade |
|---|---|---|
| `postgres_data` | PostgreSQL: cadastros, lançamentos, auditoria e metadados de comprovantes | sim, para dados relacionais |
| `media_volume` | arquivos de comprovantes em `/workspace/media/attachments` | sim, para o conteúdo dos comprovantes |
| `static_volume` | resultado regenerável de `collectstatic` | não |
| `app_logs` | logs rotacionados da aplicação | operacional |

O registro de um comprovante no banco não contém o arquivo. Uma restauração
completa que preserve comprovantes precisa recompor, de forma consistente, o
banco e `media_volume`. Substituir o diretório clonado não substitui esses
volumes. Instalações local e VPS têm dados independentes e não sincronizam
automaticamente.

## Backup e restauração

O backup central do PostgreSQL é feito pelo projeto irmão BackupRestore:

```powershell
python cli.py backup --projeto controle_bancario --tipos banco
```

Esse comando protege o banco e valida que o dump customizado pode ser listado;
ele não inclui `media_volume` e não demonstra uma restauração completa. Use o
ensaio oferecido pelo BackupRestore para validar a restauração do banco.

**Lacuna operacional:** não há neste repositório um procedimento automatizado,
versionado e testado para backup/restauração de `media_volume`. Antes de uma
operação destrutiva ou de depender dos comprovantes como arquivo recuperável,
defina e ensaie uma cópia separada desse volume, com retenção e correspondência
ao backup do banco. Até isso existir, não trate os comprovantes como cobertos
pelo backup central.

Restaurações são administrativas: pare a aplicação, preserve o estado atual,
confirme o destino e use um procedimento já ensaiado. Nunca execute
`docker compose down -v` em dados que devam ser preservados.

## Rollout de concessões para transferências internas

As migrations `accounts.0009_user_transfer_destination_access` e
`transactions.0003_bankoperation_responsible_user` não redistribuem nem
concedem acesso a dados existentes. Depois de aplicar a versão em uma janela
operacional planejada, um administrator deve abrir **Segurança > Permissões**
e registrar, para cada pessoa, apenas as contas que ela pode usar como destino
de transferência. Administradores e superusuários também precisam dessa
concessão explícita.

Revogar uma concessão bloqueia imediatamente criar, converter, editar,
realizar, excluir e conciliar transferências que atinjam aquele destino. O
histórico continua visível por meio da conta de origem para quem já tem esse
acesso. Antes de revogar, confirme que não há operação em andamento que a
pessoa ainda precise alterar.

Recorrências internas antigas não recebem responsável automaticamente e não
geram novas ocorrências até regularização. Na mesma tela, atribua um
responsável somente após conferir que ele está ativo, tem a permissão de criar
lançamentos, pode criar na origem e possui concessão para todos os destinos da
operação. A ação fica na auditoria; repita a verificação depois do rollout
consultando os eventos `app_user_transfer_destination_access` e
`bank_operation / assign_responsible`.

## Rollout da exigência de data no realizado

A migration `transactions.0005_realizado_exige_data` acrescenta a constraint
`ck_cash_flow_entry_realized_has_date_and_amount` e **não corrige dados**.
Antes de aplicá-la, ela confere se há lançamento `realizado` sem data ou sem
valor de realização. Se houver, para com a lista dos ids, e a transação desfaz
tudo. O `deploy.sh` então reverte código e imagem, e o banco fica como estava.

Por isso a correção vem antes do deploy. A consulta abaixo, só de leitura,
mostra o que a migration recusaria:

```sql
SELECT id, description, due_date, realized_date, realized_amount
FROM cash_flow_entry
WHERE status = 'realizado'
  AND (realized_date IS NULL OR realized_amount IS NULL)
ORDER BY id;
```

Cada linha pede uma decisão caso a caso: se é duplicata, exclua; se falta a
data e o valor reais, edite; se não foi realizada, volte a ficar em aberto.
Faça pela tela, para ficar na auditoria. Se o mês estiver fechado, reabra com
motivo e feche de novo. Em 17/09/2026 a cópia de produção tinha só o #1236,
duplicata do #1237, e junho estava fechado na conta dele.

## VPS

A implantação atual usa Ubuntu 24.04 em VPS Oracle. O Nginx publica
`https://bancario-mspa.duckdns.org`; a aplicação e o PostgreSQL permanecem em
loopback nas portas `5201` e `5202`. O vhost versionado e o instalador do nginx
estão em `_manutencao/vps/nginx/` (arquivo `controle-bancario`).

O código do servidor é um espelho somente-leitura do branch `main`. Mudanças
nascem na estação de desenvolvimento, seguem para o GitHub e chegam ao VPS por
`~/deploy.sh bancario`. Não edite nem faça commit no servidor. O script de
implantação recusa uma árvore suja.

Configuração esperada em `.env.vps`:

```dotenv
DEBUG=False
USE_HTTPS=True
ALLOWED_HOSTS=bancario-mspa.duckdns.org,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://bancario-mspa.duckdns.org
```

Verificações não destrutivas usuais:

```bash
docker compose --env-file .env.vps -f compose.yaml ps
curl -I http://127.0.0.1:5201/health/
curl -I https://bancario-mspa.duckdns.org/
~/deploy.sh bancario --check
~/deploy.sh --status
```

Uma atualização só deve ocorrer depois dos backups necessários. O comando
operacional é `~/deploy.sh bancario`; ele atualiza o espelho, reconstrói a
imagem, aguarda os health checks e valida o endereço público. `.env.vps`,
`.secrets/` e `.certs/` também ficam fora do Git e precisam ser preservados em
uma reinstalação.

**Antes do primeiro deploy desta versão**, crie `.secrets/patrimonio_token` no
servidor. O Compose recusa subir com um arquivo de segredo declarado e ausente,
e o `deploy.sh` faria rollback de uma implantação que não tinha defeito nenhum:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > .secrets/patrimonio_token
sudo chown --reference=.secrets/django_secret_key .secrets/patrimonio_token
sudo chmod --reference=.secrets/django_secret_key .secrets/patrimonio_token
```

**Dono e modo copiados do `django_secret_key`, e não `ubuntu` com `600`.** O
Compose sem Swarm monta o segredo com as permissões do arquivo no host, e o
contêiner lê como o usuário `app`, que não é o `ubuntu`. Um token que só o
`ubuntu` lê sobe sem erro nenhum e deixa a rota respondendo **503** — a falha
aparece longe da causa.

## Resumo publicado para o consolidador

`GET /patrimonio/v1/resumo` devolve, em JSON, o caixa que este sistema conhece:
uma linha por conta, com moeda e saldo na data pedida, mais o total **por
moeda** -- nunca somado entre moedas. É o que o consolidador de patrimônio lê;
ele não toca no banco daqui, e este sistema não sabe nada sobre ele.

Enquanto o consumidor migra, a v2 convive com a v1 e usa o mesmo Bearer. Ela
mantém o envelope de patrimônio e acrescenta fluxos diários agregados:

```bash
printf 'Authorization: Bearer %s\n' "$(sudo cat .secrets/patrimonio_token)" \
  | curl -s -H @- "https://bancario-mspa.duckdns.org/patrimonio/v2/resumo?inicio=2026-09-01&data=2026-09-16"
```

`data` é o fim inclusivo e `inicio` o início inclusivo; sem `data`, vale hoje,
e sem `inicio`, o recorte é de um dia. O limite é de 3.654 dias (dez anos),
compatível com os recortes de cinco anos e "Tudo" do Dashboard. `fluxos` contém
somente fatos realizados, agrupados por `data`, `moeda` e `natureza`:
`gerencial`, `transferencia`, `movimentacao` e `ajuste_de_base`. Cada item traz
`entradas`, `saidas`, `liquido` e `linhas`; valores são texto e moedas nunca
são combinadas. Um saldo inicial datado dentro do período aparece como
`ajuste_de_base`. Não são publicados movimentos individuais nem dados abertos,
projetados ou fora do intervalo.

```bash
printf 'Authorization: Bearer %s\n' "$(sudo cat .secrets/patrimonio_token)" \
  | curl -s -H @- "https://bancario-mspa.duckdns.org/patrimonio/v1/resumo?data=2026-09-16"
```

No servidor, pelo endereço público: no loopback (`127.0.0.1:5201`) o
`SECURE_SSL_REDIRECT` responde **301** a qualquer rota. O token vai pela entrada
padrão (`-H @-`), e não na linha de comando, onde qualquer usuário da máquina o
leria em `ps`.

`?data=` é opcional e vale a data de hoje. Conta cujo saldo inicial é posterior
à data pedida fica fora da foto: ela ainda não existia.

**O token é a permissão.** Quem o tem lê o saldo de todas as contas deste
sistema, sem escopo por titular -- um resumo filtrado produziria um patrimônio
consolidado que esconde contas sem avisar, que é pior do que não responder.
Guarde-o como se guarda uma senha de banco, e rode a rotação nos dois lados ao
mesmo tempo.

Sem um token utilizável (ausente, curto demais ou igual ao do
`.env.docker.example`), a rota responde **503** e a integração fica fora do ar
-- que é a falha visível. `401` quer dizer "token errado", não "não
configurado", e os dois mandam o operador procurar em lugares diferentes.
