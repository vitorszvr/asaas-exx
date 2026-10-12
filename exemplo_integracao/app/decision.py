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

    # 0. STATUS DA COBRANÇA EM CHARGEBACK (enum de status em "Recuperar uma única cobrança").
    # AWAITING_CHARGEBACK_REVERSAL = disputa vencida, aguardando repasse da adquirente:
    # o pedido segue suspenso até chegar CONFIRMED/RECEIVED.
    if status in {"CHARGEBACK_REQUESTED", "CHARGEBACK_DISPUTE", "AWAITING_CHARGEBACK_REVERSAL"}:
        return "DISPUTED"

    # 1. TRATAMENTO DE CHARGEBACK CONFORME A DOCUMENTAÇÃO OFICIAL (guia "Chargeback"):
    # chargeback.status: REQUESTED | IN_DISPUTE | DISPUTE_LOST | REVERSED | DONE
    chargeback_info = remote_payment.get("chargeback")
    if chargeback_info:
        cb_status = chargeback_info.get("status")
        # Contestação aberta ou em disputa: suspende acesso, permitindo defesa
        if cb_status in {"REQUESTED", "IN_DISPUTE"}:
            return "DISPUTED"
        # Disputa perdida: o valor é devolvido ao portador
        if cb_status == "DISPUTE_LOST":
            return "REVERSED"
        # REVERSED (chargeback revertido a favor do lojista) e DONE ("Processo concluído",
        # sem indicar o resultado) seguem o fluxo normal abaixo: o status da cobrança decide.

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

