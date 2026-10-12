#!/usr/bin/env python3
"""
SRE-RCA-001 · Regresión del agente de RCA sin infraestructura.

Fixtures: trazas reales de Tempo (EKS), ya sanitizadas por el Gateway, en scripts/fixtures/rca/.
Valida la clasificación de las trampas transaccionales, el impacto en el Error Budget y los guardrails
(allowlist de solo lectura, minimización de datos y bloqueo fail-closed de PII hacia el modelo).

Exit codes: 0 = PASS · 1 = FAIL
"""
import asyncio
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sre_rca_agent as agent  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "rca"
results: list[tuple[bool, str]] = []


def check(condition: bool, name: str) -> None:
    results.append((condition, name))


def journey(name: str) -> agent.Journey:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return agent.parse_trace(data["trace_id"], data)


# Trampas transaccionales sobre trazas reales
j = journey("failed_as_2xx")
v = agent.classify(j)
check(v.clasificacion == "FALLA_TECNICA_OCULTA" and v.consume_error_budget,
      "falso 200 (failed_as_2xx) → falla técnica oculta, CONSUME budget")
check(j.root.attrs.get("http.response.status_code") == "200" and j.root.status_code == "ERROR",
      "falso 200: HTTP 200 con estado normalizado ERROR")

j = journey("declined_as_5xx")
v = agent.classify(j)
check(v.clasificacion == "RECHAZO_NEGOCIO" and not v.consume_error_budget,
      "falso 500 (declined_as_5xx) → rechazo de negocio, NO consume budget")
check("RIESGO_ALTO" in v.analisis_causal, "falso 500: motivo RIESGO_ALTO citado")

j = journey("exception_core")
v = agent.classify(j)
check(v.clasificacion == "FALLA_TECNICA" and v.consume_error_budget and "AS400" in v.recomendacion,
      "excepción del core → falla técnica, CONSUME budget, dependencia AS400")
check(j.services == ["payments-qr", "fraud-api"], "journey: payments-qr → fraud-api en orden cronológico")

# Dependencia externa: pago de servicio público con la entidad de recaudo caída (traza real de EKS)
j = journey("dependency_biller_500")
v = agent.classify(j)
check(v.clasificacion == "FALLA_TECNICA" and v.consume_error_budget and v.componente_culpable.startswith("convenios-api"),
      "entidad de recaudo con HTTP 500 → falla técnica de dependencia, CONSUME, culpable convenios-api")
check("no corresponde rollback" in v.recomendacion and j.root.attrs.get("business.transaction_id", "") in v.recomendacion,
      "recomendación: escalar a la entidad con el código de transacción, sin rollback")
check(j.services == ["payments-qr", "fraud-api", "convenios-api"], "journey: payments-qr → fraud-api → convenios-api")
ctx_dep = json.dumps(agent.llm_context(j), ensure_ascii=False)
check('"monto": "220.000 COP"' in ctx_dep and "customer.document" not in ctx_dep and agent.pii_findings(ctx_dep) == [],
      "contexto al modelo: monto formateado, sin seudónimo del cliente y sin PII")
check(len(j.root.attrs.get("customer.document", "")) == 64, "documento del cliente seudonimizado en el borde (SHA-256)")

# Latencia: misma traza exitosa, raíz por encima del umbral
slow = copy.deepcopy(journey("declined_as_5xx"))
slow.root.attrs = {k: v for k, v in slow.root.attrs.items() if not k.startswith("business.")}
slow.root.end_ns = slow.root.start_ns + 1_200 * 1_000_000
check(agent.classify(slow).clasificacion == "LATENCIA", "raíz > 800 ms → consume budget de latencia")

# Guardrails
ctx = json.dumps(agent.llm_context(journey("exception_core")), ensure_ascii=False)
check("client.address" not in ctx and "network.peer.address" not in ctx and "stacktrace" not in ctx,
      "minimización: sin IPs ni stack traces hacia el modelo")
check(agent.pii_findings(ctx) == [], "contexto sanitizado: sin PAN, email ni IDs numéricos")
check(set(agent.pii_findings("tarjeta 4111 1111 1111 1111 email a@b.co cc 1032456789")) >= {"PAN", "EMAIL", "ID_NUMERICO"},
      "fail-closed: PAN, email e ID numérico detectados antes de enviar al modelo")


async def write_tool_rejected() -> bool:
    mcp = agent.TempoMCP("http://unused")
    try:
        await mcp.call("delete-traces", {})
    except PermissionError:
        return True
    return False

check(asyncio.run(write_tool_rejected()), "allowlist: herramienta fuera de solo lectura rechazada")
check(agent._parse_json('<think>razonamiento</think>{"clasificacion": "LATENCIA"}') == {"clasificacion": "LATENCIA"},
      "salida de R1: se descarta <think> y se extrae el JSON")

for ok, name in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
failed = sum(1 for ok, _ in results if not ok)
print(f"RESULT: {'PASS' if not failed else 'FAIL'} ({len(results) - failed}/{len(results)})")
sys.exit(1 if failed else 0)
