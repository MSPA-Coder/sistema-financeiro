"""Configuracao do projeto Django `financeiro` (Controle Bancario).

Backend unico: PostgreSQL. Segredo e modo de depuracao vem do ambiente e nao
possuem padrao permissivo: uma implantacao sem `DJANGO_SECRET_KEY` falha ao
subir, e `DEBUG` so liga quando pedido explicitamente.
"""

import os
from pathlib import Path

from sharedauth.ui import CAMINHO_ESTATICO as SHAREDAUTH_UI

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

def _read_required_secret(name: str) -> str:
    """Lê segredo por arquivo no Compose e só aceita ambiente no modo local.

    `REQUIRE_FILE_SECRETS=true` é o contrato do Compose operacional. A queda
    para variável direta é mantida somente para comandos locais explícitos, que
    não passam por esse contrato.
    """
    path = os.environ.get(f"{name}_FILE")
    require_file = os.environ.get("REQUIRE_FILE_SECRETS", "false").lower() == "true"

    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Arquivo de segredo obrigatorio para {name} nao pode ser lido.") from exc
        if value:
            return value
        raise RuntimeError(f"Arquivo de segredo obrigatorio para {name} esta vazio.")

    if require_file:
        raise RuntimeError(f"Arquivo de segredo obrigatorio para {name} nao foi configurado.")

    value = os.environ.get(name, "")
    if value:
        return value
    raise RuntimeError(f"{name} e obrigatoria. Defina a variavel ou {name}_FILE.")


# SECURITY WARNING: keep the secret key used in production secret!
# No Compose, a chave é lida de /run/secrets e a ausência falha ao subir.
SECRET_KEY = _read_required_secret("DJANGO_SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
# O padrao e desligado: a imagem de runtime nao pode expor tracebacks so
# porque a variavel nao foi informada.
DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'

ALLOWED_HOSTS = os.environ.get(
    'ALLOWED_HOSTS',
    'localhost,127.0.0.1',
).split(',')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Apps do projeto
    'core',
    'accounts',
    'banking',
    'bank_statements',
    'transactions',
    'management',
    'reports',
    'dashboard',
    # Third party
    'django_htmx',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serve o STATIC_ROOT gerado por `collectstatic`. Em
    # desenvolvimento esse diretorio nao existe (quem serve e o app
    # `staticfiles`), entao o middleware e inserido logo abaixo apenas quando
    # ha o que servir.
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'core.htmx.HtmxFlashMessagesMiddleware',
    'core.security.ContentSecurityPolicyMiddleware',
]

ROOT_URLCONF = 'financeiro.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.app_shell',
            ],
        },
    },
]

WSGI_APPLICATION = 'financeiro.wsgi.application'

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'controle_bancario'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': _read_required_secret('POSTGRES_PASSWORD'),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': 600,
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'accounts.password_validators.ConfigurablePasswordPolicyValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/
LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# O prefixo ("sharedauth", ...) serve o CSS/JS do pacote comum sob
# static/sharedauth/ sem copia-los para dentro do repositorio -- o WhiteNoise
# aplica hash e compressao neles do mesmo jeito que aplica no restante.
STATICFILES_DIRS = ([BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []) + [
    ('sharedauth', SHAREDAUTH_UI),
]

# `STATICFILES_STORAGE` foi removido no Django 5.1: declarar o backend do
# WhiteNoise ali era silenciosamente ignorado, e a aplicacao servia os assets
# sem hash nem compressao. `STORAGES` e a chave que o Django le hoje.
#
# O backend com manifesto exige `collectstatic` previo (a etapa `migrate` do
# Compose faz isso). Em desenvolvimento os arquivos sao servidos
# direto de STATICFILES_DIRS, sem hash.
_USE_MANIFEST_STATIC = not DEBUG

if _USE_MANIFEST_STATIC:
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if _USE_MANIFEST_STATIC
            else 'django.contrib.staticfiles.storage.StaticFilesStorage'
        ),
    },
}

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
# https://docs.djangoproject.com/en/6.0/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom user model
AUTH_USER_MODEL = 'accounts.AppUser'

AUTHENTICATION_BACKENDS = [
    'accounts.auth_backends.CaseInsensitiveUsernameBackend',
    'accounts.auth_backends.AppPermissionBackend',
]
LOGIN_URL = '/login'
LOGIN_REDIRECT_URL = '/reports/upcoming-movements/'
LOGOUT_REDIRECT_URL = '/login'

# Endurecimento de transporte. E uma propriedade da implantacao, nao do modo de
# depuracao: a instalacao padrao publica em loopback sobre HTTP, onde cookies
# `Secure` simplesmente nao seriam enviados e o redirect para HTTPS levaria a
# uma porta que ninguem atende. Ligue junto com um proxy TLS a frente.
USE_HTTPS = os.environ.get('USE_HTTPS', 'False').lower() == 'true'
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

if USE_HTTPS:
    if not CSRF_TRUSTED_ORIGINS:
        raise RuntimeError(
            'CSRF_TRUSTED_ORIGINS e obrigatoria quando USE_HTTPS=True. '
            'Informe as origens HTTPS publicas separadas por virgula.'
        )
    if any(not origin.startswith('https://') for origin in CSRF_TRUSTED_ORIGINS):
        raise RuntimeError('CSRF_TRUSTED_ORIGINS deve conter somente origens HTTPS validas.')

# Session settings
SESSION_COOKIE_NAME = 'controle_bancario_session'
SESSION_COOKIE_SECURE = USE_HTTPS
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = int(os.environ.get('SESSION_COOKIE_AGE', 86400))  # 24 horas padrão

# Security settings
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
# `same-origin`, nao `no-referrer`. Sob `no-referrer` o navegador serializa o
# cabecalho `Origin` como `null` tambem em POST de mesma origem (Fetch spec), e
# a protecao CSRF do Django recusa a requisicao com 403 mesmo com o token
# correto. `same-origin` nao vaza referrer para fora da origem, que e o que
# importa, e preserva o `Origin` de que o CSRF depende.
SECURE_REFERRER_POLICY = 'same-origin'
CSRF_COOKIE_SECURE = USE_HTTPS
CSRF_COOKIE_HTTPONLY = True

# Content Security Policy (CSP): aplicada via core.security.ContentSecurityPolicyMiddleware
# em todo ambiente (dev e prod), com a mesma politica estrita (sem inline).
if USE_HTTPS:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # O proxy TLS informa o esquema original; sem isso o Django veria HTTP e
    # entraria em loop de redirect.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Logging e Observabilidade
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        # Rotacionado: o arquivo vive num volume do container e cresceria sem
        # limite com FileHandler puro.
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'financeiro.log',
            'maxBytes': int(os.environ.get('LOG_MAX_BYTES', 5 * 1024 * 1024)),
            'backupCount': int(os.environ.get('LOG_BACKUP_COUNT', 5)),
            'encoding': 'utf-8',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'financeiro': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# Vocabulario financeiro (status, tipos de lancamento e tipos de operacao) fica
# em `core.domain.finance`, que e a fonte unica consumida por services e views.

# Limites defensivos para upload de extratos bancários (Bancos > Importações).
MAX_BANK_STATEMENT_SIZE_BYTES = int(os.environ.get('MAX_BANK_STATEMENT_SIZE_BYTES', 5 * 1024 * 1024))
MAX_BANK_STATEMENT_ROWS = int(os.environ.get('MAX_BANK_STATEMENT_ROWS', 5000))

# Limite defensivo para upload de comprovantes (Bancos > Anexos).
MAX_ATTACHMENT_SIZE_BYTES = int(os.environ.get('MAX_ATTACHMENT_SIZE_BYTES', 10 * 1024 * 1024))
