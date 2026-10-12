# Exemplo de Integração Antifraude Asaas (Python / FastAPI)

Este diretório contém a implementação de referência executável da arquitetura proposta em [`apresentacao_solucao_asaas.md`](../apresentacao_solucao_asaas.md) e [`resposta.md`](../resposta.md).

O projeto implementa duas alternativas completas de pagamento integradas ao Asaas:
1. **Possibilidade 1:** API Direta com Sistema 3DS (`POST /v3/payments`) para Cartão de Crédito e Pix Dinâmico;
2. **Possibilidade 2:** Asaas Checkout Hospedado (`POST /v3/checkouts`) com redução de escopo regulatório de PCI-DSS (SAQ A).

Ambas as vias neutralizam completamente os golpes de **cartão clonado**, **comprovante falso de Pix**, **confirmação forjada por URL de retorno** e **adulteração de preços no front-end**.

---

## 1. Estrutura do Projeto

```text
exemplo_integracao/
├── app/
│   ├── __init__.py
│   ├── config.py       # Configurações de ambiente e catálogo seguro de preços
│   ├── db.py           # SQLite com transições atômicas e campos de chargeback
│   ├── asaas.py        # Cliente HTTP (httpx com timeout de 60s e retries)
│   ├── orders.py       # Domínio: pedidos, luhn, 3DS e checkout hospedado
│   ├── decision.py     # Validação cruzada e máquina de estados de chargeback
│   ├── worker.py       # Processador assíncrono de eventos de Webhook
│   └── main.py         # Endpoints FastAPI e Webhook blindado
├── tests/
│   ├── __init__.py
│   └── test_security.py # Testes automatizados contra vetores de ataque (pytest)
├── .env.example        # Modelo de variáveis de ambiente
├── requirements.txt    # Dependências mínimas
└── README.md           # Este guia de execução
```

---

## 2. Pré-requisitos e Instalação

Requer **Python 3.11+**.

1. Crie e ative um ambiente virtual:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Instale as dependências:
```bash
pip install -r requirements.txt
```

3. Configure as variáveis de ambiente:
```bash
cp .env.example .env
```

---

## 3. Execução dos Testes Automatizados

A suíte de testes de segurança simula todos os vetores de ataque contra o sistema, bem como os fluxos de chargeback e checkout:

```bash
pytest tests/test_security.py -v
```

### O que é testado:
- **Ataque 1:** Rejeição imediata de Webhooks sem token com status `HTTP 401`.
- **Ataque 2:** Tentativa de burlar pagamento via URLs de retorno (`/api/orders/{id}/status` permanece `PENDING`).
- **Ataque 3:** Tentativa de enviar preços adulterados no payload da requisição (o valor é recalculado pelo backend).
- **Validação de Cartão:** Validação da numeração de cartão pelo Algoritmo de Luhn antes de chamar o gateway.
- **Ciclo de Chargeback:** Evento `PAYMENT_CHARGEBACK_REQUESTED` com objeto aninhado suspende o pedido para `DISPUTED`, grava a data limite de defesa (`deadlineToSendDisputeDocuments`) e emite alerta com a URL de contestação (`POST /v3/chargebacks/{id}/dispute`).
- **Aprovação de Checkout:** Evento `CHECKOUT_PAID` com conferência de assinatura e sessão libera o pedido para `PAID`.

---

## 4. Executando o Servidor Localmente

Inicie o servidor de desenvolvimento via Uvicorn:

```bash
uvicorn app.main:app --reload --port 8000
```

A documentação interativa da API estará disponível em:
- Swagger UI: `http://localhost:8000/docs`
- Redoc: `http://localhost:8000/redoc`

---

## 5. Homologação Prática no Sandbox do Asaas

Para testar transações reais contra o ambiente de [Sandbox do Asaas](https://docs.asaas.com/docs/sandbox):

1. Obtenha sua API Key no painel Sandbox em *Configurações da Conta > Integrações*;
2. Preencha `ASAAS_API_KEY` e `ASAAS_WEBHOOK_TOKEN` no arquivo `.env`;
3. **Simulação de Recusa Oficial (Cartões de Teste do Asaas):**
   - **Mastercard:** `5184019740373151`
   - **Visa:** `4916561358240741`
   - *Comportamento esperado:* A API responde `HTTP 400` (`invalid_creditCard`), nenhuma cobrança indevida é persistida e o pedido continua `PENDING`.
4. **Simulação de Aprovação Limpa:**
   - Utilize qualquer número válido gerado por algoritmo de Luhn com data de expiração futura e CVV `123`.
   - *Comportamento esperado:* O status só transiciona para `PAID` quando o Asaas entrega o Webhook oficial na rota `/webhooks/asaas`.

