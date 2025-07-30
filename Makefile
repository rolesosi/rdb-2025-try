build:
	docker compose build

up:
	docker compose up -d

logs:
	docker compose logs -f --tail=100

down:
	docker compose down

reset:
	docker compose down -v