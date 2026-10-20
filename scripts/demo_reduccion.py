#!/usr/bin/env python3
"""FinOps en vivo: reducción de spans del tail sampling y log-to-metrics, leídos de Prometheus (PROM_URL).

Misma PromQL que se muestra en Grafana · Explore. Exit codes: 0 = datos presentes · 1 = sin datos
"""
import json
import os
import sys
import urllib.parse
import urllib.request

PROM = os.getenv("PROM_URL", "http://localhost:9090")
WINDOW = "5m"
RECEIVED = f'sum(rate(otelcol_receiver_accepted_spans{{job="otel-gateway"}}[{WINDOW}]))'
SENT_BY_EXPORTER = f"sum by (exporter) (rate(otelcol_exporter_sent_spans[{WINDOW}]))"
LOG_ERRORS_PER_MIN = f"sum(rate(app_log_errors_total[{WINDOW}])) * 60"


def query(promql):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]["result"]


def main():
    received = query(RECEIVED)
    sent = {s["metric"].get("exporter", "?"): float(s["value"][1]) for s in query(SENT_BY_EXPORTER)}
    if not received or not sent:
        print("sin datos en Prometheus: revisa el tráfico de demostración (kubectl -n pagos get job demo-traffic)")
        return 1
    rx = float(received[0]["value"][1])
    print(f"FinOps · tail sampling en el Gateway (ventana {WINDOW})")
    print(f"  spans recibidos            {rx:8.1f} /s")
    for exporter, value in sorted(sent.items()):
        print(f"  spans enviados {exporter:<24} {value:8.1f} /s  ({1 - value / rx:6.1%} menos)")
    backend = sent.get("otlp_grpc/backend-otel")
    if backend is not None:
        print(f"  REDUCCIÓN hacia el backend  {1 - backend / rx:6.1%}  (errores, lentas y alto valor se conservan al 100 %)")
    errors = query(LOG_ERRORS_PER_MIN)
    if errors:
        print(f"Log-to-metrics · errores de log convertidos en métrica: {float(errors[0]['value'][1]):.1f} /min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
