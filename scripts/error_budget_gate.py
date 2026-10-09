#!/usr/bin/env python3
"""Error Budget Quality Gate.

  pre-deploy  EB-001 disponibilidad · EB-002 latencia
              Budget agotado o fast burn → BLOCK · slow burn → WARN · sin datos → BLOCK
  canary      CANARY-001 burn rate de la nueva service.version durante la ventana de análisis
              2 evaluaciones consecutivas sobre umbral → FAIL (dispara rollback)

Exit codes: 0 = PROCEED · 1 = BLOCK/FAIL
Override auditado (solo pre-deploy): BUDGET_OVERRIDE_REASON="INC-1234 hotfix P0"
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

PROM = os.getenv("PROM_URL", "http://localhost:9090")
METRIC = "traces_span_metrics_calls_total"
BUCKET = "traces_span_metrics_duration_milliseconds_bucket"
COUNT = "traces_span_metrics_duration_milliseconds_count"

PROCEED, BLOCK, WARN, FAIL, NODATA = "PROCEED", "BLOCK", "WARN", "FAIL", "NO_DATA"
summary_rows = []


def log(gate, status, msg):
    print(f"[{status:<7}] {gate:<10} {msg}", flush=True)
    summary_rows.append((gate, status, msg))


def prom_scalar(query):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def selector(slo, *extra):
    labels = [f'{k}="{v}"' for k, v in slo["selector"].items()] + [e for e in extra if e]
    return "{" + ",".join(labels) + "}"


def requests(slo, window, extra=""):
    return prom_scalar(f"sum(increase({METRIC}{selector(slo, extra)}[{window}]))") or 0.0


def bad_ratio(slo, sli, window, extra=""):
    """Fracción de eventos malos en la ventana. None si no hay tráfico."""
    if sli == "availability":
        errors = 'status_code="STATUS_CODE_ERROR"'
        bad = f"(sum(rate({METRIC}{selector(slo, extra, errors)}[{window}])) or vector(0))"
        total = f"sum(rate({METRIC}{selector(slo, extra)}[{window}]))"
        return prom_scalar(f"{bad} / {total}")
    threshold = slo["slis"]["latency"]["threshold_ms"]
    # Prometheus 3 normaliza le a float ("800.0"); el exporter publica "800"
    le = f'le=~"{threshold}|{float(threshold)}"'
    good = f"sum(rate({BUCKET}{selector(slo, extra, le)}[{window}]))"
    total = f"sum(rate({COUNT}{selector(slo, extra)}[{window}]))"
    return prom_scalar(f"1 - ({good} / {total})")


def fmt(burn):
    return "n/a" if burn is None else f"{burn:.2f}"


def breached(long, short, threshold):
    return long is not None and short is not None and long > threshold and short > threshold


def budget(slo, sli):
    return 1 - slo["slis"][sli]["objective"] / 100


def burn_rate(slo, sli, window, extra=""):
    ratio = bad_ratio(slo, sli, window, extra)
    return None if ratio is None else ratio / budget(slo, sli)


def evaluate_sli(slo, profile, sli, gate_id):
    policy = slo["policy"]
    window = profile["budget_window"]
    volume = requests(slo, profile["fast_burn"]["long"])
    if volume < policy["min_requests"]:
        log(gate_id, NODATA, f"{sli}: {volume:,.0f} requests en {profile['fast_burn']['long']} "
                             f"(mínimo {policy['min_requests']}) · policy={policy['no_data']}")
        return BLOCK if policy["no_data"] == "block" else WARN

    consumed = (bad_ratio(slo, sli, window) or 0.0) / budget(slo, sli)
    remaining = 100 * (1 - consumed)
    budget_volume = requests(slo, window)
    budget_significant = budget_volume >= policy.get("min_budget_requests", 0)
    fast = profile["fast_burn"]
    slow = profile["slow_burn"]
    fast_long, fast_short = burn_rate(slo, sli, fast["long"]), burn_rate(slo, sli, fast["short"])
    slow_long, slow_short = burn_rate(slo, sli, slow["long"]), burn_rate(slo, sli, slow["short"])

    if not budget_significant:
        budget_state = f"budget=n/s ({budget_volume:,.0f} < {policy['min_budget_requests']:,} requests)"
    elif remaining > 0:
        budget_state = f"budget_remaining={remaining:.1f}%"
    else:
        budget_state = f"budget_remaining=0% (consumido {consumed:.1f}x)"
    detail = (f"objective={slo['slis'][sli]['objective']}% {budget_state} ({window}) "
              f"burn[{fast['long']}/{fast['short']}]={fmt(fast_long)}/{fmt(fast_short)} "
              f"burn[{slow['long']}/{slow['short']}]={fmt(slow_long)}/{fmt(slow_short)}")

    if budget_significant and remaining <= 0 and policy["budget_exhausted"] == "block":
        log(gate_id, BLOCK, f"{sli}: Error Budget agotado · {detail}")
        return BLOCK
    if breached(fast_long, fast_short, fast["threshold"]) and policy["fast_burn"] == "block":
        log(gate_id, BLOCK, f"{sli}: fast burn > {fast['threshold']}x · {detail}")
        return BLOCK
    if breached(slow_long, slow_short, slow["threshold"]):
        status = BLOCK if policy["slow_burn"] == "block" else WARN
        log(gate_id, status, f"{sli}: slow burn > {slow['threshold']}x · {detail}")
        return status
    log(gate_id, PROCEED, f"{sli}: {detail}")
    return PROCEED


def pre_deploy(slo, profile):
    results = [evaluate_sli(slo, profile, "availability", "EB-001"),
               evaluate_sli(slo, profile, "latency", "EB-002")]
    if BLOCK not in results:
        return PROCEED
    reason = os.getenv("BUDGET_OVERRIDE_REASON", "").strip()
    if reason:
        actor = os.getenv("GITHUB_ACTOR", os.getenv("USER", "unknown"))
        log("EB-OVR", WARN, f"override aplicado por {actor} · motivo='{reason}' · registrar en post-mortem")
        return PROCEED
    return BLOCK


def canary(slo, profile, version):
    cfg = profile["canary"]
    extra = f'service_version="{version}"'
    deadline = time.monotonic() + cfg["duration_s"]
    consecutive, evaluated = 0, 0
    log("CANARY-001", "START", f"version={version} duration={cfg['duration_s']}s window={cfg['window']} "
                               f"threshold={cfg['threshold']}x")
    while time.monotonic() < deadline:
        time.sleep(cfg["interval_s"])
        volume = requests(slo, cfg["window"], extra)
        if volume < cfg["min_requests"]:
            print(f"[{'WAIT':<7}] CANARY-001 version={version} requests={volume:,.0f} < {cfg['min_requests']}", flush=True)
            continue
        evaluated += 1
        burn = burn_rate(slo, "availability", cfg["window"], extra)
        lat_burn = burn_rate(slo, "latency", cfg["window"], extra)
        over = burn is not None and burn > cfg["threshold"]
        consecutive = consecutive + 1 if over else 0
        print(f"[{'CHECK':<7}] CANARY-001 version={version} requests={volume:,.0f} "
              f"burn_availability={fmt(burn)}x burn_latency={fmt(lat_burn)}x consecutive_breaches={consecutive}", flush=True)
        if consecutive >= 2:
            log("CANARY-001", FAIL, f"version={version} burn={fmt(burn)}x > {cfg['threshold']}x en 2 evaluaciones · rollback requerido")
            return FAIL
    if evaluated == 0:
        log("CANARY-001", FAIL, f"version={version} sin telemetría durante el análisis · fail-closed")
        return FAIL
    log("CANARY-001", PROCEED, f"version={version} estable tras {evaluated} evaluaciones")
    return PROCEED


def write_github_summary(mode, decision, service):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(f"### Error Budget Gate · {service} · {mode} → **{decision}**\n\n| Gate | Estado | Detalle |\n|---|---|---|\n")
            for gate, status, msg in summary_rows:
                f.write(f"| {gate} | {status} | {msg} |\n")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"decision={decision}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["pre-deploy", "canary"])
    parser.add_argument("--slo", default="slo/payments-qr.yaml")
    parser.add_argument("--profile", default=os.getenv("SLO_PROFILE", "prod"))
    parser.add_argument("--version", help="service.version bajo análisis (canary)")
    args = parser.parse_args()

    with open(args.slo) as f:
        slo = yaml.safe_load(f)
    profile = slo["profiles"][args.profile]
    print(f"error-budget-gate · mode={args.mode} service={slo['service']} profile={args.profile} "
          f"· {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}", flush=True)

    if args.mode == "pre-deploy":
        decision = pre_deploy(slo, profile)
    else:
        if not args.version:
            parser.error("--version es obligatorio en modo canary")
        decision = canary(slo, profile, args.version)

    print(f"DECISION: {decision}")
    write_github_summary(args.mode, decision, slo["service"])
    sys.exit(0 if decision == PROCEED else 1)


if __name__ == "__main__":
    main()
