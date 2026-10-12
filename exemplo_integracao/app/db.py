import sqlite3
from datetime import datetime, timezone
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    total             TEXT NOT NULL,
    method            TEXT,                     -- CARD_3DS | PIX_DIRECT | HOSTED_CHECKOUT
    status            TEXT NOT NULL,            -- PENDING, CREATING, AWAITING_PAYMENT, PAID, DISPUTED, REVERSED, FAILED
    customer_id       TEXT,
    asaas_payment_id  TEXT UNIQUE,              -- Vinculo com cobranca direta
    asaas_checkout_id TEXT UNIQUE,              -- Vinculo com checkout hospedado
    dispute_reason    TEXT,                     -- Motivo informado pela bandeira (ex: ABSENT_CARD_FRAUD)
    dispute_deadline  TEXT,                     -- Prazo limite oficial para contestacao de chargeback
    refunded_amount   TEXT NOT NULL DEFAULT '0',
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id          TEXT PRIMARY KEY,              -- ID unico do evento enviado pelo Asaas (Idempotencia)
    event       TEXT NOT NULL,
    resource_id TEXT,                          -- payment.id ou checkout.id
    payload     TEXT NOT NULL,
    state       TEXT NOT NULL,                 -- RECEBIDO, EM_PROCESSAMENTO, PROCESSADO, COM_ERRO
    attempts    INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL
);
"""

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)

def transition(conn, order_id: str, new_status: str, allowed_from: set[str]) -> bool:
    """Garante transição atômica de status. Evita race conditions entre múltiplos workers."""
    placeholders = ",".join("?" * len(allowed_from))
    cur = conn.execute(
        f"UPDATE orders SET status=?, updated_at=? WHERE id=? AND status IN ({placeholders})",
        (new_status, now(), order_id, *allowed_from),
    )
    return cur.rowcount == 1

