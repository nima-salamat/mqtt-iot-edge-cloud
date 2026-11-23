from pathlib import Path
from urllib.parse import unquote, urlparse
import os

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DJANGO_DEBUG", "0").lower() in {"1", "true", "yes"}
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-unsafe-key-change-before-deployment")
_configured_allowed_hosts = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,api").split(",")
    if host.strip()
]
# MediaMTX calls Django directly over the private Compose network using the
# service hostname "api". Keep that internal Host header valid even when an
# operator supplies a public-only DJANGO_ALLOWED_HOSTS value.
ALLOWED_HOSTS = list(dict.fromkeys([*_configured_allowed_hosts, "api"]))
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard.apps.DashboardConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "iotlab.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]
WSGI_APPLICATION = "iotlab.wsgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_MODE = os.environ.get("DATABASE_MODE", "").strip().lower()
if not DATABASE_MODE:
    # Preserve the previous behavior for deployments that only define DATABASE_URL.
    DATABASE_MODE = "postgres" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"

if DATABASE_MODE == "postgres":
    if not DATABASE_URL.startswith(("postgres://", "postgresql://")):
        raise ImproperlyConfigured(
            "DATABASE_MODE=postgres requires a valid DATABASE_URL."
        )
    parsed = urlparse(DATABASE_URL)
    database_name = unquote(parsed.path.lstrip("/"))
    if not parsed.hostname or not database_name:
        raise ImproperlyConfigured("DATABASE_URL must include a database host and name.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database_name,
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"connect_timeout": 10},
        }
    }
elif DATABASE_MODE == "sqlite":
    DATABASE_PATH = os.environ.get(
        "DATABASE_PATH", str(BASE_DIR / "data" / "db.sqlite3")
    ).strip()
    if not DATABASE_PATH:
        raise ImproperlyConfigured("DATABASE_MODE=sqlite requires a non-empty DATABASE_PATH.")
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DATABASE_PATH,
            "OPTIONS": {"timeout": 30},
        }
    }
else:
    raise ImproperlyConfigured(
        "Unsupported DATABASE_MODE. Choose 'sqlite' or 'postgres'."
    )

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(BASE_DIR / "data" / "media")))
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
MEDIA_URL = "/media/"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
]
LANGUAGE_CODE = "fa"
TIME_ZONE = "Asia/Tehran"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 180 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "9001"))
MQTT_WS_PATH = os.environ.get("MQTT_WS_PATH", "/mqtt")
MQTT_USERNAME = os.environ.get("MQTT_SERVER_USERNAME", os.environ.get("MQTT_USERNAME", ""))
MQTT_PASSWORD = os.environ.get("MQTT_SERVER_PASSWORD", os.environ.get("MQTT_PASSWORD", ""))
MQTT_TLS = os.environ.get("MQTT_TLS", "0").lower() in {"1", "true", "yes"}
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "iotlab").strip("/")
