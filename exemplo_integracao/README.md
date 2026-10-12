# Exemplo de Integração Antifraude Asaas (Python / FastAPI)

Este diretório contém a implementação de referência executável da arquitetura proposta em [`apresentacao_solucao_asaas.md`](../apresentacao_solucao_asaas.md).

O projeto implementa duas alternativas completas de pagamento integradas ao Asaas:

1. **Possibilidade 1:** API Direta com Sistema 3DS (`POST /v3/payments`) para Cartão de Crédito e Pix Dinâmico;
2. **Possibilidade 2:** Asaas Checkout Hospedado (`POST /v3/checkouts`) com menor escopo de PCI-DSS (o SAQ aplicável deve ser definido com adquirente/QSA).

As duas vias mitigam **comprovante falso de Pix**, **confirmação forjada por URL de retorno** e **adulteração de preços no front-end**; o 3DS (Possibilidade 1) adiciona autenticação do portador contra **cartão clonado**.

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

- **Ataque 1:** Rejeição imediata de Webhooks sem token (`asaas-access-token`) com status `HTTP 401`.
- **Ataque 2:** Tentativa de burlar pagamento via URLs de retorno (`/api/orders/{id}/status` permanece `PENDING`).
- **Ataque 3:** Tentativa de enviar preços adulterados no payload da requisição (o valor é recalculado pelo backend a partir do catálogo).
- **Validação de Cartão:** Validação da numeração de cartão pelo Algoritmo de Luhn antes de chamar o gateway.
- **Ciclo de Chargeback:** Evento de chargeback suspende o pedido para `DISPUTED`, consulta `GET /v3/payments/{id}/chargeback` para gravar o motivo e o prazo limite de defesa (`deadlineToSendDisputeDocuments` para `POST /v3/chargebacks/{id}/dispute`), reverte para `PAID` quando o chargeback tem status `REVERSED` e estorna para `REVERSED` em `DISPUTE_LOST`.
- **Aprovação e Validação de Checkout:** Evento `CHECKOUT_PAID` confere status `PAID`, vínculo da sessão (`id` e `externalReference`) e soma dos itens (`value × quantity`), liberando o pedido para `PAID` (e bloqueando caso o valor divirja).

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

1. Obtenha sua API Key no painel Sandbox em _Configurações da Conta > Integrações_;
2. Preencha `ASAAS_API_KEY` e `ASAAS_WEBHOOK_TOKEN` no arquivo `.env`;
3. **Simulação de Recusa Oficial (Cartões de Teste do Asaas):**
   - **Mastercard:** `5184019740373151`
   - **Visa:** `4916561358240741`
   - _Comportamento esperado:_ a falha é tratada pela aplicação e o pedido não é liberado.
4. **Simulação de Aprovação:**
   - Utilize um número fictício válido (Luhn) com data de expiração futura.
   - _Comportamento esperado:_ o status só transiciona para `PAID` quando o Asaas entrega o Webhook oficial na rota `/webhooks/asaas` e a cobrança é reconsultada.
5. **3DS:** segundo a documentação, o 3DS não pode ser testado em sandbox (as transações são aprovadas automaticamente); a validação real exige produção com o 3DS habilitado pelo suporte do Asaas.
