"""Generador de tráfico dummy: Pagos QR de Banco Plus.

Simula el journey  api-gateway → payments-qr → core-banking-as400 (vía IBM MQ)
y emite trazas OTLP con PII real-looking a propósito, para demostrar que el
Collector Gateway la destruye antes de exportar.

La latencia se simula con timestamps explícitos (sin sleep), así un solo
proceso genera cientos de TPS sin esfuerzo.
"""

import os
import random
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-gateway:4317")
TPS = float(os.getenv("TPS", "50"))
# Mezcla del tráfico (el resto es éxito rápido)
P_TECH_ERROR = float(os.getenv("P_TECH_ERROR", "0.07"))      # 5xx: timeout core / MQ caído
P_SLOW = float(os.getenv("P_SLOW", "0.05"))                  # éxito pero > 800 ms
P_BUSINESS_DECLINE = float(os.getenv("P_BUSINESS_DECLINE", "0.05"))  # 4xx: fondos insuficientes
P_HIGH_VALUE = float(os.getenv("P_HIGH_VALUE", "0.01"))      # monto ≥ 50 M COP

CHANNELS = ["app-movil", "web", "qr-comercio", "cbu-transferencia"]
NS = 1_000_000  # ms → ns


def tracer_for(service: str):
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.namespace": "pluspay",
                "deployment.environment.name": "poc-local",
                "cloud.provider": random.choice(["aws", "gcp"]),
                "team.tribe": "tribu-pagos",
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True)))
    return provider.get_tracer("bancoplus.traffic-generator")


gateway_tracer = tracer_for("api-gateway")
payments_tracer = tracer_for("payments-qr")
core_tracer = tracer_for("core-banking-as400")
propagator = TraceContextTextMapPropagator()


def fake_pan() -> str:
    return "4" + "".join(random.choices("0123456789", k=15))


def fake_account() -> str:
    return "".join(random.choices("0123456789", k=12))


def fake_document() -> str:
    return str(random.randint(10_000_000, 1_299_999_999))


def one_payment():
    r = random.random()
    if r < P_TECH_ERROR:
        outcome = "tech_error"
    elif r < P_TECH_ERROR + P_SLOW:
        outcome = "slow"
    elif r < P_TECH_ERROR + P_SLOW + P_BUSINESS_DECLINE:
        outcome = "declined"
    else:
        outcome = "ok"

    pan, account, document = fake_pan(), fake_account(), fake_document()
    email = f"cliente{random.randint(1, 99999)}@correo.com"
    amount = random.uniform(50_000_000, 200_000_000) if random.random() < P_HIGH_VALUE else random.uniform(5_000, 2_000_000)
    channel = random.choice(CHANNELS)

    core_ms = {"ok": random.uniform(40, 250), "slow": random.uniform(900, 3000),
               "declined": random.uniform(40, 200), "tech_error": random.uniform(2000, 5000)}[outcome]
    status_code = {"ok": 201, "slow": 201, "declined": 422, "tech_error": 504}[outcome]

    t0 = time.time_ns()
    t_pay = t0 + int(random.uniform(2, 8) * NS)
    t_core = t_pay + int(random.uniform(1, 5) * NS)
    t_core_end = t_core + int(core_ms * NS)
    t_pay_end = t_core_end + int(random.uniform(1, 5) * NS)
    t_end = t_pay_end + int(random.uniform(1, 4) * NS)

    root = gateway_tracer.start_span(
        "POST /v1/payments/qr",
        kind=SpanKind.SERVER,
        start_time=t0,
        attributes={
            "http.request.method": "POST",
            "http.route": "/v1/payments/qr",
            # PII "olvidada" por un desarrollador en la URL → debe caer en el barrido Regex
            "url.full": f"https://api.bancoplus.co/v1/payments/qr?account={account}&email={email}",
            "http.response.status_code": status_code,
            "payment.channel": channel,
        },
    )
    ctx = trace.set_span_in_context(root)

    pay = payments_tracer.start_span(
        "ProcessQrPayment",
        context=ctx,
        kind=SpanKind.SERVER,
        start_time=t_pay,
        attributes={
            "payment.journey": "pago-qr",
            "payment.channel": channel,
            "payment.amount": int(amount),  # COP en pesos enteros: numeric_attribute solo evalúa int
            "payment.currency": "COP",
            # PII en llaves conocidas → hash con sal
            "account.number": account,
            "customer.document": document,
            # PII en valores libres → barrido Regex
            "card.number": pan,
            "customer.email": email,
            # Nunca debe sobrevivir → eliminado por llave
            "card.cvv": f"{random.randint(100, 999)}",
            "payment.anomaly": "true" if amount >= 50_000_000 else "false",
        },
    )
    pay_ctx = trace.set_span_in_context(pay)

    # Propagación W3C Trace Context hacia el AS400 vía propiedades del mensaje MQ
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
            span.add_event("exception", {"exception.type": "CoreTimeoutException", "exception.message": msg}, timestamp=t_core_end)
            span.set_status(Status(StatusCode.ERROR, "core timeout"))
    elif outcome == "declined":
        # Rechazo de NEGOCIO: no es un error técnico, no consume Error Budget
        pay.set_attribute("payment.decline_reason", "FONDOS_INSUFICIENTES")

    core.end(end_time=t_core_end)
    pay.end(end_time=t_pay_end)
    root.end(end_time=t_end)


def main():
    print(f"Generando tráfico → {ENDPOINT} a {TPS} TPS", flush=True)
    interval = 1.0 / TPS
    sent = 0
    while True:
        start = time.monotonic()
        one_payment()
        sent += 1
        if sent % 1000 == 0:
            print(f"{sent} transacciones enviadas", flush=True)
        time.sleep(max(0.0, interval - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
