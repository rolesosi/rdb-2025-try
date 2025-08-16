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

up-build:
	docker compose up -d --build

monitor:
	docker stats rdb-2025-worker rdb-2025-api2 rdb-2025-api1 rdb-2025-nginx rdb-2025-redis