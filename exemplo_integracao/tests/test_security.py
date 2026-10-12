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

def test_chargeback_com_objeto_aninhado_suspende_pedido(monkeypatch):
    order = orders.create_order([{"sku": "CURSO-PYTHON", "quantity": 1}])
    order_id = order["id"]
    with db() as conn:
        conn.execute("UPDATE orders SET method='CARD_3DS', asaas_payment_id='pay_cb', status='PAID' WHERE id=?", (order_id,))
    
    # O Asaas envia a cobranca com status CONFIRMED mas objeto chargeback REQUESTED
    remote_mock = {
        "id": "pay_cb", "externalReference": order_id, "billingType": "CREDIT_CARD",
        "value": 199.90, "status": "CONFIRMED",
        "chargeback": {
            "id": "cb_123",
            "status": "REQUESTED",
            "reason": "ABSENT_CARD_FRAUD",
            "deadlineToSendDisputeDocuments": "2026-10-30"
        }
    }
    monkeypatch.setattr(asaas, "get_payment", lambda pid: remote_mock)
    
    event_payload = {"id": "evt_cb_1", "event": "PAYMENT_CHARGEBACK_REQUESTED", "payment": {"id": "pay_cb"}}
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    
    updated = orders.get_order(order_id)
    assert updated["status"] == "DISPUTED"
    assert updated["dispute_reason"] == "ABSENT_CARD_FRAUD"
    assert updated["dispute_deadline"] == "2026-10-30"

def test_checkout_paid_valida_sessao_e_libera_pedido():
    order = orders.create_order([{"sku": "CURSO-PYTHON", "quantity": 1}])
    order_id = order["id"]
    with db() as conn:
        conn.execute("UPDATE orders SET method='HOSTED_CHECKOUT', asaas_checkout_id='chk_123', status='AWAITING_PAYMENT' WHERE id=?", (order_id,))
    
    event_payload = {
        "id": "evt_chk_1",
        "event": "CHECKOUT_PAID",
        "checkout": {"id": "chk_123", "status": "PAID", "externalReference": order_id}
    }
    client.post("/webhooks/asaas", json=event_payload, headers=AUTH_HEADER)
    
    updated = orders.get_order(order_id)
    assert updated["status"] == "PAID"

