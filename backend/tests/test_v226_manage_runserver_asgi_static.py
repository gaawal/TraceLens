from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manage_runserver_routes_to_uvicorn_asgi_without_affecting_other_commands():
    manage = (ROOT / "manage.py").read_text(encoding="utf-8")
    assert 'sys.argv[1] == "runserver"' in manage
    assert 'uvicorn.run(' in manage
    assert '"config.asgi:application"' in manage
    assert 'execute_from_command_line(sys.argv)' in manage


def test_asgi_devserver_keeps_debug_staticfiles_support():
    asgi = (ROOT / "config/asgi.py").read_text(encoding="utf-8")
    assert "get_asgi_application" in asgi
    assert "ASGIStaticFilesHandler" in asgi
    assert "if settings.DEBUG" in asgi


def test_uvicorn_is_a_declared_backend_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "uvicorn[standard]" in requirements


def test_runserver_does_not_start_watchfiles_unless_reload_is_explicit():
    manage = (ROOT / "manage.py").read_text(encoding="utf-8")
    settings = (ROOT / "config/settings.py").read_text(encoding="utf-8")
    assert 'reload_enabled = "--reload" in args' in manage
    assert 'reload=reload_enabled' in manage
    assert 'access_log=False' in manage
    assert 'log_level="warning"' in manage
    assert '"watchfiles.main": {"handlers": [], "level": "ERROR"' in settings
    assert '"tracelens.http": {"handlers": ["console", "file"], "level": "WARNING"' in settings


def test_make_run_has_no_reload_or_access_log_noise():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "--reload" not in makefile
    assert "--no-access-log" in makefile
