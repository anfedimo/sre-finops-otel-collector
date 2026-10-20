.PHONY: up down restart validate validate-rules verify test-pii test-rca rca demo-pago logs incident gate release rollback \
        demo-preflight demo-gitops demo-gate demo-reduccion demo-falso-500 demo-falso-200 demo-excepcion demo-trafico

COLLECTOR_IMAGE  := otel/opentelemetry-collector-contrib:0.162.0
PROMETHEUS_IMAGE := prom/prometheus:v3.13.4
PY               := .venv/bin/python
SLO_PROFILE      ?= poc
VERSION          ?= 1.1.0
PROFILE          ?= stable
SLO_FILE         ?= ../bancoplus-payments-qr/slo.yaml
TUNNELS          := scripts/eks_tunnels.sh

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

test-rca: $(PY)  ## Regresión del agente de RCA sobre trazas reales congeladas (sin infraestructura)
	$(PY) scripts/test_rca_agent.py

rca: $(PY)       ## Informe RCA de la falla más reciente vía MCP (abre y cierra su propio túnel a svc/tempo)
	$(TUNNELS) $(PY) scripts/sre_rca_agent.py $(RCA_ARGS)

# Demostración en vivo sobre EKS: un comando por demo, cada uno con sus propios túneles
demo-preflight:  ## Verificación previa: IP, nodos, Argo CD, réplicas, tráfico y túneles colgados
	scripts/demo_preflight.sh

demo-gitops:     ## Demo A: cambio manual no autorizado y reversión de Argo CD (termina solo)
	scripts/demo_gitops.sh

demo-gate: $(PY) ## Demo B: Quality Gate de Error Budget con el perfil poc (esperado: DECISION: BLOCK)
	@$(TUNNELS) $(PY) scripts/error_budget_gate.py pre-deploy --slo $(SLO_FILE) --profile poc \
	  || echo "exit 1: el pipeline se detiene aquí y no despliega"

demo-reduccion: $(PY) ## Demo C: reducción de spans del tail sampling y log-to-metrics (Prometheus)
	@$(TUNNELS) $(PY) scripts/demo_reduccion.py

demo-pago: $(PY) ## Demo D: pago de servicio público que falla en la entidad y su diagnóstico RCA (EKS)
	scripts/demo_pago_servicio.sh

demo-falso-500: $(PY) ## Demo E: rechazo de riesgo que viaja como HTTP 500 (no consume Error Budget)
	@$(TUNNELS) $(PY) scripts/sre_rca_agent.py --query '{ span.business.status_reclassified = "declined_as_5xx" }'

demo-falso-200: $(PY) ## Demo E: falla del sistema que viaja como HTTP 200 (consume Error Budget)
	@$(TUNNELS) $(PY) scripts/sre_rca_agent.py --query '{ span.business.status_reclassified = "failed_as_2xx" }'

demo-excepcion: $(PY) ## Demo F: excepción del core AS400 con cuenta y tarjeta enmascaradas
	@$(TUNNELS) $(PY) scripts/sre_rca_agent.py --query '{ event:name = "exception" }'

demo-trafico:    ## Relanza el tráfico de demostración en EKS
	kubectl -n pagos delete job demo-traffic --ignore-not-found
	kubectl apply -f deploy/eks/demo-traffic-job.yaml

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
