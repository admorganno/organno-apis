import json
import os
import time
import logging
from datetime import date, timedelta

import psycopg2
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ.get("DB_NAME", "railway")
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DURATIONS_FILE = os.path.join(SCRIPT_DIR, "product_durations.json")


def load_durations() -> dict:
    with open(DURATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def connect_db():
    # Railway pode estar dormindo — tenta acordar e reconecta
    for attempt in range(3):
        try:
            conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                dbname=DB_NAME,
                connect_timeout=20,
            )
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"Tentativa {attempt + 1} falhou: {e}")
            if attempt < 2:
                time.sleep(8)
    raise RuntimeError("Não foi possível conectar ao banco após 3 tentativas")


def fetch_expiring_today(cur, codes: list[str], today: date) -> list[dict]:
    """
    Busca compras onde data_compra + dias_produto = hoje.
    Faz a conta no Python pra evitar query gigante por codigo.
    """
    # Agrupa códigos por quantidade de dias pra minimizar queries
    by_days: dict[int, list[str]] = {}
    durations = load_durations()
    for code in codes:
        dias = durations[code]["dias"]
        by_days.setdefault(dias, []).append(code)

    results = []
    for dias, group_codes in by_days.items():
        purchase_date = today - timedelta(days=dias)
        cur.execute(
            """
            SELECT
                dv.codigo_produto,
                dv.quantidade,
                v.codigo_venda,
                v.cpf_cliente,
                v.data AS data_compra,
                c.nome,
                c.ddd_celular,
                c.celular,
                c.email
            FROM detalhes_vendas dv
            JOIN vendas v ON v.codigo_venda = dv.codigo_venda
            LEFT JOIN clientes c ON c.cpf = v.cpf_cliente
            WHERE dv.codigo_produto = ANY(%s)
              AND v.data = %s
              AND dv.status_produto_vendido = 'Válido'
              AND v.status = 'Válido'
            """,
            (group_codes, purchase_date),
        )
        for row in cur.fetchall():
            codigo = row[0].strip()
            info = durations.get(codigo, {})
            results.append(
                {
                    "codigo_produto": codigo,
                    "nome_produto": info.get("nome", ""),
                    "dias_duracao": dias,
                    "quantidade": int(row[1]) if row[1] else 1,
                    "codigo_venda": int(row[2]),
                    "cpf_cliente": row[3],
                    "data_compra": str(row[4]),
                    "data_expiracao": str(today),
                    "cliente_nome": row[5],
                    "cliente_ddd": row[6],
                    "cliente_celular": row[7],
                    "cliente_email": row[8],
                }
            )
    return results


def send_webhook(payload: dict) -> bool:
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Webhook falhou para venda {payload.get('codigo_venda')}: {e}")
        return False


def run():
    today = date.today()
    log.info(f"🚀 Iniciando verificação de expiração — {today}")

    durations = load_durations()
    codes = list(durations.keys())

    conn = connect_db()
    cur = conn.cursor()

    expiring = fetch_expiring_today(cur, codes, today)
    cur.close()
    conn.close()

    log.info(f"📦 {len(expiring)} compras expiram hoje")

    ok = 0
    fail = 0
    for item in expiring:
        log.info(
            f"  → venda {item['codigo_venda']} | {item['nome_produto']} | "
            f"cliente: {item['cliente_nome']} {item['cliente_ddd']}{item['cliente_celular']}"
        )
        if send_webhook(item):
            ok += 1
        else:
            fail += 1

    log.info(f"✅ {ok} webhooks enviados | ❌ {fail} falhas")


if __name__ == "__main__":
    run()
