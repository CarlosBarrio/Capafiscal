INVOICE_WORDS = [
    "factura",
    "invoice",
    "bill",
    "receipt",
    "recibo"
]

BODY_WORDS = [
    "adjuntamos factura",
    "invoice attached",
    "iva",
    "vat",
    "importe",
    "total"
]


KNOWN_SENDERS = [
    "repsol",
    "endesa",
    "iberdrola",
    "amazon",
    "vodafone",
    "orange",
    "movistar",
    "adobe",
    "microsoft"
]


def score_email(
    sender,
    subject,
    body,
    attachments
):

    score = 0

    reasons = []

    subject_lower = subject.lower()
    body_lower = body.lower()

    for file in attachments:

        file_lower = file.lower()

        if file_lower.endswith(".pdf"):

            score += 20

            reasons.append(
                "adjunto pdf"
            )

        for word in INVOICE_WORDS:

            if word in file_lower:

                score += 20

                reasons.append(
                    f"adjunto contiene {word}"
                )

                break

    for word in INVOICE_WORDS:

        if word in subject_lower:

            score += 25

            reasons.append(
                f"asunto contiene {word}"
            )

            break

    for word in BODY_WORDS:

        if word in body_lower:

            score += 5

            reasons.append(
                f"texto contiene {word}"
            )

    for sender_name in KNOWN_SENDERS:

        if sender_name in sender.lower():

            score += 20

            reasons.append(
                f"remitente conocido {sender_name}"
            )

            break

    score = min(score, 100)

    return {
        "score": score,
        "is_invoice": score >= 60,
        "reasons": reasons
    }