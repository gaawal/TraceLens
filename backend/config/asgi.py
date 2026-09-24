import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_asgi_application()

# Uvicorn doesn't add Django's staticfiles runserver wrapper automatically.
# Keep development behaviour equivalent to manage.py runserver when DEBUG=True.
try:
    from django.conf import settings
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    if settings.DEBUG:
        application = ASGIStaticFilesHandler(application)
except ImportError:
    pass
