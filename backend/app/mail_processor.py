import json
from pathlib import Path

from app.mail_scoring import score_email


BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"

EMAILS_FILE = DATA_DIR / "emails.json"


def load_emails():

    if not EMAILS_FILE.exists():
        return []

    with open(
        EMAILS_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def save_emails(data):

    with open(
        EMAILS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def classify_score(score):

    if score >= 80:
        return "Factura muy probable"

    if score >= 60:
        return "Posible factura"

    if score >= 40:
        return "Revisar"

    return "No parece factura"


def process_email(email):

    sender = email.get(
        "sender",
        ""
    )

    subject = email.get(
        "subject",
        ""
    )

    body = email.get(
        "body",
        ""
    )

    attachments = email.get(
        "attachments",
        []
    )

    scoring = score_email(
        sender=sender,
        subject=subject,
        body=body,
        attachments=attachments
    )

    score = scoring["score"]

    return {

        "email_id": email.get("id"),

        "sender": sender,

        "subject": subject,

        "attachments": attachments,

        "score": score,

        "classification":
            classify_score(score),

        "is_invoice":
            scoring["is_invoice"],

        "reasons":
            scoring["reasons"]
    }


def process_all_emails():

    emails = load_emails()

    results = []

    for email in emails:

        results.append(
            process_email(email)
        )

    return results


def get_invoice_candidates():

    emails = process_all_emails()

    return [

        e

        for e in emails

        if e["is_invoice"]

    ]


def get_top_candidates(limit=10):

    emails = process_all_emails()

    emails.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return emails[:limit]


def stats():

    processed = process_all_emails()

    invoice_count = len([
        e
        for e in processed
        if e["is_invoice"]
    ])

    review_count = len([
        e
        for e in processed
        if e["classification"] == "Revisar"
    ])

    ignored_count = len([
        e
        for e in processed
        if e["classification"] == "No parece factura"
    ])

    return {

        "total_emails":
            len(processed),

        "invoice_candidates":
            invoice_count,

        "needs_review":
            review_count,

        "ignored":
            ignored_count
    }