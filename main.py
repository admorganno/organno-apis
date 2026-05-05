import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY     = os.environ["NUMMUS_API_KEY"]
CLIENT_ID   = os.environ["NUMMUS_CLIENT_ID"]
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://n8n.quanthum.cloud/webhook/cashback-organno-nummus",
)

BASE_URL       = "https://api.production.nummus.com.br/v1"
TARGET_DAYS    = [7, 3, 1]
API_PAGE_LIMIT = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

HEADERS = {
    "x-api-key":   API_KEY,
    "x-client-id": CLIENT_ID,
    "Accept":      "application/json",
}


# ── Nummus API ───────────────────────────────────────────────────────────────
def fetch_all_cashbacks(period_start: str, period_end: str, customer_filter: dict | None = None) -> list[dict]:
    all_items: list[dict] = []
    page = 0
    while True:
        params: dict = {
            "limit":  API_PAGE_LIMIT,
            "offset": page,
            "period": json.dumps({"start": period_start, "end": period_end}),
        }
        if customer_filter:
            params["customer"] = json.dumps(customer_filter)
        resp = requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data    = resp.json()
        content = data.get("content", [])
        all_items.extend(content)
        if not data.get("nextPage"):
            break
        page += 1
        time.sleep(0.15)
    return all_items


def parse_op_date(dh_operation: str) -> date:
    return datetime.strptime(dh_operation, "%d/%m/%Y %H:%M").date()


def calc_saldo_total(document_number: str, today: date) -> float:
    total = 0.0
    start = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")
    cbs   = fetch_all_cashbacks(start, end, customer_filter={"document_number": document_number})
    for cb in cbs:
        vals      = [p["expiresIn"] for p in cb.get("products", []) if p.get("expiresIn")]
        expiry    = parse_op_date(cb["dh_operation"]) + timedelta(days=min(vals) if vals else 40)
        remaining = cb.get("value_cashback", 0) - cb.get("value_rescued", 0)
        if expiry >= today and remaining > 0:
            total += remaining
    return round(total, 2)


# ── Webhook ──────────────────────────────────────────────────────────────────
def send_webhook(payload: dict) -> bool:
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"❌ Webhook falhou (cashback {payload.get('id')}): {e}")
        return False


# ── Job 1: cashbacks prestes a expirar ──────────────────────────────────────
def job_expiry_check(today: date) -> None:
    log.info(f"🔍 [EXPIRAÇÃO] Iniciado — {today}")

    period_start = (today - timedelta(days=70)).strftime("%Y-%m-%d")
    period_end   = today.strftime("%Y-%m-%d")
    cashbacks    = fetch_all_cashbacks(period_start, period_end)
    log.info(f"📦 {len(cashbacks)} cashbacks no período")

    targets: dict[date, str] = {
        today + timedelta(days=d): ("24 horas" if d == 1 else f"{d} dias")
        for d in TARGET_DAYS
    }

    sent = errors = 0
    for cb in cashbacks:
        try:
            vals = [p["expiresIn"] for p in cb.get("products", []) if p.get("expiresIn")]
            if not vals:
                continue
            expiry = parse_op_date(cb["dh_operation"]) + timedelta(days=min(vals))
            if expiry not in targets:
                continue
            saldo   = calc_saldo_total(cb["customer"]["document_number"], today)
            payload = {**cb, "periodo": targets[expiry], "saldo_total": saldo}
            if send_webhook(payload):
                sent += 1
                log.info(f"✅ {cb['customer']['name']} | R$ {cb['value_cashback']:.2f} | {targets[expiry]}")
            else:
                errors += 1
        except Exception as e:
            log.error(f"⚠️  Erro no cashback {cb.get('id')}: {e}")
            errors += 1

    log.info(f"🏁 [EXPIRAÇÃO] enviados: {sent} | erros: {errors}")


# ── Job 2: cashbacks gerados ontem ──────────────────────────────────────────
def job_generated_check(today: date) -> None:
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    log.info(f"🛒 [GERADOS] Buscando cashbacks de {yesterday}")

    cashbacks = fetch_all_cashbacks(yesterday, yesterday)
    log.info(f"📦 {len(cashbacks)} cashbacks gerados em {yesterday}")

    sent = errors = 0
    for cb in cashbacks:
        try:
            saldo   = calc_saldo_total(cb["customer"]["document_number"], today)
            payload = {**cb, "periodo": "cashback gerado", "saldo_total": saldo}
            if send_webhook(payload):
                sent += 1
                log.info(f"✅ {cb['customer']['name']} | R$ {cb['value_cashback']:.2f} | gerado em {yesterday}")
            else:
                errors += 1
        except Exception as e:
            log.error(f"⚠️  Erro no cashback {cb.get('id')}: {e}")
            errors += 1

    log.info(f"🏁 [GERADOS] enviados: {sent} | erros: {errors}")


# ── Wake-up ──────────────────────────────────────────────────────────────────
def wake_up_api() -> None:
    """Primeira requisição acorda o serviço (pode falhar); segunda garante resposta."""
    log.info("🔌 Acordando API Nummus...")
    try:
        requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params={"limit": 1}, timeout=20)
        log.info("✓ API acordada")
    except Exception as e:
        log.warning(f"Wake-up falhou (normal se estava dormindo): {e}")

    time.sleep(6)

    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params={"limit": 1}, timeout=20)
            resp.raise_for_status()
            log.info("✓ API pronta")
            return
        except Exception as e:
            log.warning(f"Tentativa {attempt + 1}/3 falhou: {e}")
            if attempt < 2:
                time.sleep(8)

    raise RuntimeError("API Nummus não respondeu após 3 tentativas")


# ── Entry point — roda e encerra (Railway Cron Job) ──────────────────────────
if __name__ == "__main__":
    today = date.today()
    log.info(f"🚀 Iniciando jobs — {today}")
    wake_up_api()
    job_expiry_check(today)
    job_generated_check(today)
    log.info("✅ Todos os jobs concluídos.")
