.PHONY: install migrate run test schema check

install:
	python -m pip install -r requirements.txt

migrate:
	python manage.py migrate

run:
	uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --no-access-log

test:
	python manage.py test

schema:
	python manage.py spectacular --validate --file schema.yml

check:
	python manage.py check
