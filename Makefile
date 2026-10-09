.PHONY: up down restart validate verify logs incident

COLLECTOR_IMAGE := otel/opentelemetry-collector-contrib:0.162.0

up:            ## Levanta el entorno completo
	docker compose up -d --build

down:          ## Destruye el entorno
	docker compose down -v

restart:       ## Recarga el Collector tras editar su config
	docker compose restart otel-gateway

validate:      ## Valida la config del Collector con el binario real
	docker run --rm -e PII_HASH_SALT=validate \
	  -v "$(CURDIR)/collector/otel-collector-config.yaml:/etc/otelcol/config.yaml:ro" \
	  $(COLLECTOR_IMAGE) validate --config=/etc/otelcol/config.yaml

verify:        ## Evidencia: PII, ahorro FinOps y SLI
	python3 scripts/verify.py

logs:          ## Logs del Gateway
	docker compose logs -f otel-gateway

incident:      ## Simula un mal despliegue: 30% de errores técnicos
	P_TECH_ERROR=0.30 docker compose up -d traffic-generator
