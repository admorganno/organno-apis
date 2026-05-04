import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
import schedule

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY     = os.environ["NUMMUS_API_KEY"]
CLIENT_ID   = os.environ["NUMMUS_CLIENT_ID"]
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://n8n.quanthum.cloud/webhook/cashback-organno-nummus",
)
# 11:00 BRT = 14:00 UTC
RUN_AT_UTC   = os.environ.get("RUN_AT_UTC", "14:00")
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() == "true"

BASE_URL       = "https://api.production.nummus.com.br/v1"
TARGET_DAYS    = [7, 3, 1]
API_PAGE_LIMIT = 50   # máximo suportado pela API Nummus

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
def _fetch_page(period_start: str, period_end: str, offset: int) -> dict:
    params = {
        "limit":  API_PAGE_LIMIT,
        "offset": offset,
        "period": json.dumps({"start": period_start, "end": period_end}),
    }
    resp = requests.get(f"{BASE_URL}/cashback", headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_all_cashbacks(period_start: str, period_end: str) -> list[dict]:
    all_items: list[dict] = []
    page = 0  # API usa offset como número de página (0, 1, 2...), não índice de registro
    while True:
        data    = _fetch_page(period_start, period_end, page)
        content = data.get("content", [])
        all_items.extend(content)
        if not data.get("nextPage"):
            break
        page += 1
        time.sleep(0.15)
    return all_items


def parse_op_date(dh_operation: str) -> date:
    return datetime.strptime(dh_operation, "%d/%m/%Y %H:%M").date()


def fetch_balance(customer_id: str) -> float | None:
    try:
        resp = requests.get(
            f"{BASE_URL}/cashback/amount",
            headers=HEADERS,
            params={"customer_id": customer_id},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("balance_available")
    except Exception:
        return None


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
def job_expiry_check():
    today = date.today()
    log.info(f"🔍 [EXPIRAÇÃO] Verificação iniciada — {today}")

    # Cobre expiresIn de 1 a 70 dias para os 3 alvos (1, 3 e 7 dias à frente)
    period_start = (today - timedelta(days=70)).strftime("%Y-%m-%d")
    period_end   = today.strftime("%Y-%m-%d")
    log.info(f"📅 Consultando operações de {period_start} até {period_end}")

    try:
        cashbacks = fetch_all_cashbacks(period_start, period_end)
    except Exception as e:
        log.error(f"❌ Erro ao buscar cashbacks: {e}")
        return

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

            saldo = fetch_balance(cb["customer"]["id"])
            payload = {**cb, "periodo": targets[expiry], "saldo_total": saldo}
            if send_webhook(payload):
                sent += 1
                log.info(
                    f"✅ {cb['customer']['name']} | "
                    f"R$ {cb['value_cashback']:.2f} | "
                    f"vence em {targets[expiry]} ({expiry})"
                )
            else:
                errors += 1
        except Exception as e:
            log.error(f"⚠️  Erro no cashback {cb.get('id')}: {e}")
            errors += 1

    log.info(f"🏁 [EXPIRAÇÃO] Concluído — enviados: {sent} | erros: {errors}")


# ── Job 2: cashbacks gerados ontem ──────────────────────────────────────────
def job_generated_check():
    # Roda às 23h BRT → captura todos os cashbacks criados no dia anterior
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    log.info(f"🛒 [GERADOS] Buscando cashbacks criados em {yesterday}")

    try:
        cashbacks = fetch_all_cashbacks(yesterday, yesterday)
    except Exception as e:
        log.error(f"❌ Erro ao buscar cashbacks gerados: {e}")
        return

    log.info(f"📦 {len(cashbacks)} cashbacks gerados em {yesterday}")

    sent = errors = 0
    for cb in cashbacks:
        try:
            saldo = fetch_balance(cb["customer"]["id"])
            payload = {**cb, "periodo": "cashback gerado", "saldo_total": saldo}
            if send_webhook(payload):
                sent += 1
                log.info(
                    f"✅ {cb['customer']['name']} | "
                    f"R$ {cb['value_cashback']:.2f} | gerado em {yesterday}"
                )
            else:
                errors += 1
        except Exception as e:
            log.error(f"⚠️  Erro no cashback {cb.get('id')}: {e}")
            errors += 1

    log.info(f"🏁 [GERADOS] Concluído — enviados: {sent} | erros: {errors}")


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if RUN_ON_START:
        log.info("▶️  RUN_ON_START=true — executando ambos os jobs agora")
        job_expiry_check()
        job_generated_check()

    schedule.every().day.at(RUN_AT_UTC).do(job_expiry_check)
    schedule.every().day.at(RUN_AT_UTC).do(job_generated_check)

    log.info(f"⏰ Ambos os jobs agendados para {RUN_AT_UTC} UTC (11:00 BRT)")

    while True:
        schedule.run_pending()
        time.sleep(30)
