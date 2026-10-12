import httpx
from .config import ASAAS_API_KEY, ASAAS_BASE_URL, USER_AGENT

# Timeout padrão de 60s conforme exigência da documentação oficial do Asaas para
# processamento de cartão de crédito (reduzindo risco de duplicações e falsos timeouts).
_client = httpx.Client(
    base_url=ASAAS_BASE_URL,
    timeout=httpx.Timeout(60.0, connect=10.0),
    headers={"access_token": ASAAS_API_KEY, "User-Agent": USER_AGENT},
)

class AsaasDefinitiveError(Exception):
    """HTTP 4xx: Resposta conclusiva de erro da API. NÃO fazer retry automático."""
    def __init__(self, status: int, errors: list[dict]):
        super().__init__(f"HTTP {status}: {errors}")
        self.status = status
        self.errors = errors

    @property
    def codes(self) -> set[str]:
        return {e.get("code", "") for e in self.errors}

class AsaasUnavailableError(Exception):
    """Timeout, queda de rede, 5xx ou 429: Resultado INCONCLUSIVO. Deve consultar antes de retentar."""

def _request(method: str, path: str, **kwargs) -> dict:
    try:
        r = _client.request(method, path, **kwargs)
    except httpx.TransportError as exc:
        raise AsaasUnavailableError(str(exc)) from exc
    if r.status_code >= 500 or r.status_code == 429:
        raise AsaasUnavailableError(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        try:
            errs = r.json().get("errors", [])
        except ValueError:
            errs = []
        raise AsaasDefinitiveError(r.status_code, errs)
    return r.json()

# Clientes
def find_customer_by_cpf_cnpj(cpf_cnpj: str) -> dict | None:
    data = _request("GET", "/v3/customers", params={"cpfCnpj": cpf_cnpj})
    return data["data"][0] if data.get("data") else None

def create_customer(name: str, cpf_cnpj: str, email: str) -> dict:
    return _request("POST", "/v3/customers", json={"name": name, "cpfCnpj": cpf_cnpj, "email": email})

# Possibilidade 1: API Direta (Cobrança)
def create_payment(payload: dict) -> dict:
    # A doc oficial do Asaas recomenda timeout mínimo de 60 segundos para processamento de cartão
    return _request("POST", "/v3/payments", json=payload, timeout=httpx.Timeout(60.0, connect=10.0))

def get_payment(payment_id: str) -> dict:
    return _request("GET", f"/v3/payments/{payment_id}")

def find_payment_by_reference(order_id: str) -> dict | None:
    data = _request("GET", "/v3/payments", params={"externalReference": order_id})
    return data["data"][0] if data.get("data") else None

def get_pix_qrcode(payment_id: str) -> dict:
    return _request("GET", f"/v3/payments/{payment_id}/pixQrCode")

# Possibilidade 2: Asaas Checkout
def create_checkout_session(payload: dict) -> dict:
    return _request("POST", "/v3/checkouts", json=payload)

