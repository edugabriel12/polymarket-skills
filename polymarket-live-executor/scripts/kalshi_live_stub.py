#!/usr/bin/env python3
"""Esqueleto de execução LIVE na Kalshi — auth + gates de segurança, SEM trading.

Este módulo existe para o operador validar a autenticação (RSA-PSS) e os
gates de segurança ANTES de qualquer decisão de ir a live. Ele NÃO executa
ordens: place_order() levanta NotImplementedError incondicionalmente neste
estágio (paper-only por decisão do plano Kalshi; ver CLAUDE.md §1 regra 2 e
§4 — live exige opt-in explícito do operador e critérios de prontidão).

Autenticação da API Kalshi (portfólio/trading exigem assinatura):
  - Par de chaves RSA gerado no site da Kalshi (Account > API Keys).
  - Cada request privado leva 3 headers:
      KALSHI-ACCESS-KEY:       key_id (UUID da chave)
      KALSHI-ACCESS-TIMESTAMP: epoch em MILISSEGUNDOS (string)
      KALSHI-ACCESS-SIGNATURE: base64(RSA-PSS-SHA256(ts + METHOD + path))
    onde path é o caminho completo SEM query string, ex.:
    "/trade-api/v2/portfolio/balance".
  - RSA-PSS com MGF1(SHA-256) e salt_length = tamanho do digest.

Gates de segurança (todos obrigatórios; espelham a disciplina do
execute_live.py da Polymarket):
  KALSHI_API_KEY_ID        key id da API (UUID)
  KALSHI_PRIVATE_KEY_PATH  caminho do PEM — ARQUIVO, NUNCA a chave em env
  KALSHI_LIVE_CONFIRM      literalmente "true"

A chave privada NUNCA transita por variável de ambiente, argumento de CLI
ou log: se KALSHI_PRIVATE_KEY (o material em si) estiver setada no
ambiente, os gates FALHAM por princípio — o operador deve movê-la para um
arquivo com permissão 0600 e apontar KALSHI_PRIVATE_KEY_PATH.

Uso (smoke do operador):
  python kalshi_live_stub.py --check-auth   # gates + GET /portfolio/balance
  python kalshi_live_stub.py --test         # suíte hermética (RSA efêmero)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

API_BASE = os.environ.get("KALSHI_API_BASE",
                          "https://api.elections.kalshi.com/trade-api/v2")
# Diretório de auditoria local (fora do repo). Toda TENTATIVA de ordem —
# mesmo as rejeitadas pelo stub — fica registrada aqui.
LIVE_DIR = Path.home() / ".kalshi-live"
TRADES_LOG = LIVE_DIR / "trades.log"

REQUIRED_ENV = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH",
                "KALSHI_LIVE_CONFIRM")


# ---------------------------------------------------------------------------
# Assinatura RSA-PSS
# ---------------------------------------------------------------------------


def load_private_key(path: str | Path):
    """Carrega a chave privada RSA de um ARQUIVO PEM. O conteúdo nunca é
    logado nem retornado como string — só o objeto de chave."""
    from cryptography.hazmat.primitives import serialization
    pem = Path(path).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def sign_kalshi_request(private_key, ts_ms: str, method: str, path: str) -> str:
    """Assina `{ts_ms}{METHOD}{path}` com RSA-PSS SHA-256 e retorna base64.

    `path` deve ser o caminho completo do request SEM query string
    (ex. "/trade-api/v2/portfolio/balance"). `method` em maiúsculas.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    payload = f"{ts_ms}{method.upper()}{path}".encode("utf-8")
    sig = private_key.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def build_auth_headers(key_id: str, private_key, method: str, path: str,
                       ts_ms: Optional[str] = None) -> dict:
    """Monta os 3 headers de autenticação da Kalshi para um request."""
    ts_ms = ts_ms or str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": sign_kalshi_request(
            private_key, ts_ms, method, path),
    }


# ---------------------------------------------------------------------------
# Gates de segurança
# ---------------------------------------------------------------------------


def check_safety_gates(env: Optional[dict] = None) -> tuple[bool, list[str]]:
    """Valida os pré-requisitos de live. Retorna (ok, problemas).

    NUNCA lê o conteúdo da chave — só verifica que o arquivo existe. A
    presença de KALSHI_PRIVATE_KEY (material da chave em env) é falha DURA:
    chave privada vem de arquivo, nunca de variável de ambiente.
    """
    env = os.environ if env is None else env
    problems: list[str] = []

    if env.get("KALSHI_PRIVATE_KEY"):
        problems.append(
            "KALSHI_PRIVATE_KEY setada no ambiente — a chave privada deve "
            "ficar num ARQUIVO (chmod 600) apontado por "
            "KALSHI_PRIVATE_KEY_PATH, nunca em env. Remova a variável.")

    if not env.get("KALSHI_API_KEY_ID"):
        problems.append("KALSHI_API_KEY_ID ausente (key id da API Kalshi).")

    key_path = env.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_path:
        problems.append("KALSHI_PRIVATE_KEY_PATH ausente (caminho do PEM).")
    elif not Path(key_path).is_file():
        problems.append(
            f"KALSHI_PRIVATE_KEY_PATH não aponta para um arquivo: {key_path}")

    if env.get("KALSHI_LIVE_CONFIRM") != "true":
        problems.append(
            'KALSHI_LIVE_CONFIRM != "true" — gate de confirmação explícita.')

    return (not problems, problems)


# ---------------------------------------------------------------------------
# Endpoints privados
# ---------------------------------------------------------------------------


def get_balance(timeout: int = 15) -> Optional[dict]:
    """Único endpoint privado do esqueleto: GET /portfolio/balance.

    Serve como smoke de autenticação — se isto responde 200, a assinatura
    RSA-PSS e o key id estão corretos. Retorna o JSON ou levanta
    RuntimeError com o status (sem vazar headers/assinatura)."""
    ok, problems = check_safety_gates()
    if not ok:
        raise RuntimeError("safety gates failed: " + " | ".join(problems))
    key_id = os.environ["KALSHI_API_KEY_ID"]
    pk = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    # path assinado = caminho completo sem query string
    path = "/trade-api/v2/portfolio/balance"
    url = API_BASE.rstrip("/").removesuffix("/trade-api/v2") + path
    headers = build_auth_headers(key_id, pk, "GET", path)
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"balance request failed: HTTP {r.status_code}")
    return r.json()


def _log_order_attempt(payload: dict) -> None:
    """Auditoria local de TODA tentativa de ordem (mesmo rejeitada)."""
    try:
        LIVE_DIR.mkdir(mode=0o700, exist_ok=True)
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "order_attempt_rejected_stub",
            **payload,
        }, ensure_ascii=False)
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:
            os.chmod(TRADES_LOG, 0o600)
        except OSError:
            pass
    except Exception:
        pass  # auditoria nunca derruba o caller


def place_order(ticker: str, side: str, contracts: int, price: float,
                **_kw) -> dict:
    """SEMPRE levanta NotImplementedError neste estágio.

    A execução live na Kalshi NÃO está habilitada — este PR entrega apenas
    o esqueleto de auth/gates (decisão do plano: paper agora, live depois
    dos critérios de prontidão do §4 do CLAUDE.md e de novo opt-in
    explícito do operador). A tentativa fica auditada em
    ~/.kalshi-live/trades.log.
    """
    _log_order_attempt({"ticker": ticker, "side": side,
                        "contracts": contracts, "price": price})
    raise NotImplementedError("live execution not enabled — paper only")


# ---------------------------------------------------------------------------
# Testes herméticos
# ---------------------------------------------------------------------------


def _test() -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    import tempfile

    # T1: assinatura RSA-PSS verifica com a chave pública (par efêmero).
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ts = "1760000000000"
    sig_b64 = sign_kalshi_request(key, ts, "get",
                                  "/trade-api/v2/portfolio/balance")
    key.public_key().verify(
        base64.b64decode(sig_b64),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # levanta InvalidSignature se errado
    print("Test 1 PASS: RSA-PSS assina/verifica; method normalizado p/ GET")

    # T2: headers completos e coerentes.
    h = build_auth_headers("key-123", key, "POST", "/trade-api/v2/orders",
                           ts_ms=ts)
    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == ts
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        f"{ts}POST/trade-api/v2/orders".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    print("Test 2 PASS: build_auth_headers com os 3 headers assinados")

    # T3: gates negam por default e listam TODOS os problemas.
    ok, probs = check_safety_gates(env={})
    assert not ok and len(probs) == 3, probs
    print("Test 3 PASS: gates negam sem env (3 problemas listados)")

    # T4: chave em ENV é falha dura mesmo com o resto correto.
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as tf:
        tf.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        pem_path = tf.name
    try:
        good = {"KALSHI_API_KEY_ID": "key-123",
                "KALSHI_PRIVATE_KEY_PATH": pem_path,
                "KALSHI_LIVE_CONFIRM": "true"}
        ok, probs = check_safety_gates(env=good)
        assert ok and not probs, probs
        bad = dict(good, KALSHI_PRIVATE_KEY="-----BEGIN FAKE-----")
        ok, probs = check_safety_gates(env=bad)
        assert not ok and any("nunca em env" in p for p in probs), probs
        print("Test 4 PASS: env completo passa; chave em env = falha dura")

        # T5: CONFIRM diferente de "true" nega.
        ok, probs = check_safety_gates(env=dict(good,
                                                KALSHI_LIVE_CONFIRM="TRUE"))
        assert not ok, probs
        print('Test 5 PASS: KALSHI_LIVE_CONFIRM exige literalmente "true"')

        # T6: load_private_key lê o PEM do arquivo (nunca de env).
        pk = load_private_key(pem_path)
        assert pk.key_size == 2048
        print("Test 6 PASS: load_private_key carrega PEM do arquivo")
    finally:
        os.unlink(pem_path)

    # T7: place_order SEMPRE levanta, mesmo com tudo configurado.
    try:
        place_order("KXHIGHNY-26JUL12-T87", "YES", 10, 0.40)
        raise AssertionError("place_order deveria levantar NotImplementedError")
    except NotImplementedError as e:
        assert "paper only" in str(e)
    print("Test 7 PASS: place_order levanta NotImplementedError incondicional")

    print("\nAll kalshi_live_stub self-tests PASS (7/7)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-auth", action="store_true",
                    help="valida gates e faz GET /portfolio/balance")
    ap.add_argument("--test", action="store_true",
                    help="suíte hermética (RSA efêmero, sem rede)")
    args = ap.parse_args()

    if args.test:
        _test()
        return
    if args.check_auth:
        ok, problems = check_safety_gates()
        if not ok:
            print("Safety gates: FAIL")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print("Safety gates: OK")
        bal = get_balance()
        # A API retorna centavos; imprime cru + interpretação.
        print(f"Balance response: {json.dumps(bal)}")
        print("Auth OK — assinatura RSA-PSS aceita pela Kalshi.")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
