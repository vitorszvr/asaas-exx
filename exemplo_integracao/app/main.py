import hmac
import json
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from . import orders
from .config import ASAAS_WEBHOOK_TOKEN as WEBHOOK_TOKEN
from .db import db, init_db, now
from .worker import process_pending_webhooks

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Gateway Asaas Antifraude", lifespan=lifespan)

class OrderItemDTO(BaseModel):
    sku: str
    quantity: int = Field(..., ge=1, le=10)

class CustomerDTO(BaseModel):
    name: str
    cpfCnpj: str
    email: str

class CardDTO(BaseModel):
    holderName: str
    number: str
    expiryMonth: str
    expiryYear: str
    ccv: str

class HolderInfoDTO(BaseModel):
    name: str
    email: str
    cpfCnpj: str
    postalCode: str
    addressNumber: str
    phone: str

class DeviceDTO(BaseModel):
    # Campos de deviceInfo conforme docs.asaas.com/docs/criar-cobrança-com-3ds.
    # remoteIp NÃO é aceito do cliente: é obtido pelo backend.
    userAgent: str
    colorDepth: int | None = None
    screenHeight: int | None = None
    screenWidth: int | None = None
    timeZoneOffset: float | None = None  # em HORAS (ex.: 3 para UTC-3)
    language: str | None = None

class PayCard3DSDTO(BaseModel):
    customer: CustomerDTO
    card: CardDTO
    holderInfo: HolderInfoDTO
    device: DeviceDTO

class PayPixDTO(BaseModel):
    customer: CustomerDTO

# --- Rotas de Negócio ---
@app.post("/api/orders", status_code=201)
def endpoint_create_order(items: list[OrderItemDTO]):
    try:
        return orders.create_order([i.model_dump() for i in items])
    except ValueError as exc:
        raise HTTPException(400, str(exc))

# --- Endpoints da Possibilidade 1 (API Direta) ---
@app.post("/api/orders/{order_id}/pay/card-3ds")
def endpoint_pay_card_3ds(order_id: str, body: PayCard3DSDTO, request: Request):
    remote_ip = request.client.host if request.client else "127.0.0.1"
    try:
        return orders.pay_via_api_card_3ds(
            order_id=order_id,
            customer_data=body.customer.model_dump(),
            card_data=body.card.model_dump(),
            holder_info=body.holderInfo.model_dump(),
            device_data=body.device.model_dump(),
            remote_ip=remote_ip,
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc))

@app.post("/api/orders/{order_id}/pay/pix-direct")
def endpoint_pay_pix_direct(order_id: str, body: PayPixDTO):
    try:
        return orders.pay_via_api_pix_direct(order_id, body.customer.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))

# --- Endpoint da Possibilidade 2 (Checkout Hospedado) ---
@app.post("/api/orders/{order_id}/pay/hosted-checkout")
def endpoint_pay_hosted_checkout(order_id: str):
    return orders.pay_via_hosted_checkout(order_id)

# --- Status somente-leitura para a tela de retorno do cliente ---
@app.get("/api/orders/{order_id}/status")
def endpoint_get_status(order_id: str):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404, "Pedido não encontrado")
    return {"status": order["status"]}

# --- Webhook Unificado: Recebe tanto eventos de cobrança quanto de checkout ---
@app.post("/webhooks/asaas")
async def endpoint_webhook(request: Request, bg: BackgroundTasks):
    # 1. Validação do token de autenticação em tempo constante (rejeita se não configurado)
    token = request.headers.get("asaas-access-token", "")
    if not WEBHOOK_TOKEN or not hmac.compare_digest(token, WEBHOOK_TOKEN):
        raise HTTPException(401, "Token de webhook inválido ou não configurado")

    # 2. Persistência imediata com deduplicação por chave única
    payload = await request.json()
    event_id = payload.get("id")
    event_name = payload.get("event")

    if event_id and event_name:
        resource_id = (payload.get("payment") or payload.get("checkout") or {}).get("id")
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO webhook_events (id, event, resource_id, payload, state, received_at) "
                "VALUES (?, ?, ?, ?, 'RECEBIDO', ?)",
                (event_id, event_name, resource_id, json.dumps(payload), now()),
            )
        # 3. Disparo do processamento assíncrono em segundo plano
        bg.add_task(process_pending_webhooks)

    # 4. Resposta HTTP 200 rápida para não pausar a fila do Asaas
    return {"status": "received"}

