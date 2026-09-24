"""Configure Django once for the whole suite.

Several suites import models at module scope. Without this they fail with
``ImproperlyConfigured: settings are not configured`` depending on which module pytest
happens to import first, which made the result order-dependent.
"""
from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
