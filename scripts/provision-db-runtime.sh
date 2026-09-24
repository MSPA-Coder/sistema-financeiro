#!/bin/sh
# Cria/atualiza o papel usado pela aplicação sem expor a credencial
# administrativa ao contêiner web. O mesmo script roda no Compose local, no VPS
# e na CI. Veio do ControleRendaVariavel; aqui ganhou vários esquemas e a
# permissão de manutenção opcional.
#
# O papel administrativo (o `POSTGRES_USER` da imagem, superusuário) continua
# dono das tabelas e só aparece no banco, neste serviço e no `migrate`. O
# papel da aplicação recebe DML e uso de sequências, e nada de DDL: uma injeção
# de SQL ou um defeito de escrita fica restrito aos dados, sem alcançar o
# cluster (`COPY ... PROGRAM`, `pg_read_file`, outros bancos).
#
# Variáveis opcionais:
#   DB_SCHEMAS         esquemas liberados, separados por espaço (padrão: public)
#   DB_APP_MAINTAIN=1  concede MAINTAIN (VACUUM/ANALYZE) nas tabelas -- só para
#                      quem tem rotina de manutenção disparada pela aplicação

set -eu

: "${DB_HOST:?DB_HOST ausente}"
: "${DB_PORT:?DB_PORT ausente}"
: "${DB_NAME:?DB_NAME ausente}"
: "${DB_ADMIN_USER:?DB_ADMIN_USER ausente}"
: "${DB_ADMIN_PASSWORD_FILE:?DB_ADMIN_PASSWORD_FILE ausente}"
: "${DB_APP_USER:?DB_APP_USER ausente}"
: "${DB_APP_PASSWORD_FILE:?DB_APP_PASSWORD_FILE ausente}"
DB_SCHEMAS=${DB_SCHEMAS:-public}
DB_APP_MAINTAIN=${DB_APP_MAINTAIN:-0}

if [ "$DB_APP_USER" = "$DB_ADMIN_USER" ]; then
    echo "DB_APP_USER não pode ser o papel administrativo ($DB_ADMIN_USER)" >&2
    exit 1
fi

read_secret() {
    arquivo=$1
    [ -r "$arquivo" ] || {
        echo "segredo ausente ou ilegível: $arquivo" >&2
        exit 1
    }
    valor=$(tr -d '\r\n' < "$arquivo")
    [ -n "$valor" ] || {
        echo "segredo vazio: $arquivo" >&2
        exit 1
    }
    printf '%s' "$valor"
}

admin_password=$(read_secret "$DB_ADMIN_PASSWORD_FILE")
app_password=$(read_secret "$DB_APP_PASSWORD_FILE")

# A senha é escrita em arquivo temporário no tmpfs, não na linha de comando.
# O escape cobre senhas manuais além das geradas pelo provisionamento padrão.
sql_file=$(mktemp /tmp/provision-db.XXXXXX)
trap 'rm -f "$sql_file"' EXIT HUP INT TERM
escaped_app_password=$(printf '%s' "$app_password" | sed "s/'/''/g")

tabelas="SELECT, INSERT, UPDATE, DELETE"
if [ "$DB_APP_MAINTAIN" = "1" ]; then
    tabelas="$tabelas, MAINTAIN"
fi

cat > "$sql_file" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_APP_USER') THEN
        CREATE ROLE "$DB_APP_USER" LOGIN;
    END IF;
END
\$\$;
ALTER ROLE "$DB_APP_USER"
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD '$escaped_app_password';
GRANT CONNECT ON DATABASE "$DB_NAME" TO "$DB_APP_USER";
REVOKE CREATE ON DATABASE "$DB_NAME" FROM "$DB_APP_USER";
SQL

for esquema in $DB_SCHEMAS; do
    # Numa instalação nova, esquema próprio só nasceria na migração, que roda
    # depois deste script. Criá-lo aqui, com o mesmo dono que a migração daria,
    # permite conceder já; a migração usa `CREATE SCHEMA IF NOT EXISTS`.
    if [ "$esquema" != "public" ]; then
        printf 'CREATE SCHEMA IF NOT EXISTS "%s" AUTHORIZATION "%s";\n' \
            "$esquema" "$DB_ADMIN_USER" >> "$sql_file"
    fi
    cat >> "$sql_file" <<SQL
GRANT USAGE ON SCHEMA "$esquema" TO "$DB_APP_USER";
REVOKE CREATE ON SCHEMA "$esquema" FROM "$DB_APP_USER";
GRANT $tabelas ON ALL TABLES IN SCHEMA "$esquema" TO "$DB_APP_USER";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "$esquema" TO "$DB_APP_USER";
ALTER DEFAULT PRIVILEGES FOR ROLE "$DB_ADMIN_USER" IN SCHEMA "$esquema"
    GRANT $tabelas ON TABLES TO "$DB_APP_USER";
ALTER DEFAULT PRIVILEGES FOR ROLE "$DB_ADMIN_USER" IN SCHEMA "$esquema"
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "$DB_APP_USER";
SQL
done

PGPASSWORD=$admin_password \
    psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_ADMIN_USER" \
    --dbname="$DB_NAME" --file="$sql_file" --set=ON_ERROR_STOP=1 >/dev/null

echo "papel de runtime provisionado: $DB_APP_USER ($DB_SCHEMAS)"
