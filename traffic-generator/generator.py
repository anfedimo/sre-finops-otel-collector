"""Load generator · journey pago-qr.

  api-gateway → payments-qr → fraud-api (score de riesgo → fraud-db) → core-banking-as400 (IBM MQ)

Emula la telemetría de servicios Java autoinstrumentados con el agente OpenTelemetry:
  - Spans HTTP con estado derivado del código de respuesta (5xx → ERROR), como lo hace el agente.
  - Cabeceras de negocio capturadas por configuración del agente
    (OTEL_INSTRUMENTATION_HTTP_SERVER_CAPTURE_RESPONSE_HEADERS) → http.response.header.x-business-*.
  - Anti-patrones de producción: rechazos de riesgo devueltos como HTTP 500 y errores
    de sistema devueltos como HTTP 200.

El Gateway normaliza estas señales a business.* y corrige el estado del span (PII-001, BIZ-001,
FINOPS-001, SLO-001). Latencia inyectada vía timestamps explícitos.
"""

import os
import random
import time
from collections import Counter

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-gateway:4317")
TPS = float(os.getenv("TPS", "50"))
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
REPORT_EVERY = int(os.getenv("REPORT_EVERY", "1000"))

# Mix transaccional (el resto: aprobado dentro de SLO)
MIX = {
    "tech_error": float(os.getenv("P_TECH_ERROR", "0.0001")),            # 504 timeout core
    "hidden_error_200": float(os.getenv("P_HIDDEN_ERROR_200", "0.0001")),  # 200 con error de sistema
    "risk_decline_500": float(os.getenv("P_RISK_DECLINE_500", "0.03")),    # rechazo de riesgo como 500
    "funds_decline_200": float(os.getenv("P_FUNDS_DECLINE_200", "0.04")),  # fondos insuficientes en 200
    "slow": float(os.getenv("P_SLOW", "0.002")),                         # aprobado > 800 ms
}
P_HIGH_VALUE = float(os.getenv("P_HIGH_VALUE", "0.01"))

HIGH_VALUE_COP = 50_000_000
CHANNELS = ["app-movil", "web", "qr-comercio", "cbu-transferencia"]
NS = 1_000_000

# outcome → (HTTP status, x-business-outcome, x-business-reason)
CONTRACT = {
    "approved": (201, "approved", "OK"),
    "slow": (201, "approved", "OK"),
    "funds_decline_200": (200, "declined", "FONDOS_INSUFICIENTES"),
    "risk_decline_500": (500, "declined", "RIESGO_ALTO"),
    "hidden_error_200": (200, "failed", "ERROR_SISTEMA"),
    "tech_error": (504, "failed", "CORE_TIMEOUT"),
}


def tracer_for(service: str):
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.namespace": "pluspay",
                "service.version": APP_VERSION,
                "deployment.environment.name": "poc-local",
                "cloud.provider": "aws",
                "team.tribe": "tribu-pagos",
                "telemetry.distro.name": "opentelemetry-java-instrumentation",
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True)))
    return provider.get_tracer("io.opentelemetry.javaagent")


gateway_tracer = tracer_for("api-gateway")
payments_tracer = tracer_for("payments-qr")
fraud_tracer = tracer_for("fraud-api")
core_tracer = tracer_for("core-banking-as400")
propagator = TraceContextTextMapPropagator()


def fake_pan() -> str:
    return "4" + "".join(random.choices("0123456789", k=15))


def fake_account() -> str:
    return "".join(random.choices("0123456789", k=12))


def fake_document() -> str:
    return str(random.randint(10_000_000, 1_299_999_999))


def pick_outcome() -> str:
    r, acc = random.random(), 0.0
    for outcome, p in MIX.items():
        acc += p
        if r < acc:
            return outcome
    return "approved"


def business_headers(outcome: str) -> dict:
    # Formato del agente Java: http.response.header.<nombre> como arreglo de strings
    _, result, reason = CONTRACT[outcome]
    return {
        "http.response.header.x-business-operation": ["pago_qr"],
        "http.response.header.x-business-outcome": [result],
        "http.response.header.x-business-reason": [reason],
    }


def http_status(span, code: int):
    span.set_attribute("http.response.status_code", code)
    if code >= 500:
        span.set_status(Status(StatusCode.ERROR))


def one_payment() -> str:
    outcome = pick_outcome()
    status_code = CONTRACT[outcome][0]

    pan, account, document = fake_pan(), fake_account(), fake_document()
    email = f"cliente{random.randint(1, 99999)}@correo.com"
    amount = int(random.uniform(HIGH_VALUE_COP, 200_000_000) if random.random() < P_HIGH_VALUE
                 else random.uniform(5_000, 2_000_000))
    channel = random.choice(CHANNELS)
    risk_score = round(random.uniform(0.86, 0.99), 2) if outcome == "risk_decline_500" else round(random.uniform(0.01, 0.40), 2)
    calls_core = outcome != "risk_decline_500"

    fraud_ms = random.uniform(15, 80)
    core_ms = {"slow": random.uniform(900, 3000), "tech_error": random.uniform(2000, 5000)}.get(outcome, random.uniform(40, 250))

    t0 = time.time_ns()
    t_pay = t0 + int(random.uniform(2, 8) * NS)
    t_fraud = t_pay + int(random.uniform(1, 3) * NS)
    t_db = t_fraud + int(random.uniform(1, 3) * NS)
    t_db_end = t_db + int(fraud_ms * 0.6 * NS)
    t_fraud_end = t_db_end + int(fraud_ms * 0.4 * NS)
    t_core = t_fraud_end + int(random.uniform(1, 5) * NS)
    t_core_end = t_core + int(core_ms * NS) if calls_core else t_core
    t_pay_end = t_core_end + int(random.uniform(1, 5) * NS)
    t_end = t_pay_end + int(random.uniform(1, 4) * NS)

    root = gateway_tracer.start_span(
        "POST /v1/payments/qr",
        kind=SpanKind.SERVER,
        start_time=t0,
        attributes={
            "http.request.method": "POST",
            "http.route": "/v1/payments/qr",
            # Caso de prueba: PII en query string (cobertura del barrido Regex)
            "url.full": f"https://api.bancoplus.co/v1/payments/qr?account={account}&email={email}",
            "payment.channel": channel,
            **business_headers(outcome),
        },
    )
    http_status(root, status_code)
    ctx = trace.set_span_in_context(root)

    pay = payments_tracer.start_span(
        "ProcessQrPayment",
        context=ctx,
        kind=SpanKind.SERVER,
        start_time=t_pay,
        attributes={
            "http.request.method": "POST",
            "http.route": "/internal/payments/qr",
            "payment.channel": channel,
            "payment.amount": amount,
            "payment.currency": "COP",
            "account.number": account,
            "customer.document": document,
            "card.number": pan,
            "customer.email": email,
            "card.cvv": f"{random.randint(100, 999)}",
            "payment.anomaly": "true" if amount >= HIGH_VALUE_COP else "false",
            **business_headers(outcome),
        },
    )
    http_status(pay, status_code)
    pay_ctx = trace.set_span_in_context(pay)

    fraud = fraud_tracer.start_span(
        "POST /v1/risk/score",
        context=pay_ctx,
        kind=SpanKind.SERVER,
        start_time=t_fraud,
        attributes={"http.request.method": "POST", "http.route": "/v1/risk/score", "http.response.status_code": 200,
                    "risk.score": risk_score, "risk.decision": "decline" if risk_score >= 0.85 else "approve"},
    )
    fraud_ctx = trace.set_span_in_context(fraud)
    db = fraud_tracer.start_span(
        "SELECT fraud.risk_rules",
        context=fraud_ctx,
        kind=SpanKind.CLIENT,
        start_time=t_db,
        attributes={"db.system.name": "postgresql", "db.namespace": "fraud", "db.operation.name": "SELECT"},
    )
    db.end(end_time=t_db_end)
    fraud.end(end_time=t_fraud_end)

    if outcome == "risk_decline_500":
        # Anti-patrón: decisión de negocio propagada como excepción → HTTP 500
        for span in (pay, root):
            span.add_event("exception", {"exception.type": "RiskDeclinedException",
                                         "exception.message": f"Operación declinada por score de riesgo {risk_score}"},
                           timestamp=t_fraud_end)

    if calls_core:
        mq_properties: dict = {}
        propagator.inject(mq_properties, context=pay_ctx)
        core = core_tracer.start_span(
            "MQPUT PAGOS.QR.REQ",
            context=pay_ctx,
            kind=SpanKind.PRODUCER,
            start_time=t_core,
            attributes={
                "messaging.system": "ibmmq",
                "messaging.destination.name": "PAGOS.QR.REQ",
                "messaging.message.traceparent": mq_properties.get("traceparent", ""),
                "peer.service": "as400-core",
            },
        )
        if outcome == "tech_error":
            msg = f"Timeout esperando respuesta del core AS400 para cuenta {account} tarjeta {pan}"
            for span in (core, pay, root):
                span.add_event("exception", {"exception.type": "CoreTimeoutException", "exception.message": msg},
                               timestamp=t_core_end)
            core.set_status(Status(StatusCode.ERROR))
        core.end(end_time=t_core_end)

    pay.end(end_time=t_pay_end)
    root.end(end_time=t_end)
    return outcome


def main():
    mix = " ".join(f"{k}:{v}" for k, v in MIX.items())
    print(f"[load-gen] start version={APP_VERSION} target={ENDPOINT} tps={TPS:g} mix={mix} high_value:{P_HIGH_VALUE}",
          flush=True)
    interval = 1.0 / TPS
    stats = Counter()
    while True:
        start = time.monotonic()
        stats[one_payment()] += 1
        total = sum(stats.values())
        if total % REPORT_EVERY == 0:
            breakdown = " ".join(f"{k}={v}" for k, v in sorted(stats.items()))
            print(f"[load-gen] sent={total} {breakdown}", flush=True)
        time.sleep(max(0.0, interval - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
