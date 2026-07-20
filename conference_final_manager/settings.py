import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

APP_NAME = "Conference Final Manager"
APP_VERSION = "1.10.6"
STATE_ARCHIVE_VERSION = 3


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    return [value.strip() for value in raw.split(",") if value.strip()]


def _env_path(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


SECRET_KEY = os.environ.get("SMS_SECRET_KEY", "local-dev-only-change-if-exposed")
DEBUG = _env_bool("SMS_DEBUG", True)
ALLOWED_HOSTS = _env_list(
    "SMS_ALLOWED_HOSTS", ["127.0.0.1", "localhost", "testserver"]
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "submissions",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "submissions.middleware.SelectiveGZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "submissions.middleware.AppTimezoneMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "conference_final_manager.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "submissions.context_processors.global_workflow_alerts",
            ],
        },
    },
]

WSGI_APPLICATION = "conference_final_manager.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _env_path("SMS_DATABASE_PATH", BASE_DIR / "db.sqlite3"),
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = _env_path("SMS_STATIC_ROOT", BASE_DIR / "staticfiles")
MEDIA_URL = "/media/"
MEDIA_ROOT = _env_path("SMS_MEDIA_ROOT", BASE_DIR / "data" / "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Local editorial workflow may upload many Final Submission PDFs/source files at once.
# Keep this finite instead of None so accidental massive uploads are still bounded.
DATA_UPLOAD_MAX_NUMBER_FILES = 5000
