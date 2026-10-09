#!/usr/bin/env python3
"""PII-REGEX-001 · Prueba de regresión del procesador transform/pii.

Extrae las sentencias OTTL de la configuración del Collector y las aplica, en el mismo orden,
sobre fixtures representativos de lo que emiten los agentes. No requiere infraestructura.

  python3 scripts/test_pii_redaction.py [--config collector/otel-collector-config.yaml]

Exit codes: 0 = PASS · 1 = FAIL
"""

import argparse
import re
import sys

import yaml

REPLACE = re.compile(r'^replace_all_patterns\(attributes, "value", "(?P<regex>(?:[^"\\]|\\.)*)", "(?P<repl>(?:[^"\\]|\\.)*)"\)')
DELETE = re.compile(r'^delete_matching_keys\(attributes, "(?P<regex>(?:[^"\\]|\\.)*)"\)')

PAN = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+(@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCOUNT = re.compile(r"\b\d{8,12}\b")

# (contexto, atributos de entrada, verificaciones exactas esperadas)
FIXTURES = [
    ("span", {"url.query": "account=001234567890&email=cliente1%40correo.com"},
     {"url.query": "account=[NUM_REDACTADO]&email=[EMAIL_REDACTADO]"}),
    ("span", {"url.full": "https://api.bancoplus.co/v1/payments/qr?account=001234567890&email=ana.ruiz%40banco.com.co"}, {}),
    ("span", {"customer.email": "ana.ruiz@banco.com.co"}, {"customer.email": "[EMAIL_REDACTADO]"}),
    ("span", {"card.number": "4111111111111111"}, {"card.number": "****-****-****-1111"}),
    ("span", {"card.number": "4111 1111 1111 1111"}, {"card.number": "****-****-****-1111"}),
    ("span", {"card.number": "378282246310005"}, {"card.number": "****-******-10005"}),
    ("span", {"card.cvv": "123", "auth.token": "abc", "payment.mapping": "v2"}, {"payment.mapping": "v2"}),
    ("spanevent", {"exception.message": "Timeout core AS400 cuenta 001234567890 tarjeta 4111111111111111 cliente x%40y.com"}, {}),
    # Negativos: datos legítimos que no deben alterarse
    ("span", {"http.route": "/v1/payments/qr", "business.reason": "OK", "payment.reference": "50000"},
     {"http.route": "/v1/payments/qr", "business.reason": "OK", "payment.reference": "50000"}),
]
FORBIDDEN_KEYS = {"card.cvv", "auth.token"}


def ottl_string(value):
    """Literal OTTL → string Python (\\\\ → \\, \\" → ")."""
    return re.sub(r'\\(.)', r'\1', value)


def load_statements(path):
    with open(path) as f:
        config = yaml.safe_load(f)
    blocks = config["processors"]["transform/pii"]["trace_statements"]
    rules = {}
    for block in blocks:
        ops = []
        for stmt in block["statements"]:
            if m := REPLACE.match(stmt):
                repl = ottl_string(m["repl"]).replace("$$", "$")
                ops.append(("replace", re.compile(ottl_string(m["regex"])), re.sub(r"\$(\d)", r"\\g<\1>", repl)))
            elif m := DELETE.match(stmt):
                ops.append(("delete", re.compile(ottl_string(m["regex"])), None))
        rules[block["context"]] = ops
    return rules


def apply(ops, attrs):
    out = dict(attrs)
    for kind, regex, repl in ops:
        if kind == "delete":
            out = {k: v for k, v in out.items() if not regex.search(k)}
        else:
            out = {k: regex.sub(repl, v) if isinstance(v, str) else v for k, v in out.items()}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="collector/otel-collector-config.yaml")
    args = parser.parse_args()

    rules = load_statements(args.config)
    print(f"PII-REGEX-001 · {args.config} · span={len(rules.get('span', []))} spanevent={len(rules.get('spanevent', []))} reglas")
    failures = 0
    for i, (context, attrs, expected) in enumerate(FIXTURES, 1):
        result = apply(rules.get(context, []), attrs)
        problems = []
        for key, value in result.items():
            if isinstance(value, str) and (PAN.search(value) or EMAIL.search(value) or ACCOUNT.search(value)):
                problems.append(f"PII en claro {key}={value}")
        problems += [f"llave prohibida {k}" for k in FORBIDDEN_KEYS & result.keys()]
        problems += [f"{k}: esperado '{v}', obtenido '{result.get(k)}'" for k, v in expected.items() if result.get(k) != v]
        status = "FAIL" if problems else "PASS"
        failures += bool(problems)
        print(f"[{status}] #{i:<2} {context:<9} {next(iter(attrs))}")
        for p in problems:
            print(f"           {p}")
    print("RESULT:", "FAIL" if failures else "PASS", f"({len(FIXTURES) - failures}/{len(FIXTURES)})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
