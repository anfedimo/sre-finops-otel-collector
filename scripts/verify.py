#!/usr/bin/env python3
"""Quality Gates de la plataforma de telemetría.

  PII-001     Cero PII en backends externos (PCI-DSS / SFC CE 007/2018)  → bloqueante
  BIZ-001     Estado del span consistente con business.outcome            → bloqueante
  FINOPS-001  Reducción de ingesta de trazas ≥ 30%                        → bloqueante
  SLO-001     Disponibilidad técnica de payments-qr ≥ 99.95%              → informativo

Exit codes: 0 = PASS · 1 = FAIL bloqueante · 2 = sin datos suficientes
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROM = os.getenv("PROM_URL", "http://localhost:9090")
BACKENDS = {
    "dynatrace": os.getenv("APM_LEGACY_URL", "http://localhost:16686"),
    "otel-backend": os.getenv("OTEL_BACKEND_URL", "http://localhost:16687"),
}
SERVICE = "payments-qr"
MIN_INGEST_REDUCTION = float(os.getenv("MIN_INGEST_REDUCTION", "30"))
SLO_AVAILABILITY = float(os.getenv("SLO_AVAILABILITY", "99.95"))

PII_PATTERNS = {
    "PAN": re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
    "EMAIL": re.compile(r"[A-Za-z0-9._%+-]+(@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ID_NUM": re.compile(r"\b\d{8,12}\b"),
}
FORBIDDEN_KEYS = {"card.cvv"}
EVIDENCE_KEYS = ("card.number", "account.number", "customer.document", "customer.email", "card.cvv")

PASS, FAIL, WARN, NODATA = "PASS", "FAIL", "WARN", "NO_DATA"


def log(gate, status, msg):
    print(f"[{status:<7}] {gate:<10} {msg}")


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def prom_query(query):
    return get_json(f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query}))["data"]["result"]


def prom_scalar(query):
    result = prom_query(query)
    return float(result[0]["value"][1]) if result else 0.0


def fetch_spans(base):
    now = datetime.now(timezone.utc)
    query = urllib.parse.urlencode({
        "query.service_name": SERVICE,
        "query.start_time_min": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query.start_time_max": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query.search_depth": 200,
    })
    result = get_json(f"{base}/api/v3/traces?{query}")["result"]["resourceSpans"]
    return [span for rs in result for ss in rs["scopeSpans"] for span in ss["spans"]]


def attr_map(attributes):
    return {a["key"]: next(iter(a["value"].values()), "") for a in attributes or []}


def string_values(attributes):
    # Tipos numéricos (montos, status codes) fuera de alcance del escaneo
    return [a["value"]["stringValue"] for a in attributes or [] if "stringValue" in a["value"]]


def gate_pii():
    status = PASS
    for backend, base in BACKENDS.items():
        spans = fetch_spans(base)
        if not spans:
            log("PII-001", NODATA, f"{backend}: 0 spans en ventana de 1h")
            status = NODATA if status == PASS else status
            continue
        values, evidence, violations = [], None, 0
        for span in spans:
            attrs = attr_map(span.get("attributes"))
            for key in FORBIDDEN_KEYS & attrs.keys():
                log("PII-001", FAIL, f"{backend}: llave prohibida presente ({key})")
                violations += 1
            values += string_values(span.get("attributes")) + [span["name"]]
            values += [v for e in span.get("events", []) for v in string_values(e.get("attributes"))]
            if evidence is None and "card.number" in attrs:
                evidence = attrs
        for label, pattern in PII_PATTERNS.items():
            hits = [v for v in values if pattern.search(v)]
            if hits:
                log("PII-001", FAIL, f"{backend}: {len(hits)} valores {label} en claro · muestra={hits[0][:60]}")
                violations += len(hits)
        if violations:
            status = FAIL
        else:
            log("PII-001", PASS, f"{backend}: {len(spans)} spans escaneados, 0 violaciones")
        if evidence:
            for key in EVIDENCE_KEYS:
                print(f"{'':<20}{key:<20} {evidence.get(key, '<removed>')}")
    return status


def gate_business():
    sel = f'traces_span_metrics_calls_total{{service_name="{SERVICE}",span_name="ProcessQrPayment"'

    def calls(extra=""):
        return prom_scalar(f"sum({sel}{',' + extra if extra else ''}}})")

    total = calls()
    if total == 0:
        log("BIZ-001", NODATA, "sin métricas RED")
        return NODATA
    http_5xx = calls('http_response_status_code=~"5.."')
    technical = calls('status_code="STATUS_CODE_ERROR"')
    false_5xx = calls('business_outcome="declined",http_response_status_code=~"5.."')
    hidden_2xx = calls('business_outcome="failed",http_response_status_code=~"2.."')
    misclassified = (calls('business_outcome="declined",status_code="STATUS_CODE_ERROR"')
                     + calls('business_outcome="failed",status_code!="STATUS_CODE_ERROR"'))
    status = PASS if misclassified == 0 else FAIL
    log("BIZ-001", status, f"misclassified={misclassified:,.0f} · tasa_5xx_http={100 * http_5xx / total:.2f}% "
                           f"vs tasa_error_tecnico={100 * technical / total:.3f}%")
    print(f"{'':<20}{'rechazos 5xx reclasificados':<30} {false_5xx:>10,.0f}  (no consumen Error Budget)")
    print(f"{'':<20}{'errores ocultos en 2xx':<30} {hidden_2xx:>10,.0f}  (consumen Error Budget)")
    for outcome in ("approved", "declined", "failed"):
        n = calls(f'business_outcome="{outcome}"')
        print(f"{'':<20}{'business.outcome=' + outcome:<30} {n:>10,.0f}  ({100 * n / total:.2f}%)")
    return status


def gate_finops():
    received = prom_scalar('sum({__name__=~"otelcol_receiver_accepted_spans(_total)?"})')
    exported = prom_scalar('sum({__name__=~"otelcol_exporter_sent_spans(_total)?",exporter="otlp_grpc/backend-otel"})')
    if received == 0:
        log("FINOPS-001", NODATA, "sin métricas del Gateway")
        return NODATA
    reduction = 100 * (1 - exported / received)
    status = PASS if reduction >= MIN_INGEST_REDUCTION else FAIL
    log("FINOPS-001", status, f"reducción={reduction:.1f}% (umbral ≥ {MIN_INGEST_REDUCTION:.0f}%) "
                              f"received={received:,.0f} exported={exported:,.0f}")
    retained = prom_query('sum by (policy) ({__name__=~"otelcol_processor_tail_sampling_count_traces_sampled(_total)?",sampled="true"})')
    for r in sorted(retained, key=lambda r: r["metric"]["policy"]):
        print(f"{'':<20}{r['metric']['policy']:<22} traces={float(r['value'][1]):,.0f}")
    return status


def gate_slo():
    selector = f'traces_span_metrics_calls_total{{service_name="{SERVICE}",span_name="ProcessQrPayment"'
    total = prom_scalar(f"sum({selector}}})")
    errors = prom_scalar(f'sum({selector},status_code="STATUS_CODE_ERROR"}})')
    if total == 0:
        log("SLO-001", NODATA, "sin métricas RED")
        return NODATA
    availability = 100 * (1 - errors / total)
    status = PASS if availability >= SLO_AVAILABILITY else WARN
    log("SLO-001", status, f"availability={availability:.2f}% (SLO {SLO_AVAILABILITY}%) "
                           f"requests={total:,.0f} errors={errors:,.0f}")
    return status


if __name__ == "__main__":
    print(f"telemetry-quality-gates · service={SERVICE} · {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    results = [gate_pii(), gate_business(), gate_finops(), gate_slo()]
    if FAIL in results:
        print("RESULT: FAIL")
        sys.exit(1)
    if NODATA in results:
        print("RESULT: NO_DATA (reintentar tras 60s de tráfico)")
        sys.exit(2)
    print("RESULT: PASS")
