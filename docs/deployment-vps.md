# Implantação de teste no VPS

Esta implantação publica o Controle Bancário somente pelo Nginx em
`http://bancario-mspa.duckdns.org`. O Docker mantém o Django em
`127.0.0.1:5201` e o PostgreSQL em `127.0.0.1:5202`; não abra essas portas no
firewall nem na OCI.

O banco e os anexos do computador local não são copiados automaticamente. A
primeira subida no VPS cria uma base independente e vazia. Uma futura migração
precisa de backup validado do PostgreSQL e cópia deliberada do volume de mídia.

## Primeira instalação

Com Docker Engine e o plugin Compose já instalados no VPS, clone o repositório
e crie os arquivos locais não versionados:

```bash
mkdir -p ~/apps
git clone https://github.com/MSPA-Coder/sistema-financeiro.git ~/apps/controle-bancario
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

Instale a configuração do Nginx e valide-a antes de recarregar o serviço:

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
```

## Atualização, backup e rollback

Antes de atualizar, faça backup do banco. Se o VPS já tiver comprovantes
anexados, preserve também o volume de mídia antes de operações administrativas:

```bash
mkdir -p backups
docker compose --env-file .env.vps -f compose.yaml exec -T postgres \
  pg_dump -U controle_bancario -d controle_bancario -Fc > backups/controle-bancario-$(date +%Y%m%dT%H%M%S).dump
```

Atualize somente revisões já validadas e reconstrua a imagem:

```bash
git pull --ff-only origin main
docker compose --env-file .env.vps -f compose.yaml up --build -d
```

Para rollback, preserve o backup, escolha uma revisão conhecida e suba de novo:

```bash
git log --oneline -5
git checkout <commit-validado>
docker compose --env-file .env.vps -f compose.yaml up --build -d
```

Não use `docker compose down --volumes`: isso removeria o banco e os volumes
de mídia do VPS.
