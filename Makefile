.PHONY: up down restart validate validate-rules verify test-pii logs incident gate release rollback

COLLECTOR_IMAGE  := otel/opentelemetry-collector-contrib:0.162.0
PROMETHEUS_IMAGE := prom/prometheus:v3.13.4
PY               := .venv/bin/python
SLO_PROFILE      ?= poc
VERSION          ?= 1.1.0
PROFILE          ?= stable

export SLO_PROFILE

up:              ## Despliegue del stack
	docker compose up -d --build

down:            ## Teardown
	docker compose down -v
	rm -rf .deploy

restart:         ## Recarga del Gateway tras cambios de config
	docker compose restart otel-gateway

validate:        ## Validación de la config contra el binario del Collector
	docker run --rm -e PII_HASH_SALT=validate -e DT_OTLP_ENDPOINT=http://validate:4318 \
	  -e DT_API_TOKEN=validate -e OTEL_BACKEND_ENDPOINT=validate:4317 \
	  -v "$(CURDIR)/collector/otel-collector-config.yaml:/etc/otelcol/config.yaml:ro" \
	  $(COLLECTOR_IMAGE) validate --config=/etc/otelcol/config.yaml

validate-rules:  ## Validación de reglas SLO con promtool
	docker run --rm --entrypoint promtool -v "$(CURDIR)/prometheus:/p:ro" \
	  $(PROMETHEUS_IMAGE) check rules /p/rules/slo-payments-qr.yaml

verify:          ## Quality Gates de telemetría (PII, FinOps, SLO)
	python3 scripts/verify.py

test-pii: $(PY)  ## Regresión de las regex de PII contra la config del Collector (sin infraestructura)
	$(PY) scripts/test_pii_redaction.py

logs:
	docker compose logs -f otel-gateway

incident:        ## Inyección de fallo: 30% de errores técnicos
	P_TECH_ERROR=0.30 docker compose up -d traffic-generator

$(PY): scripts/requirements.txt
	python3 -m venv .venv && .venv/bin/pip install -q -r scripts/requirements.txt
	@touch $(PY)

gate: $(PY)      ## Quality Gate pre-deploy (Error Budget + burn rate)
	$(PY) scripts/error_budget_gate.py pre-deploy

release: $(PY)   ## Pipeline local: gate → deploy → canary → rollback si falla
	$(PY) scripts/error_budget_gate.py pre-deploy
	scripts/deploy.sh $(VERSION) $(PROFILE)
	$(PY) scripts/error_budget_gate.py canary --version $(VERSION) || (scripts/deploy.sh --rollback && exit 1)

rollback:        ## Rollback manual a la versión previa
	scripts/deploy.sh --rollback
