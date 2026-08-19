# Implantação no VPS

Esta implantação publica o Controle Bancário pelo Nginx em
`https://bancario-mspa.duckdns.org`. O Docker mantém o Django em
`127.0.0.1:5201` e o PostgreSQL em `127.0.0.1:5202`; não abra essas portas no
firewall nem na OCI.

O VPS mantém a sua própria base, independente da instalação local: os dois
ambientes nunca sincronizam dados automaticamente. Levar dados de um lado ao
outro exige backup validado do PostgreSQL e cópia deliberada do volume de mídia.

O código no VPS é um espelho do `main`: toda mudança nasce na máquina de
desenvolvimento, vai ao GitHub e só então chega ao servidor. O servidor não é
lugar de editar código — `~/deploy.sh` recusa implantar se encontrar alteração
não commitada.

O arquivo `.env.vps` da implantação TLS precisa conter
`USE_HTTPS=True` e
`CSRF_TRUSTED_ORIGINS=https://bancario-mspa.duckdns.org`; sem a origem
confiável, o Django recusa os POSTs de login protegidos por CSRF.

## Primeira instalação

Com Docker Engine e o plugin Compose já instalados no VPS, clone o repositório
e crie os arquivos locais não versionados:

O repositório é privado. O VPS o lê por uma *deploy key* somente-leitura,
registrada no GitHub em **Settings → Deploy keys** e apontada pelo apelido
`github-bancario` em `~/.ssh/config`:

```
Host github-bancario
    HostName github.com
    User git
    IdentityFile ~/.ssh/deploy_bancario
    IdentitiesOnly yes
```

```bash
mkdir -p ~/apps
git clone git@github-bancario:MSPA-Coder/sistema-financeiro.git ~/apps/controle-bancario
cd ~/apps/controle-bancario
cp .env.vps.example .env.vps
mkdir -p .secrets .certs
umask 077
openssl rand -hex 32 > .secrets/postgres_password
openssl rand -hex 48 > .secrets/django_secret_key
touch .certs/local-root-ca.crt
sudo chown root:root .secrets/postgres_password
sudo chmod 0444 .secrets/postgres_password
sudo chown 999:999 .secrets/django_secret_key
sudo chmod 0400 .secrets/django_secret_key
```

O diretório `.secrets/` é privado para `ubuntu`. A senha do PostgreSQL é
montada de forma somente leitura tanto no banco quanto no Django; por isso é
legível pelos dois contêineres. A chave de sessão é legível somente pelo
usuário da aplicação.

Instale Certbot e emita o certificado. A porta 80 precisa estar acessível pela
internet para a validação HTTP inicial:

```bash
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d bancario-mspa.duckdns.org
```

Instale a configuração TLS do Nginx e valide-a antes de recarregar o serviço:

```bash
sudo install -m 0644 deploy/nginx/controle-bancario.conf /etc/nginx/sites-available/controle-bancario
sudo ln -s /etc/nginx/sites-available/controle-bancario /etc/nginx/sites-enabled/controle-bancario
sudo nginx -t
sudo systemctl reload nginx
```

Suba os serviços. A etapa `migrate` aplica as migrations e gera os assets antes
de liberar o Django:

```bash
docker compose --env-file .env.vps -f compose.yaml up --build -d
docker compose --env-file .env.vps -f compose.yaml ps
curl -I http://127.0.0.1:5201/health/
curl -I http://bancario-mspa.duckdns.org/
curl -I https://bancario-mspa.duckdns.org/
sudo systemctl status certbot.timer
```

## Atualização, backup e rollback

Antes de atualizar, faça backup do banco. Se o VPS já tiver comprovantes
anexados, preserve também o volume de mídia antes de operações administrativas:

```bash
mkdir -p backups
docker compose --env-file .env.vps -f compose.yaml exec -T postgres \
  pg_dump -U controle_bancario -d controle_bancario -Fc > backups/controle-bancario-$(date +%Y%m%dT%H%M%S).dump
```

A implantação é feita por `~/deploy.sh`, que confere a árvore, traz o `main`,
reconstrói a imagem, espera os health checks e valida o endereço público:

```bash
~/deploy.sh bancario --check   # mostra o que mudaria, sem alterar nada
~/deploy.sh bancario           # implanta
~/deploy.sh --status           # estado dos quatro projetos do VPS
```

O script aborta quando encontra alteração não commitada no servidor. Nesse caso
a correção é levar a mudança para a máquina de desenvolvimento, commitar e
enviar ao GitHub — nunca commitar no VPS.

O serviço `migrate` aplica as migrações e roda `collectstatic --clear` a cada
subida; regenerar os estáticos é comportamento normal, não sinal de problema.

Para rollback, preserve o backup, escolha uma revisão conhecida e suba de novo:

```bash
git log --oneline -5
git checkout <commit-validado>
docker compose --env-file .env.vps -f compose.yaml up --build -d
```

Esse estado é destacado (`detached HEAD`); a implantação seguinte pelo
`deploy.sh` volta a alinhar o servidor com o `main`.

`.secrets/` e `.certs/` não são versionados e vivem apenas no servidor; um
reclone precisa restaurá-los. Os dados ficam nos volumes
`controle-bancario_postgres_data` e `controle-bancario_media_volume`, fora da
pasta do código: substituir o diretório do projeto não os afeta.

Não use `docker compose down --volumes`: isso removeria o banco e os volumes
de mídia do VPS.
