.PHONY: build up down logs ingest clean

# Build the Docker image
build:
	docker compose build

# Start the dashboard
up:
	docker compose up -d
	@echo ""
	@echo "Opal is running at http://localhost:9090"
	@echo ""

# Build + start
all: build up

# Stop the dashboard
down:
	docker compose down

# Stream logs
logs:
	docker compose logs -f opal

# Ingest a CSV — usage: make ingest CSV=path/to/file.csv
ingest:
	@if [ -z "$(CSV)" ]; then \
		echo "Usage: make ingest CSV=path/to/engagement.csv"; exit 1; \
	fi
	cp "$(CSV)" csv/engagement.csv
	docker compose run --rm ingest

# Stop and remove everything including the database
clean:
	docker compose down --rmi local
	rm -f data/opal.db
