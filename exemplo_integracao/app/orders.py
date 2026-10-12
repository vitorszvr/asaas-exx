import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from . import asaas
from .config import CATALOG, PUBLIC_BASE_URL, ASAAS_BASE_URL
from .db import db, now, transition

def create_order(items: list[dict]) -> dict:
    total = Decimal("0")
    for item in items:
        product = CATALOG.get(item["sku"])
        qty = int(item["quantity"])
        if product is None or not 1 <= qty <= 10:
            raise ValueError("Item inválido ou quantidade fora dos limites permitidos")
        total += Decimal(product["price"]) * qty
    order_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO orders (id, total, status, updated_at) VALUES (?, ?, 'PENDING', ?)",
            (order_id, str(total), now()),
        )
    return {"id": order_id, "total": str(total)}

def get_order(order_id: str):
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

def _get_or_create_customer(order, customer_data: dict) -> str:
    """Busca cliente existente por CPF/CNPJ ou cria novo, persistindo o ID no pedido."""
    if order["customer_id"]:
        return order["customer_id"]
    existing = asaas.find_customer_by_cpf_cnpj(customer_data["cpfCnpj"])
    cust_id = existing["id"] if existing else asaas.create_customer(
        customer_data["name"], customer_data["cpfCnpj"], customer_data["email"]
    )["id"]
    with db() as conn:
        conn.execute("UPDATE orders SET customer_id=? WHERE id=?", (cust_id, order["id"]))
    return cust_id

def luhn_check(card_number: str) -> bool:
    digits = [int(c) for c in card_number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0

_ip_attempts: dict[str, list[float]] = {}
def rate_limit_ip(ip: str, max_attempts: int = 5, window_seconds: int = 600) -> bool:
    t = time.time()
    history = [x for x in _ip_attempts.get(ip, []) if t - x < window_seconds]
    history.append(t)
    _ip_attempts[ip] = history
    return len(history) > max_attempts

def _claim_order_lock(order_id: str) -> None:
    with db() as conn:
        if not transition(conn, order_id, "CREATING", {"PENDING"}):
            raise RuntimeError("Pedido já em processamento")

def _release_order_lock(order_id: str) -> None:
    with db() as conn:
        transition(conn, order_id, "PENDING", {"CREATING"})

# -------------------------------------------------------------
# POSSIBILIDADE 1: API DIRETA COM 3DS (CARTÃO + PIX)
# -------------------------------------------------------------
def pay_via_api_card_3ds(order_id: str, customer_data: dict, card_data: dict,
                         holder_info: dict, device_data: dict, remote_ip: str) -> dict:
    order = get_order(order_id)
    if not order or order["status"] != "PENDING":
        raise ValueError("Pedido inexistente ou em estado inválido")
    if not luhn_check(card_data["number"]):
        raise ValueError("Número de cartão inválido pelo algoritmo de Luhn")
    if rate_limit_ip(remote_ip):
        raise PermissionError("Muitas tentativas de pagamento para este IP")

    _claim_order_lock(order_id)
    try:
        cust_id = _get_or_create_customer(order, customer_data)
        payload = {
            "customer": cust_id,
            "billingType": "CREDIT_CARD",
            "value": float(round(Decimal(order["total"]), 2)),
            "dueDate": date.today().isoformat(),
            "externalReference": order_id,
            "creditCard": {
                "holderName": card_data["holderName"],
                "number": card_data["number"],
                "expiryMonth": card_data["expiryMonth"],
                "expiryYear": card_data["expiryYear"],
                "ccv": card_data["ccv"],
            },
            "creditCardHolderInfo": holder_info,
            "remoteIp": remote_ip,  # IP capturado no backend
            "threeDSecure": {"callbackUrl": f"{PUBLIC_BASE_URL}/pedido/{order_id}/processando"},
            "deviceInfo": {**device_data, "remoteIp": remote_ip},
        }
        
        # Criação resiliente com busca prévia por externalReference em caso de timeout
        remote_payment = None
        for attempt in range(4):
            try:
                remote_payment = asaas.create_payment(payload)
                break
            except asaas.AsaasUnavailableError:
                existing = asaas.find_payment_by_reference(order_id)
                if existing:
                    remote_payment = existing
                    break
                time.sleep(2 ** attempt)
        if not remote_payment:
            raise asaas.AsaasUnavailableError("Falha de comunicação com o Asaas")

    except Exception:
        _release_order_lock(order_id)
        raise

    with db() as conn:
        conn.execute(
            "UPDATE orders SET method='CARD_3DS', asaas_payment_id=? WHERE id=?",
            (remote_payment["id"], order_id),
        )
        transition(conn, order_id, "AWAITING_PAYMENT", {"CREATING"})

    # Avaliação do retorno de 3DS:
    # 1. Se status == CONFIRMED e challengeUrl == null: Fluxo Frictionless ou Data Only (aprovado sem desafio).
    # 2. Se challengeUrl presente: O portador deve ser redirecionado para validação do banco (ISSUER_CHALLENGE).
    return {
        "paymentId": remote_payment["id"],
        "status": remote_payment.get("status"),
        "challengeUrl": remote_payment.get("threeDSecureUrl") or remote_payment.get("threeDSecureChallengeUrl"),
    }

def pay_via_api_pix_direct(order_id: str, customer_data: dict) -> dict:
    order = get_order(order_id)
    if not order or order["status"] != "PENDING":
        raise ValueError("Pedido inexistente ou em estado inválido")
    _claim_order_lock(order_id)
    try:
        cust_id = _get_or_create_customer(order, customer_data)
        due_date = (date.today() + timedelta(days=1)).isoformat()
        remote_payment = asaas.create_payment({
            "customer": cust_id,
            "billingType": "PIX",
            "value": float(round(Decimal(order["total"]), 2)),
            "dueDate": due_date,
            "externalReference": order_id,
        })
        pix_info = asaas.get_pix_qrcode(remote_payment["id"])
    except Exception:
        _release_order_lock(order_id)
        raise

    with db() as conn:
        conn.execute(
            "UPDATE orders SET method='PIX_DIRECT', asaas_payment_id=? WHERE id=?",
            (remote_payment["id"], order_id),
        )
        transition(conn, order_id, "AWAITING_PAYMENT", {"CREATING"})

    return {
        "paymentId": remote_payment["id"],
        "encodedImage": pix_info["encodedImage"],
        "payload": pix_info["payload"],
        "expirationDate": pix_info["expirationDate"],
    }

# -------------------------------------------------------------
# POSSIBILIDADE 2: ASAAS CHECKOUT HOSPEDADO
# -------------------------------------------------------------
def pay_via_hosted_checkout(order_id: str) -> dict:
    """Cria uma sessão de checkout hospedada no Asaas com Pix e Cartão inclusos."""
    order = get_order(order_id)
    if not order or order["status"] != "PENDING":
        raise ValueError("Pedido inexistente ou em estado inválido")
    _claim_order_lock(order_id)
    try:
        checkout_session = asaas.create_checkout_session({
            "billingTypes": ["CREDIT_CARD", "PIX"],
            "chargeTypes": ["DETACHED"],
            "minutesToExpire": 30,
            "externalReference": order_id,
            "callback": {
                "successUrl": f"{PUBLIC_BASE_URL}/pedido/{order_id}/processando",
                "cancelUrl":  f"{PUBLIC_BASE_URL}/pedido/{order_id}/cancelado",
                "expiredUrl": f"{PUBLIC_BASE_URL}/pedido/{order_id}/expirado",
            },
            "items": [
                {
                    "name": f"Pedido {order_id[:8]}",
                    "quantity": 1,
                    "value": float(round(Decimal(order["total"]), 2)),
                    # imageBase64 é obrigatório na especificação OpenAPI de CheckoutSessionItemsDTO
                    "imageBase64": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
                }
            ],
        })
    except Exception:
        _release_order_lock(order_id)
        raise

    with db() as conn:
        conn.execute(
            "UPDATE orders SET method='HOSTED_CHECKOUT', asaas_checkout_id=? WHERE id=?",
            (checkout_session["id"], order_id),
        )
        transition(conn, order_id, "AWAITING_PAYMENT", {"CREATING"})

    # URL com fallback seguro conforme documentação de montagem de link
    checkout_id = checkout_session["id"]
    is_sandbox = "sandbox" in ASAAS_BASE_URL
    checkout_domain = "sandbox.asaas.com" if is_sandbox else "asaas.com"
    checkout_url = checkout_session.get("link") or f"https://{checkout_domain}/checkoutSession/show?id={checkout_id}"

    return {"checkoutUrl": checkout_url}

