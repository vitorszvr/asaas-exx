import json
import logging
from . import asaas
from .db import db, transition
from .decision import SecurityDivergence, decide_payment_status

log = logging.getLogger("worker")

ALLOWED_TRANSITIONS = {
    "PAID": {"AWAITING_PAYMENT", "DISPUTED"},
    "DISPUTED": {"PAID", "REVERSED"},
    "PARTIALLY_REFUNDED": {"PAID", "DISPUTED"},
    "REVERSED": {"PAID", "DISPUTED", "PARTIALLY_REFUNDED"},
    "FAILED": {"AWAITING_PAYMENT"},
}

def apply_state(order_id: str, new_status: str) -> None:
    with db() as conn:
        changed = transition(conn, order_id, new_status, ALLOWED_TRANSITIONS[new_status])
    if changed:
        if new_status == "PAID":
            log.info("Pedido %s APROVADO financeiramente. Liberando entrega...", order_id)
        elif new_status in {"DISPUTED", "REVERSED"}:
            log.warning("Pedido %s CONTESTADO ou ESTORNADO. Suspendendo acesso...", order_id)

def process_single_event(event_row) -> None:
    payload = json.loads(event_row["payload"])
    event_name = payload["event"]

    # TRATAMENTO PARA POSSIBILIDADE 2: EVENTOS DE CHECKOUT HOSPEDADO
    # A documentação pública não possui endpoint GET /v3/checkouts/{id}.
    # Por isso, validamos a sessão localmente e conferimos o status do payload.
    if event_name.startswith("CHECKOUT_"):
        checkout_data = payload.get("checkout") or {}
        checkout_id = checkout_data.get("id")
        with db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE asaas_checkout_id=?", (checkout_id,)).fetchone()
        if not order:
            return log.warning("Checkout %s não vinculado a pedido local", checkout_id)

        # Validação cruzada com dados da sessão
        if event_name == "CHECKOUT_PAID":
            if checkout_data.get("status") == "PAID" and checkout_data.get("externalReference") == order["id"]:
                apply_state(order["id"], "PAID")
            else:
                log.error("Divergência de payload no evento CHECKOUT_PAID para o pedido %s", order["id"])
        elif event_name in {"CHECKOUT_CANCELED", "CHECKOUT_EXPIRED"}:
            apply_state(order["id"], "FAILED")
        return

    # TRATAMENTO PARA POSSIBILIDADE 1: EVENTOS DE COBRANÇA DIRETA
    payment_id = payload.get("payment", {}).get("id")
    if not payment_id:
        return

    # Eventos de falha ou recusa de risco
    if event_name in {
        "PAYMENT_CREDIT_CARD_CAPTURE_REFUSED",
        "PAYMENT_CREDIT_CARD_THREE_D_SECURE_CHALLENGE_FAILED",
        "PAYMENT_REPROVED_BY_RISK_ANALYSIS",
    }:
        with db() as conn:
            order = conn.execute("SELECT id FROM orders WHERE asaas_payment_id=?", (payment_id,)).fetchone()
        if order:
            apply_state(order["id"], "FAILED")
        return

    # Reconsulta ativa na API oficial do Asaas
    remote_payment = asaas.get_payment(payment_id)
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE asaas_payment_id=?", (payment_id,)).fetchone()
    if not order:
        return log.warning("Pagamento %s desconhecido localmente", payment_id)

    try:
        new_state = decide_payment_status(order, remote_payment)
        if new_state and new_state != order["status"]:
            apply_state(order["id"], new_state)
            
            # Registro de Metadados de Chargeback para Defesa Operacional
            if new_state == "DISPUTED":
                cb = remote_payment.get("chargeback") or {}
                cb_id = cb.get("id")
                cb_reason = cb.get("reason", "ABSENT_CARD_FRAUD")
                cb_deadline = cb.get("deadlineToSendDisputeDocuments")
                with db() as conn:
                    conn.execute(
                        "UPDATE orders SET dispute_reason=?, dispute_deadline=? WHERE id=?",
                        (cb_reason, cb_deadline, order["id"]),
                    )
                log.warning(
                    "ALERTA OPERACIONAL [CHARGEBACK]: Pedido %s contestado pelo portador! "
                    "Disputa ID: %s | Motivo: %s | Prazo limite para contestação via POST /v3/chargebacks/%s/dispute: %s",
                    order["id"], cb_id, cb_reason, cb_id, cb_deadline or "Verificar no painel Asaas"
                )
    except SecurityDivergence as exc:
        log.error("Divergência de segurança no pedido %s: %s", order["id"], exc)

def process_pending_webhooks():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM webhook_events WHERE state IN ('RECEBIDO', 'COM_ERRO') AND attempts < 5 LIMIT 50"
        ).fetchall()
    for r in rows:
        with db() as conn:
            conn.execute("UPDATE webhook_events SET state='EM_PROCESSAMENTO', attempts=attempts+1 WHERE id=?", (r["id"],))
        try:
            process_single_event(r)
            with db() as conn:
                conn.execute("UPDATE webhook_events SET state='PROCESSADO' WHERE id=?", (r["id"],))
        except Exception:
            with db() as conn:
                conn.execute("UPDATE webhook_events SET state='COM_ERRO' WHERE id=?", (r["id"],))
            log.exception("Falha no processamento do evento de webhook %s", r["id"])

