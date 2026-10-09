#!/usr/bin/env python3
"""Verificación de la PoC: genera la evidencia para la presentación.

1. Privacidad: busca PII sin enmascarar en lo que llegó a AMBOS backends.
2. FinOps:     compara spans recibidos vs. exportados por el Gateway.
3. SLO:        calcula la tasa de error técnica sobre el 100% del tráfico.

Sale con código 1 si encuentra PII filtrada (sirve como gate en CI).
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROM = "http://localhost:9090"
BACKENDS = {"APM legacy": "http://localhost:16686", "SaaS nuevo": "http://localhost:16687"}

PII_PATTERNS = {
    "PAN sin enmascarar": re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
    "Email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "Cuenta/cédula": re.compile(r"\b\d{8,12}\b"),
}
FORBIDDEN_KEYS = {"card.cvv"}


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def prom(query):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    result = get_json(url)["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def fetch_spans(base):
    """Consulta la API v3 de Jaeger (formato OTLP) y devuelve los spans de payments-qr."""
    now = datetime.now(timezone.utc)
    query = urllib.parse.urlencode({
        "query.service_name": "payments-qr",
        "query.start_time_min": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query.start_time_max": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query.search_depth": 200,
    })
    result = get_json(f"{base}/api/v3/traces?{query}")["result"]["resourceSpans"]
    return [span for rs in result for ss in rs["scopeSpans"] for span in ss["spans"]]


def attr_values(attributes):
    return {a["key"]: next(iter(a["value"].values()), "") for a in attributes or []}


def string_values(attributes):
    # Solo strings: montos (double) o status codes (int) no son PII
    return [a["value"]["stringValue"] for a in attributes or [] if "stringValue" in a["value"]]


def check_privacy():
    print("\n== 1. Privacidad regulatoria (PII Masking en el borde) ==")
    leaks = 0
    for name, base in BACKENDS.items():
        spans = fetch_spans(base)
        values, sample = [], None
        for span in spans:
            tags = attr_values(span.get("attributes"))
            for key in FORBIDDEN_KEYS & tags.keys():
                print(f"  [FUGA] {name}: llave prohibida '{key}' presente")
                leaks += 1
            values += string_values(span.get("attributes")) + [span["name"]]
            values += [v for e in span.get("events", []) for v in string_values(e.get("attributes"))]
            if sample is None and "card.number" in tags:
                sample = tags
        for label, pattern in PII_PATTERNS.items():
            hits = [v for v in values if pattern.search(v)]
            if hits:
                print(f"  [FUGA] {name}: {label} → {hits[0][:80]}")
                leaks += len(hits)
        print(f"  {name}: {len(spans)} spans revisados")
        if sample:
            for key in ("card.number", "account.number", "customer.document", "customer.email", "card.cvv"):
                print(f"    {key:<20} = {sample.get(key, '(eliminado)')}")
    print("  RESULTADO:", "OK, cero PII fuera del banco" if leaks == 0 else f"FALLA, {leaks} fugas")
    return leaks


def check_finops():
    print("\n== 2. FinOps (Tail-Based Sampling) ==")
    # El sufijo _total depende de la versión del Collector; se aceptan ambos
    received = prom('sum({__name__=~"otelcol_receiver_accepted_spans(_total)?"})')
    exported = prom('sum({__name__=~"otelcol_exporter_sent_spans(_total)?",exporter="otlp_grpc/saas-nuevo"})')
    if received == 0:
        print("  Aún no hay métricas; espera ~30 s y vuelve a ejecutar")
        return
    print(f"  Spans recibidos por el Gateway : {received:,.0f}")
    print(f"  Spans exportados al SaaS       : {exported:,.0f}")
    print(f"  Reducción de ingesta           : {100 * (1 - exported / received):.1f}%")
    print("  Trazas retenidas por política (100% de errores, lentas y anomalías):")
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode(
        {"query": 'sum by (policy) ({__name__=~"otelcol_processor_tail_sampling_count_traces_sampled(_total)?",sampled="true"})'})
    for r in sorted(get_json(url)["data"]["result"], key=lambda r: r["metric"]["policy"]):
        print(f"    {r['metric']['policy']:<22} {float(r['value'][1]):>8,.0f}")


def check_slo():
    print("\n== 3. SLI de disponibilidad (sobre el 100% del tráfico, antes del muestreo) ==")
    base = 'traces_span_metrics_calls_total{service_name="payments-qr",span_name="ProcessQrPayment"'
    total = prom(f"sum({base}}})")
    errors = prom(f'sum({base},status_code="STATUS_CODE_ERROR"}})')
    if total == 0:
        print("  Aún no hay métricas RED; espera ~30 s y vuelve a ejecutar")
        return
    print(f"  Pagos procesados: {total:,.0f}   Errores técnicos: {errors:,.0f}")
    print(f"  Disponibilidad  : {100 * (1 - errors / total):.2f}%  (SLO objetivo 99.95%)")


if __name__ == "__main__":
    leaks = check_privacy()
    check_finops()
    check_slo()
    sys.exit(1 if leaks else 0)
