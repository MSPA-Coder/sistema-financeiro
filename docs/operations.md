# Operação, dados e backup

## Configuração e serviços

O Compose exige os arquivos secretos `django_secret_key` e
`postgres_password`. O build exige `github_token.txt` para ler SharedAuth. Por
padrão, todos ficam em `.secrets/`; `COMPOSE_SECRETS_DIRECTORY` altera esse
diretório. Certificados locais opcionais entram no build por
`.certs/local-root-ca.crt`. Esses caminhos não são versionados.

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

## VPS

A implantação atual usa Ubuntu 24.04 em VPS Oracle. O Nginx publica
`https://bancario-mspa.duckdns.org`; a aplicação e o PostgreSQL permanecem em
loopback nas portas `5201` e `5202`. A configuração versionada do proxy está em
`deploy/nginx/controle-bancario.conf`.

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
