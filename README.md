# sre-finops-otel-collector

PoC local de observabilidad para **Banco Plus**: un OpenTelemetry Collector en modo **Gateway** que
destruye la PII en el borde, aplica **Tail-Based Sampling** y hace **Dual-Shipping** hacia el APM
legacy y el SaaS nuevo, sin tocar el código de las aplicaciones.

Corre de forma nativa en Apple Silicon (arm64): todas las imágenes son multi-arch.

## Arquitectura

```
traffic-generator ──OTLP──► otel-gateway
 (api-gateway →              │
  payments-qr →              ├─ traces/ingest : memory_limiter → transform/pii
  core-banking-as400)        │        ├──► span_metrics ─► Prometheus  (RED sobre el 100% → SLO / Quality Gate)
                             │        └──► forward
                             └─ traces/export : tail_sampling → batch
                                      ├──► jaeger-legacy  (APM legacy)  :16686
                                      └──► jaeger-saas    (SaaS nuevo)  :16687
```

## Uso

```bash
make up          # levanta todo
make verify      # evidencia: PII, reducción de ingesta y SLI (espera ~1 min de tráfico)
make incident    # simula un mal despliegue (30% de errores)
make validate    # valida la config del Collector con el binario real
make down
```

| UI | URL |
|---|---|
| Jaeger "APM legacy" | http://localhost:16686 |
| Jaeger "SaaS nuevo" | http://localhost:16687 |
| Prometheus | http://localhost:9090 |

## Qué demuestra

### 1. Privacidad regulatoria (Security by Design)
`transform/pii` corre antes de cualquier exporter, así que la PII nunca sale de la red del banco.

| Capa | Técnica | Ejemplo |
|---|---|---|
| A. Llaves conocidas | SHA-256 con sal | `account.number` → hash (se conserva la correlación) |
| B. Llaves prohibidas | Eliminación | `card.cvv`, `*.pin`, `*.token` |
| C. Barrido Regex | Sobre todos los valores string, eventos y nombres de span | `4111…1111` → `****-****-****-1111` |

### 2. FinOps: muestreo post-análisis
El Gateway retiene cada traza en memoria (`decision_wait: 10s`) y solo después decide:

| Política | Retención |
|---|---|
| Errores (`status=ERROR`) y HTTP 5xx | 100% |
| Lentas (> 800 ms, umbral del SLO) | 100% |
| Alto valor (≥ 50 M COP) y `payment.anomaly=true` | 100% |
| Exitosas repetitivas | 5% |

Los rechazos de negocio (HTTP 422, fondos insuficientes) **no** son errores técnicos: no consumen
Error Budget y se muestrean como tráfico normal.

> El generador usa a propósito una tasa de errores alta (≈13% de anomalías) para que la demo sea
> visible. Con una tasa productiva del 1%, la reducción de ingesta de trazas supera el 90%.

### 3. SLIs exactos pese al muestreo
`span_metrics` recibe el 100% de los spans **antes** de `tail_sampling`. El Error Budget y el
Quality Gate se calculan sobre datos completos.

### 4. Trazabilidad hasta el core (AS400)
El span `MQPUT PAGOS.QR.REQ` propaga el header W3C `traceparent` en las propiedades del mensaje MQ.

## Versiones

| Componente | Versión |
|---|---|
| otelcol-contrib | 0.162.0 |
| Jaeger | 2.22.0 (API v3) |
| Prometheus | v3.13.4 |
| OpenTelemetry Python SDK | 1.45.1 |
