import os
import json
import pytest
from fastapi.testclient import TestClient

# Mock das variáveis de ambiente para testes
os.environ.update({
    "ASAAS_API_KEY": "test_key",
    "ASAAS_WEBHOOK_TOKEN": "token_seguro_com_mais_de_32_caracteres",
    "PUBLIC_BASE_URL": "https://teste.com",
    "DB_PATH": ":memory:",
})

from app.main import app
from app.db import init_db, db
from app import orders, asaas, worker
from app.decision import decide_payment_status

client = TestClient(app)
AUTH_HEADER = {"asaas-access-token": "token_seguro_com_mais_de_32_caracteres"}

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", str(tmp_path / "teste.db"))
    init_db()

def test_ataque_1_webhook_sem_token_deve_retornar_401():
    response = client.post("/webhooks/asaas", json={"id": "evt_fake", "event": "PAYMENT_CONFIRMED"})
    assert response.status_code == 401

def test_ataque_2_tentativa_de_forjar_pagamento_pela_url_de_retorno():
    order = orders.create_order([{"sku": "CURSO-PYTHON", "quantity": 1}])
    res = client.get(f"/api/orders/{order['id']}/status")
    assert res.json()["status"] == "PENDING"

def test_ataque_3_adulteracao_de_preco_no_payload():
    res = client.post("/api/orders", json=[{"sku": "CURSO-PYTHON", "quantity": 1}])
    assert res.json()["total"] == "199.90"

def test_validacao_luhn_cartao():
    assert orders.luhn_check("4111111111111111") is True
    assert orders.luhn_check("4111111111111112") is False

def _paid_card_order(payment_id: str, status: str = "PAID") -> str:
    order_id = orders.create_order([{"sku": "CURSO-PYTHON", "quantity": 1}])["id"]
    with db() as conn:
        conn.execute(
            "UPDATE orders SET method='CARD_3DS', asaas_payment_id=?, status=? WHERE id=?",
            (payment_id, status, order_id),
        )
    return order_id

def _remote(order_id: str, chargeback_status: str, status: str = "CONFIRMED") -> dict:
    # Objeto aninhado no webhook/GET payment: apenas status e reason (doc "Chargeback")
    return {
        "id": "pay_cb", "externalReference": order_id, "billingType": "CREDIT_CARD",
        "value": 199.90, "status": status,
        "chargeback": {"status": chargeback_status, "reason": "ABSENT_CARD_FRAUD"},
    }

def test_chargeback_com_objeto_aninhado_suspende_pedido(monkeypatch):
    order_id = _paid_card_order("pay_cb")
    monkeypatch.setattr(asaas, "get_payment", lambda pid: _remote(order_id, "REQUESTED"))
    # id e prazo de defesa vêm de GET /v3/payments/{id}/chargeback
    monkeypatch.setattr(asaas, "get_chargeback", lambda pid: {
        "id": "cb_123", "status": "REQUESTED", "reason": "ABSENT_CARD_FRAUD",
        "deadlineToSendDisputeDocuments": "2026-10-30",
    })

    event_payload = {"id": "evt_cb_1", "event": "PAYMENT_CHARGEBACK_REQUESTED", "payment": {"id": "pay_cb"}}
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)

    updated = orders.get_order(order_id)
    assert updated["status"] == "DISPUTED"
    assert updated["dispute_reason"] == "ABSENT_CARD_FRAUD"
    assert updated["dispute_deadline"] == "2026-10-30"

def test_chargeback_revertido_a_favor_do_lojista_volta_a_pago(monkeypatch):
    order_id = _paid_card_order("pay_cb", status="DISPUTED")
    monkeypatch.setattr(asaas, "get_payment", lambda pid: _remote(order_id, "REVERSED"))
    event_payload = {"id": "evt_cb_2", "event": "PAYMENT_CONFIRMED", "payment": {"id": "pay_cb"}}
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    assert orders.get_order(order_id)["status"] == "PAID"

def test_disputa_perdida_estorna_pedido(monkeypatch):
    order_id = _paid_card_order("pay_cb", status="DISPUTED")
    monkeypatch.setattr(asaas, "get_payment", lambda pid: _remote(order_id, "DISPUTE_LOST", status="REFUNDED"))
    event_payload = {"id": "evt_cb_3", "event": "PAYMENT_REFUNDED", "payment": {"id": "pay_cb"}}
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    assert orders.get_order(order_id)["status"] == "REVERSED"

def test_disputa_vencida_aguardando_repasse_mantem_pedido_suspenso(monkeypatch):
    order_id = _paid_card_order("pay_cb", status="DISPUTED")
    remote = _remote(order_id, "REVERSED", status="AWAITING_CHARGEBACK_REVERSAL")
    monkeypatch.setattr(asaas, "get_payment", lambda pid: remote)
    monkeypatch.setattr(asaas, "get_chargeback", lambda pid: remote["chargeback"])
    event_payload = {"id": "evt_cb_4", "event": "PAYMENT_AWAITING_CHARGEBACK_REVERSAL", "payment": {"id": "pay_cb"}}
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    assert orders.get_order(order_id)["status"] == "DISPUTED"

def _checkout_order() -> str:
    order_id = orders.create_order([{"sku": "CURSO-PYTHON", "quantity": 1}])["id"]
    with db() as conn:
        conn.execute("UPDATE orders SET method='HOSTED_CHECKOUT', asaas_checkout_id='chk_123', status='AWAITING_PAYMENT' WHERE id=?", (order_id,))
    return order_id

def test_checkout_paid_valida_sessao_e_libera_pedido():
    order_id = _checkout_order()
    event_payload = {
        "id": "evt_chk_1",
        "event": "CHECKOUT_PAID",
        "checkout": {"id": "chk_123", "status": "PAID",
                     "items": [{"name": "Pedido", "quantity": 1, "value": 199.90}]},
    }
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    assert orders.get_order(order_id)["status"] == "PAID"

def test_checkout_paid_com_valor_divergente_nao_libera_pedido():
    order_id = _checkout_order()
    event_payload = {
        "id": "evt_chk_2",
        "event": "CHECKOUT_PAID",
        "checkout": {"id": "chk_123", "status": "PAID",
                     "items": [{"name": "Pedido", "quantity": 1, "value": 0.01}]},
    }
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    assert orders.get_order(order_id)["status"] == "AWAITING_PAYMENT"

