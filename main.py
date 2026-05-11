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
WEBHOOK_URL             = os.environ["WEBHOOK_URL"]
WEBHOOK_ANIVERSARIO_URL = os.environ.get(
    "WEBHOOK_ANIVERSARIO_URL",
    "https://n8n.quanthum.cloud/webhook/mapeamento-organno",
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DURATIONS_FILE = os.path.join(SCRIPT_DIR, "product_durations.json")


def load_durations() -> dict:
    with open(DURATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def connect_db():
    # Primeira conexão acorda o Railway (pode falhar); segunda garante os dados
    log.info("🔌 Acordando banco...")
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, dbname=DB_NAME, connect_timeout=20,
        )
        conn.cursor().execute("SELECT 1")
        conn.close()
        log.info("✓ Banco acordado")
    except Exception as e:
        log.warning(f"Wake-up falhou (normal se estava dormindo): {e}")

    time.sleep(6)

    for attempt in range(3):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, user=DB_USER,
                password=DB_PASSWORD, dbname=DB_NAME, connect_timeout=20,
            )
            return conn
        except psycopg2.OperationalError as e:
            log.warning(f"Tentativa {attempt + 1}/3 falhou: {e}")
            if attempt < 2:
                time.sleep(8)

    raise RuntimeError("Não foi possível conectar ao banco após 3 tentativas")


def build_whatsapp_number(ddd_cel: str, celular: str, ddd_tel: str, telefone: str) -> str | None:
    """Monta número completo para WhatsApp. Celular tem prioridade sobre telefone."""
    cel = (celular or "").strip().replace("-", "").replace(" ", "")
    ddd_c = (ddd_cel or "").strip()
    if ddd_c and cel:
        return f"55{ddd_c}{cel}"

    tel = (telefone or "").strip().replace("-", "").replace(" ", "")
    ddd_t = (ddd_tel or "").strip()
    if ddd_t and tel:
        return f"55{ddd_t}{tel}"

    return None


def fetch_expiring_today(cur, durations: dict, today: date) -> list[dict]:
    # Agrupa códigos por dias para minimizar queries ao banco
    by_days: dict[int, list[str]] = {}
    for code, info in durations.items():
        by_days.setdefault(info["dias"], []).append(code)

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
                v.data            AS data_compra,
                c.nome            AS cliente_nome,
                c.ddd_celular,
                c.celular,
                c.ddd_telefone,
                c.telefone,
                c.email           AS cliente_email
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
            ddd_cel, celular, ddd_tel, telefone = row[6], row[7], row[8], row[9]
            whatsapp = build_whatsapp_number(ddd_cel, celular, ddd_tel, telefone)
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
                    "cliente_whatsapp": whatsapp,
                    "cliente_email": row[10],
                }
            )
    return results


def fetch_birthdays_today(cur, today: date) -> list[dict]:
    cur.execute(
        """
        SELECT
            c.nome,
            c.ddd_celular,
            c.celular,
            c.ddd_telefone,
            c.telefone
        FROM clientes c
        WHERE EXTRACT(MONTH FROM c.data_nascimento) = %s
          AND EXTRACT(DAY   FROM c.data_nascimento) = %s
          AND EXTRACT(YEAR  FROM c.data_nascimento) >= 1920
          AND c.data_nascimento IS NOT NULL
        ORDER BY c.nome
        """,
        (today.month, today.day),
    )
    results = []
    for row in cur.fetchall():
        nome, ddd_cel, celular, ddd_tel, telefone = row
        telefone_completo = build_whatsapp_number(ddd_cel, celular, ddd_tel, telefone)
        if not telefone_completo:
            continue
        results.append(
            {
                "event": "aniversario",
                "nome": nome,
                "telefone": telefone_completo,
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
    log.info(f"🚀 Iniciando verificação — {today}")

    durations = load_durations()

    conn = connect_db()
    cur = conn.cursor()

    expiring  = fetch_expiring_today(cur, durations, today)
    birthdays = fetch_birthdays_today(cur, today)

    cur.close()
    conn.close()

    log.info(f"📦 {len(expiring)} compras expiram hoje")
    log.info(f"🎂 {len(birthdays)} aniversariantes hoje")

    ok = fail = 0

    for item in expiring:
        log.info(
            f"  → venda {item['codigo_venda']} | {item['nome_produto']} | "
            f"cliente: {item['cliente_nome']} | whatsapp: {item['cliente_whatsapp']}"
        )
        if send_webhook(item):
            ok += 1
        else:
            fail += 1

    for item in birthdays:
        log.info(f"  🎂 {item['nome']} | telefone: {item['telefone']}")
        try:
            resp = requests.post(WEBHOOK_ANIVERSARIO_URL, json=item, timeout=15)
            resp.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            log.error(f"Webhook aniversário falhou para {item['nome']}: {e}")
            fail += 1

    log.info(f"✅ {ok} webhooks enviados | ❌ {fail} falhas")


if __name__ == "__main__":
    run()
