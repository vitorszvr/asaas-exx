from decimal import Decimal

class SecurityDivergence(Exception):
    """Detecta divergência entre o pedido interno e a resposta oficial do Asaas."""

def decide_payment_status(order, remote_payment: dict) -> str | None:
    if remote_payment.get("externalReference") != order["id"]:
        raise SecurityDivergence("externalReference não corresponde ao pedido")
    
    total = Decimal(order["total"])
    if Decimal(str(remote_payment.get("value"))) != total:
        raise SecurityDivergence("Valor registrado no Asaas diverge do total do pedido")

    status = remote_payment.get("status")
    
    # 1. TRATAMENTO DE CHARGEBACK CONFORME A DOCUMENTAÇÃO OFICIAL:
    chargeback_info = remote_payment.get("chargeback")
    if chargeback_info:
        cb_status = chargeback_info.get("status")
        # Disputa aberta: suspende acesso temporariamente, permitindo contestação
        if cb_status in {"REQUESTED", "IN_DISPUTE", "AWAITING_CHARGEBACK_REVERSAL"}:
            return "DISPUTED"
        # Disputa perdida ou finalizada: estorno definitivo
        if cb_status in {"DISPUTE_LOST", "REVERSED", "DONE"}:
            return "REVERSED"

    # 2. TRATAMENTO DE ESTORNOS:
    refunds = remote_payment.get("refunds") or []
    # Apenas status DONE representa valor efetivamente devolvido
    refunded_done = sum((Decimal(str(r["value"])) for r in refunds if r.get("status") == "DONE"), Decimal("0"))

    if status == "REFUNDED" or refunded_done >= total:
        return "REVERSED"
    if 0 < refunded_done < total:
        return "PARTIALLY_REFUNDED"
    
    # 3. CONFIRMAÇÃO DE PAGAMENTO:
    if order["method"] in {"CARD_3DS"} and status in {"CONFIRMED", "RECEIVED"}:
        return "PAID"
    if order["method"] in {"PIX_DIRECT"} and status == "RECEIVED":
        return "PAID"

    return None

