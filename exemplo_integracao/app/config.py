import os
from decimal import Decimal

# URLs e credenciais
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox")
ASAAS_BASE_URL = os.getenv(
    "ASAAS_BASE_URL",
    "https://api-sandbox.asaas.com" if ASAAS_ENV == "sandbox" else "https://api.asaas.com",
)
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "")
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://meusite.com.br")
DB_PATH = os.getenv("DB_PATH", "pedidos.db")
USER_AGENT = "MeuSistema/2.0 (integracao-segura-asaas)"

# Catálogo centralizado: O PREÇO NUNCA VEM DO CLIENTE
CATALOG: dict[str, dict] = {
    "CURSO-PYTHON": {"name": "Curso Python Avançado", "price": Decimal("199.90")},
    "LIVRO-FASTAPI": {"name": "Livro FastAPI Profissional", "price": Decimal("89.90")},
    "MENTORIA-DEV": {"name": "Sessão de Mentoria Técnica", "price": Decimal("350.00")},
}

