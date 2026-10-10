#!/usr/bin/env python3
"""
SRE-RCA-001 · Agente de Análisis de Causa Raíz sobre MCP (Tempo, solo lectura)

Reconstruye el Customer Journey de una transacción desde Tempo vía Model Context Protocol, clasifica
la falla (técnica vs. rechazo de negocio), calcula su impacto en el Error Budget y emite un informe RCA
con la acción sugerida para el Incident Commander.

Motores de análisis:
  llm    Modelo de razonamiento compatible con la API de OpenAI (DeepSeek-R1, Ollama, endpoint privado).
  rules  Motor determinista: mismas reglas de negocio, sin LLM. Siempre se ejecuta como guardrail:
         si el modelo contradice el impacto en el Error Budget, prevalece la regla y el informe lo marca.

Guardrails:
  - Solo herramientas de lectura del servidor MCP de Tempo (allowlist). Cero escrituras o remediación.
  - Minimización: al modelo solo llegan atributos de negocio y del journey, nunca IPs ni payloads.
  - Fail-closed: si el contexto contiene PAN, email o identificadores numéricos, no se envía al modelo.

Uso:
  scripts/sre_rca_agent.py                         # falla más reciente de los últimos 60 min
  scripts/sre_rca_agent.py --trace-id <id>
  scripts/sre_rca_agent.py --last-incident --window 180
  scripts/sre_rca_agent.py --client-hash <sha256> [--client-attr account.number]
  scripts/sre_rca_agent.py --query '{ span.business.status_reclassified = "declined_as_5xx" }' --window 240

Entorno:
  TEMPO_MCP_URL     http://localhost:3200/api/mcp (port-forward a svc/tempo)
  DEEPSEEK_API_KEY  activa el motor llm contra https://api.deepseek.com (modelo deepseek-reasoner)
  LLM_BASE_URL      endpoint compatible con OpenAI, p. ej. http://localhost:11434/v1 (Ollama)
  LLM_MODEL         modelo del endpoint, p. ej. deepseek-r1:7b
  LLM_API_KEY       credencial del endpoint (Ollama acepta cualquier valor)

Exit codes: 0 = informe emitido · 1 = sin trazas que analizar · 2 = error de conexión o configuración
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field

# Herramientas de Tempo autorizadas: todas de lectura. Cualquier otra se rechaza aunque el servidor la exponga.
READ_ONLY_TOOLS = {
    "docs-traceql", "get-attribute-names", "get-attribute-values", "get-trace",
    "traceql-metrics-instant", "traceql-metrics-range", "traceql-search",
}

# SLO de payments-qr (slo.yaml del servicio): disponibilidad 99,95 % y latencia 99 % < 800 ms
AVAILABILITY_SLO = 99.95
LATENCY_THRESHOLD_MS = 800

# Patrones del Gateway (transform/pii): si aparecen en el contexto, el envío al modelo se bloquea
PII_PATTERNS = {
    "PAN": re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
    "PAN_AMEX": re.compile(r"\b3[47]\d{2}[ -]?\d{6}[ -]?\d{5}\b"),
    "EMAIL": re.compile(r"[A-Za-z0-9._%+-]+(@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ID_NUMERICO": re.compile(r"\b\d{6,12}\b"),
}

# Atributos que llegan al modelo (minimización de datos)
SPAN_ATTRS_FOR_LLM = (
    "http.route", "http.request.method", "http.response.status_code", "url.path", "url.query", "server.address",
    "business.operation", "business.outcome", "business.reason", "business.status_reclassified",
    "business.amount_band", "payment.channel",
)

SYSTEM_PROMPT = """Eres el agente de RCA de la Plataforma de Confiabilidad de Banco Plus. Analizas una transacción
de pago QR a partir de trazas OpenTelemetry ya sanitizadas en el Gateway (sin PAN, emails ni cédulas).

Reglas de interpretación (obligatorias):
- El código HTTP NO es la fuente de verdad. Manda business.outcome, normalizado en el Gateway.
- business.status_reclassified = "declined_as_5xx": HTTP 500 con business.outcome=declined. Es un rechazo de negocio
  legítimo (p. ej. RIESGO_ALTO por prevención de fraude). NO es falla de infraestructura y NO consume Error Budget.
- business.status_reclassified = "failed_as_2xx": HTTP 200 con business.outcome=failed (p. ej. ERROR_SISTEMA). Es una
  falla técnica oculta: el cliente no pudo pagar. CONSUME Error Budget y degrada el SLO de disponibilidad.
- Span de servidor con estado ERROR y evento de excepción sin business.outcome: falla técnica no controlada. CONSUME.
- Duración del span raíz > 800 ms: consume el budget del SLO de latencia aunque el pago termine bien.
- Identifica el componente culpable con la evidencia de los spans (servicio, span, latencia, excepción).
- Cita la evidencia (span y atributo) de cada conclusión. Si un dato no está en la traza, dilo; no lo inventes.
- Solo lectura: puedes consultar herramientas de Tempo, nunca proponer cambios ejecutados por ti mismo.

Responde SOLO con un objeto JSON con estas claves:
{"clasificacion": "FALLA_TECNICA_OCULTA|FALLA_TECNICA|RECHAZO_NEGOCIO|LATENCIA|SIN_FALLA",
 "consume_error_budget": true|false,
 "componente_culpable": "servicio / span",
 "analisis_causal": "explicación breve con evidencia citada",
 "recomendacion": "acción concreta para el Incident Commander"}"""


# --------------------------------------------------------------------------------------------- modelo de datos
@dataclass
class Span:
    service: str
    name: str
    kind: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int
    status_code: str
    status_message: str
    attrs: dict
    events: list
    resource: dict

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6


@dataclass
class Journey:
    trace_id: str
    spans: list[Span]
    root: Span

    @property
    def services(self) -> list[str]:
        seen: list[str] = []
        for s in self.spans:
            if s.service not in seen:
                seen.append(s.service)
        return seen


@dataclass
class Verdict:
    clasificacion: str
    consume_error_budget: bool
    componente_culpable: str
    analisis_causal: str
    recomendacion: str
    motor: str
    evidencia: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------- parsing OTLP
def _value(v: dict):
    if not v:
        return None
    return next(iter(v.values()))


def _kv(items) -> dict:
    return {a["key"]: _value(a.get("value", {})) for a in items or []}


def parse_trace(trace_id: str, payload: dict) -> Journey:
    spans: list[Span] = []
    for rs in payload.get("trace", payload).get("resourceSpans", []):
        resource = _kv(rs.get("resource", {}).get("attributes"))
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                status = sp.get("status") or {}
                spans.append(Span(
                    service=resource.get("service.name", "desconocido"),
                    name=sp.get("name", ""),
                    kind=sp.get("kind", "").replace("SPAN_KIND_", ""),
                    span_id=sp.get("spanId", ""),
                    parent_id=sp.get("parentSpanId"),
                    start_ns=int(sp.get("startTimeUnixNano", 0)),
                    end_ns=int(sp.get("endTimeUnixNano", 0)),
                    status_code=status.get("code", "STATUS_CODE_UNSET").replace("STATUS_CODE_", ""),
                    status_message=status.get("message", ""),
                    attrs=_kv(sp.get("attributes")),
                    events=[{"name": e.get("name"), **_kv(e.get("attributes"))} for e in sp.get("events", [])],
                    resource=resource,
                ))
    if not spans:
        raise ValueError(f"la traza {trace_id} no contiene spans")
    spans.sort(key=lambda s: s.start_ns)
    ids = {s.span_id for s in spans}
    roots = [s for s in spans if not s.parent_id or s.parent_id not in ids]
    return Journey(trace_id=trace_id, spans=spans, root=roots[0])


# --------------------------------------------------------------------------------------------- motor determinista
def _culprit(j: Journey) -> Span:
    """Span más profundo con error o excepción; si no hay, el más lento por debajo del raíz."""
    with_error = [s for s in j.spans if s.status_code == "ERROR" or any(e["name"] == "exception" for e in s.events)]
    if with_error:
        return with_error[-1]
    children = [s for s in j.spans if s is not j.root] or [j.root]
    return max(children, key=lambda s: s.duration_ms)


def classify(j: Journey) -> Verdict:
    root = j.root
    a = root.attrs
    outcome, reason = a.get("business.outcome"), a.get("business.reason")
    reclassified = a.get("business.status_reclassified")
    http = a.get("http.response.status_code")
    tribe = root.resource.get("team.tribe", "tribu dueña del servicio")
    version = root.resource.get("service.version", "?")[:12]
    exception = next((e for s in j.spans for e in s.events if e["name"] == "exception"), None)
    culprit = _culprit(j)
    evidence = [f"{root.service} · {root.name} · HTTP {http} · {root.duration_ms:.0f} ms · status {root.status_code}"]
    if outcome:
        evidence.append(f"business.outcome={outcome} · business.reason={reason} · reclasificación={reclassified or 'ninguna'}")

    if reclassified == "declined_as_5xx" or outcome == "declined":
        return Verdict(
            "RECHAZO_NEGOCIO", False, f"{root.service} / regla de negocio ({reason})",
            f"Rechazo de negocio legítimo ({reason}). El backend respondió HTTP {http}, pero business.outcome=declined: "
            "no es una falla de infraestructura y no consume Error Budget. El Gateway reclasificó el estado del span a UNSET.",
            "No escalar como incidente técnico. Si el volumen de rechazos por riesgo cambia de forma anómala, revisar con "
            "Riesgo/Fraude el umbral del scoring. Deuda de contrato: devolver el rechazo como respuesta de negocio "
            "(p. ej. 200/422 con X-Business-Outcome=declined), no como HTTP 500.",
            "rules", evidence)

    if reclassified == "failed_as_2xx" or outcome == "failed":
        return Verdict(
            "FALLA_TECNICA_OCULTA", True, f"{root.service} / {root.name}",
            f"Falla técnica oculta: el backend respondió HTTP {http} pero business.outcome=failed ({reason}). El cliente "
            "no pudo pagar. Consume Error Budget de disponibilidad; un monitor basado en códigos HTTP no la habría visto.",
            f"Alertar a {tribe}: falla de sistema en {root.service} (versión {version}). Revisar el burn rate del SLO; si "
            "coincide con un despliegue reciente, evaluar rollback. Corregir el contrato: una falla no debe responder 200.",
            "rules", evidence)

    if root.status_code == "ERROR" or exception:
        message = (exception or {}).get("exception.message", root.status_message or "sin mensaje")
        etype = (exception or {}).get("exception.type", "error")
        evidence.append(f"excepción en {culprit.service} / {culprit.name}: {etype}: {message}")
        dependency = "core AS400" if "AS400" in str(message) else culprit.service
        return Verdict(
            "FALLA_TECNICA", True, f"{culprit.service} / {culprit.name}",
            f"Falla técnica no controlada en {culprit.service}: {etype} ({message}). El pago no se completó y consume "
            "Error Budget de disponibilidad.",
            f"Alertar a {tribe}. Dependencia implicada: {dependency}. Verificar timeouts y circuit breaker hacia esa "
            "dependencia y el burn rate actual; si la tasa supera 14,4x, el Quality Gate ya bloquea despliegues.",
            "rules", evidence)

    if root.duration_ms > LATENCY_THRESHOLD_MS:
        return Verdict(
            "LATENCIA", True, f"{culprit.service} / {culprit.name}",
            f"Pago completado en {root.duration_ms:.0f} ms, por encima del umbral de {LATENCY_THRESHOLD_MS} ms. Consume "
            f"Error Budget del SLO de latencia. Tramo más lento: {culprit.service} ({culprit.duration_ms:.0f} ms).",
            f"Revisar saturación de {culprit.service} (CPU, pool de conexiones) y escalar réplicas si la latencia es "
            "sostenida; validar con el burn rate de latencia antes de actuar.",
            "rules", evidence)

    return Verdict("SIN_FALLA", False, "—", "La transacción terminó dentro del SLO.", "Sin acción.", "rules", evidence)


# --------------------------------------------------------------------------------------------- cliente MCP
class TempoMCP:
    """Sesión MCP contra Tempo restringida a herramientas de lectura."""

    def __init__(self, url: str):
        self.url = url
        self.tools: list = []

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(streamable_http_client(self.url))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            init = await self.session.initialize()
            listed = await self.session.list_tools()
        except BaseException:
            await self._stack.aclose()
            raise
        self.tools = [t for t in listed.tools if t.name in READ_ONLY_TOOLS]
        rejected = sorted({t.name for t in listed.tools} - READ_ONLY_TOOLS)
        self.server = f"{init.server_info.name} {init.server_info.version} · MCP {init.protocol_version}"
        if rejected:
            log(f"herramientas no autorizadas ignoradas: {', '.join(rejected)}")
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def call(self, name: str, arguments: dict) -> str:
        if name not in READ_ONLY_TOOLS:
            raise PermissionError(f"herramienta '{name}' fuera de la allowlist de solo lectura")
        result = await self.session.call_tool(name, arguments)
        text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if getattr(result, "is_error", False):
            raise RuntimeError(f"{name}: {text[:300]}")
        return text

    async def search(self, query: str, minutes: int) -> list[dict]:
        end = dt.datetime.now(dt.UTC)
        start = end - dt.timedelta(minutes=minutes)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        raw = await self.call("traceql-search", {"query": query, "start": start.strftime(fmt), "end": end.strftime(fmt)})
        traces = json.loads(raw).get("traces", [])
        return sorted(traces, key=lambda t: int(t.get("startTimeUnixNano", 0)), reverse=True)

    async def search_recent(self, query: str, minutes: int) -> tuple[list[dict], int]:
        """La búsqueda de Tempo devuelve un máximo de resultados sin orden garantizado: se amplía la ventana
        de forma progresiva para que el primer resultado sea el incidente más reciente."""
        for window in sorted({w for w in (5, 15, minutes) if w <= minutes}):
            traces = await self.search(query, window)
            if traces:
                return traces, window
        return [], minutes

    async def journey(self, trace_id: str) -> Journey:
        return parse_trace(trace_id, json.loads(await self.call("get-trace", {"trace_id": trace_id})))

    def openai_tools(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "")[:1000],
                "parameters": t.input_schema or {"type": "object", "properties": {}},
            },
        } for t in self.tools]


# --------------------------------------------------------------------------------------------- motor LLM
def llm_config() -> dict | None:
    if os.environ.get("LLM_BASE_URL"):
        return {"base_url": os.environ["LLM_BASE_URL"], "api_key": os.environ.get("LLM_API_KEY", "local"),
                "model": os.environ.get("LLM_MODEL", "deepseek-r1:7b")}
    if os.environ.get("DEEPSEEK_API_KEY"):
        return {"base_url": "https://api.deepseek.com", "api_key": os.environ["DEEPSEEK_API_KEY"],
                "model": os.environ.get("LLM_MODEL", "deepseek-reasoner")}
    return None


def llm_context(j: Journey) -> dict:
    """Contexto minimizado: offsets relativos, atributos de negocio y excepciones ya enmascaradas."""
    t0 = j.root.start_ns
    return {
        "trace_id": j.trace_id,
        "tribu": j.root.resource.get("team.tribe"),
        "version": j.root.resource.get("service.version"),
        "slo": {"disponibilidad_pct": AVAILABILITY_SLO, "latencia_umbral_ms": LATENCY_THRESHOLD_MS},
        "spans": [{
            "servicio": s.service, "span": s.name, "tipo": s.kind,
            "inicio_ms": round((s.start_ns - t0) / 1e6, 1), "duracion_ms": round(s.duration_ms, 1),
            "estado": s.status_code, "mensaje_estado": s.status_message,
            "atributos": {k: v for k, v in s.attrs.items() if k in SPAN_ATTRS_FOR_LLM},
            "excepciones": [{k: v for k, v in e.items() if k in ("exception.type", "exception.message")}
                            for e in s.events if e["name"] == "exception"],
        } for s in j.spans],
    }


def pii_findings(text: str) -> list[str]:
    # trace_id y versiones hexadecimales no coinciden: los patrones exigen límites de palabra numéricos
    return [name for name, rx in PII_PATTERNS.items() if rx.search(text)]


def _parse_json(content: str) -> dict:
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        raise ValueError("el modelo no devolvió JSON")
    return json.loads(match.group(0))


async def llm_verdict(cfg: dict, mcp: TempoMCP, j: Journey, mode: str, max_steps: int, show_reasoning: bool) -> Verdict:
    from openai import OpenAI

    context = json.dumps(llm_context(j), ensure_ascii=False)
    leaked = pii_findings(context)
    if leaked:
        raise PermissionError(f"contexto con posibles datos sensibles ({', '.join(leaked)}): envío al modelo bloqueado")

    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Transacción a analizar (journey reconstruido desde Tempo vía MCP):\n{context}"},
    ]
    tools = mcp.openai_tools() if mode == "native" else None

    for step in range(max_steps):
        kwargs = {"model": cfg["model"], "messages": messages}
        if tools:
            kwargs["tools"] = tools
        response = client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        reasoning = getattr(msg, "reasoning_content", None)
        if show_reasoning and reasoning:
            log(f"razonamiento del modelo (paso {step + 1}):\n{reasoning.strip()}")
        if not msg.tool_calls:
            data = _parse_json(msg.content)
            return Verdict(
                clasificacion=str(data.get("clasificacion", "")).upper(),
                consume_error_budget=bool(data.get("consume_error_budget")),
                componente_culpable=str(data.get("componente_culpable", "")),
                analisis_causal=str(data.get("analisis_causal", "")),
                recomendacion=str(data.get("recomendacion", "")),
                motor=f"llm · {cfg['model']}",
            )
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                log(f"MCP · {tc.function.name}({json.dumps(args, ensure_ascii=False)[:160]})")
                result = await mcp.call(tc.function.name, args)
                if pii_findings(result):
                    result = "[resultado bloqueado por el guardrail de privacidad]"
            except Exception as exc:  # el error vuelve al modelo como resultado de la herramienta
                result = f"error: {exc}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:12000]})
    raise RuntimeError(f"el modelo no concluyó en {max_steps} pasos")


# --------------------------------------------------------------------------------------------- informe
def log(message: str) -> None:
    print(f"[SRE-RCA-001] {message}", file=sys.stderr)


def render(j: Journey, v: Verdict, rules: Verdict, server: str, discrepancy: str | None) -> str:
    a = j.root.attrs
    t0 = j.root.start_ns
    width = 34
    total = max(j.root.duration_ms, 0.001)
    lines = [
        "=" * 96,
        f" RCA REPORT · trace {j.trace_id} · {dt.datetime.fromtimestamp(t0 / 1e9, dt.UTC):%Y-%m-%d %H:%M:%S} UTC",
        f" Fuente: Tempo vía MCP ({server}) · Motor: {v.motor}",
        "=" * 96,
        "",
        "[1. Resumen Ejecutivo]",
        f"  Operación        {a.get('business.operation', 'no disponible en la traza')}",
        f"  Resultado        {a.get('business.outcome', 'sin business.outcome')} · motivo {a.get('business.reason', '—')}",
        f"  Respuesta HTTP   {a.get('http.response.status_code', '—')} · estado normalizado {j.root.status_code}",
        f"  Canal / monto    {a.get('payment.channel', 'no disponible en la traza')} / "
        f"{a.get('business.amount_band', 'no disponible en la traza')}",
        f"  Duración total   {j.root.duration_ms:.1f} ms · servicios {' → '.join(j.services)}",
        f"  Tribu · versión  {j.root.resource.get('team.tribe', '—')} · {j.root.resource.get('service.version', '—')[:12]}",
        "",
        "[2. Reconstrucción del Customer Journey]",
    ]
    depth = {j.root.span_id: 0}
    for s in j.spans:
        d = depth.get(s.parent_id, -1) + 1 if s is not j.root else 0
        depth[s.span_id] = d
        offset = (s.start_ns - t0) / 1e6
        bar_start = int(offset / total * width)
        bar_len = max(1, int(s.duration_ms / total * width))
        bar = " " * bar_start + "█" * min(bar_len, width - bar_start)
        flag = " ✖" if s.status_code == "ERROR" else ""
        lines.append(f"  +{offset:6.1f} ms  {'  ' * d}{s.service} · {s.kind} {s.name}".ljust(60)
                     + f"{s.duration_ms:7.1f} ms |{bar.ljust(width)}|{flag}")
        for e in s.events:
            if e["name"] == "exception":
                lines.append(f"  {'':10}  {'  ' * d}  └ excepción {e.get('exception.type', '')}: "
                             f"{str(e.get('exception.message', ''))[:90]}")
    lines += [
        "",
        "[3. Análisis Causal y Clasificación]",
        f"  Clasificación     {v.clasificacion}",
        f"  Error Budget      {'CONSUME' if v.consume_error_budget else 'NO CONSUME'}"
        + (f" · 1 evento malo del SLI (el SLO de {AVAILABILITY_SLO} % tolera 5 por cada 10.000 pagos al mes)"
           if v.consume_error_budget else ""),
        f"  Componente        {v.componente_culpable}",
        f"  Análisis          {v.analisis_causal}",
        "  Evidencia",
        *[f"    - {e}" for e in rules.evidencia],
    ]
    if discrepancy:
        lines += ["", f"  ⚠ GUARDRAIL: {discrepancy}"]
    lines += [
        "",
        "[4. Recomendación Operativa (Runbook as Code)]",
        f"  {v.recomendacion}",
        "  Ejecución: decisión humana del Incident Commander. Este agente no ejecuta acciones.",
        "=" * 96,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- orquestación
def reachable(url: str) -> bool:
    import httpx

    # Solo un fallo de conexión cuenta: el GET puede abrir un stream SSE que no responde (ReadTimeout = alcanzable)
    try:
        httpx.get(url, timeout=3)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    except httpx.HTTPError:
        pass
    return True


async def run(args) -> int:
    url = args.mcp_url
    if not reachable(url):
        log(f"servidor MCP no alcanzable en {url}")
        log("port-forward: kubectl -n observability port-forward svc/tempo 3200:3200")
        return 2
    try:
        async with TempoMCP(url) as mcp:
            log(f"conectado a {mcp.server} · herramientas de lectura: {len(mcp.tools)}")
            if args.trace_id:
                trace_id = args.trace_id
            else:
                if args.query:
                    query = args.query
                elif args.client_hash:
                    query = f'{{ span.{args.client_attr} = "{args.client_hash}" }}'
                else:
                    query = '{ span.business.outcome = "failed" || status = error }'
                traces, window = await mcp.search_recent(query, args.window)
                log(f"traceql-search {query} · últimos {window} min (ventana máxima {args.window}) · {len(traces)} trazas")
                if not traces:
                    print(f"Sin trazas que analizar en los últimos {args.window} min.")
                    return 1
                trace_id = traces[0]["traceID"]
            journey = await mcp.journey(trace_id)

            rules = classify(journey)
            verdict, discrepancy = rules, None
            cfg = llm_config() if args.engine in ("auto", "llm") else None
            if args.engine == "llm" and not cfg:
                log("motor llm solicitado sin DEEPSEEK_API_KEY ni LLM_BASE_URL")
                return 2
            if cfg:
                try:
                    verdict = await llm_verdict(cfg, mcp, journey, args.tool_mode, args.max_steps, args.show_reasoning)
                    if verdict.consume_error_budget != rules.consume_error_budget:
                        discrepancy = (f"el modelo concluyó {'CONSUME' if verdict.consume_error_budget else 'NO CONSUME'}"
                                       f" Error Budget; la regla de negocio indica lo contrario. Prevalece la regla.")
                        verdict.consume_error_budget = rules.consume_error_budget
                except Exception as exc:
                    log(f"motor llm no disponible ({exc}); se emite el dictamen del motor determinista")
                    verdict = rules
            else:
                log("sin LLM configurado: motor determinista (reglas de negocio de la plataforma, sin modelo)")

            if args.json:
                print(json.dumps({"trace_id": journey.trace_id, "veredicto": verdict.__dict__,
                                  "regla": rules.__dict__, "guardrail": discrepancy}, ensure_ascii=False, indent=2))
            else:
                print(render(journey, verdict, rules, mcp.server, discrepancy))
            return 0
    except BaseException as exc:
        connection = next((e for e in _flatten(exc) if isinstance(e, OSError) or "Connect" in type(e).__name__), None)
        if connection is None:
            raise
        log(f"no se pudo conectar a {url}: {connection}")
        log("port-forward: kubectl -n observability port-forward svc/tempo 3200:3200")
        return 2


def _flatten(exc: BaseException) -> list[BaseException]:
    # El transporte MCP (anyio) envuelve los errores en ExceptionGroup
    if isinstance(exc, BaseExceptionGroup):
        return [e for sub in exc.exceptions for e in _flatten(sub)]
    return [exc]


def main() -> int:
    p = argparse.ArgumentParser(description="Agente de RCA sobre el servidor MCP de Tempo (solo lectura)")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--trace-id", help="TraceID a analizar")
    target.add_argument("--client-hash", help="hash SHA-256 del cliente (atributo hasheado en el Gateway)")
    target.add_argument("--last-incident", action="store_true", help="falla más reciente de la ventana (por defecto)")
    target.add_argument("--query", help="consulta TraceQL propia; se analiza la traza más reciente que coincida")
    p.add_argument("--client-attr", default="account.number", help="atributo hasheado para --client-hash")
    p.add_argument("--window", type=int, default=60, help="ventana de búsqueda en minutos (defecto 60)")
    p.add_argument("--mcp-url", default=os.environ.get("TEMPO_MCP_URL", "http://localhost:3200/api/mcp"))
    p.add_argument("--engine", choices=["auto", "llm", "rules"], default="auto")
    p.add_argument("--tool-mode", choices=["native", "prefetch"], default="prefetch",
                   help="prefetch (defecto): el journey se consulta por MCP y se entrega resuelto al modelo · "
                        "native: el modelo invoca las herramientas MCP con function calling")
    p.add_argument("--max-steps", type=int, default=6)
    p.add_argument("--show-reasoning", action="store_true", help="muestra el razonamiento (reasoning_content) de R1")
    p.add_argument("--json", action="store_true")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
