"""Built-in email templates seeded on application startup."""

from __future__ import annotations

BUILTIN_TEMPLATES = [
    {
        "slug": "payment_reminder",
        "name": "Напоминание об оплате",
        "category": "payment",
        "language": "ru",
        "subject": "Напоминание: счёт №{invoice_number} от {invoice_date}",
        "body_html": """\
<p>Добрый день, {contact_name}!</p>
<p>Направляем вам напоминание о том, что счёт №<strong>{invoice_number}</strong>
от {invoice_date} на сумму <strong>{total_amount} {currency}</strong>
до настоящего времени не оплачен.</p>
<p>Просим произвести оплату в ближайшее время. При возникновении вопросов,
пожалуйста, свяжитесь с нами.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день, {contact_name}!\n\n"
            "Направляем вам напоминание о том, что счёт №{invoice_number} "
            "от {invoice_date} на сумму {total_amount} {currency} "
            "до настоящего времени не оплачен.\n\n"
            "Просим произвести оплату в ближайшее время.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "invoice_number", "invoice_date",
            "total_amount", "currency", "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "overdue_payment",
        "name": "Просроченный платёж (строгое напоминание)",
        "category": "payment",
        "language": "ru",
        "subject": "Требование об оплате просроченного счёта №{invoice_number}",
        "body_html": """\
<p>Уважаемый(ая) {contact_name}!</p>
<p>Обращаем ваше серьёзное внимание на то, что оплата по счёту
№<strong>{invoice_number}</strong> от {invoice_date} на сумму
<strong>{total_amount} {currency}</strong> просрочена на <strong>{overdue_days} дн.</strong></p>
<p>Просим незамедлительно произвести оплату. В случае непоступления
платежа в течение 3 рабочих дней мы будем вынуждены принять
соответствующие меры.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Уважаемый(ая) {contact_name}!\n\n"
            "Обращаем ваше серьёзное внимание на то, что оплата по счёту "
            "№{invoice_number} от {invoice_date} на сумму {total_amount} {currency} "
            "просрочена на {overdue_days} дн.\n\n"
            "Просим незамедлительно произвести оплату.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "invoice_number", "invoice_date",
            "total_amount", "currency", "overdue_days", "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "payment_received",
        "name": "Уведомление об оплате",
        "category": "payment",
        "language": "ru",
        "subject": "Оплата по счёту №{invoice_number} получена",
        "body_html": """\
<p>Добрый день, {contact_name}!</p>
<p>Уведомляем вас о том, что оплата по счёту №<strong>{invoice_number}</strong>
от {invoice_date} на сумму <strong>{total_amount} {currency}</strong>
успешно получена {payment_date}.</p>
<p>Благодарим за своевременную оплату.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день, {contact_name}!\n\n"
            "Уведомляем вас о получении оплаты по счёту №{invoice_number} "
            "от {invoice_date} на сумму {total_amount} {currency}, "
            "поступившей {payment_date}.\n\n"
            "Благодарим за своевременную оплату.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "invoice_number", "invoice_date",
            "total_amount", "currency", "payment_date", "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "price_inquiry",
        "name": "Запрос коммерческого предложения",
        "category": "inquiry",
        "language": "ru",
        "subject": "Запрос коммерческого предложения на {product_name}",
        "body_html": """\
<p>Добрый день!</p>
<p>Просим вас направить коммерческое предложение на
<strong>{product_name}</strong> в количестве <strong>{quantity} {unit}</strong>.</p>
<p>Требуемые параметры:<br/>{specifications}</p>
<p>Срок поставки: {delivery_deadline}.<br/>
Условия оплаты: {payment_terms}.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день!\n\n"
            "Просим направить коммерческое предложение на {product_name} "
            "в количестве {quantity} {unit}.\n\n"
            "Требуемые параметры: {specifications}\n"
            "Срок поставки: {delivery_deadline}.\n"
            "Условия оплаты: {payment_terms}.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "product_name", "quantity", "unit", "specifications",
            "delivery_deadline", "payment_terms", "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "order_confirmation",
        "name": "Подтверждение заказа",
        "category": "confirmation",
        "language": "ru",
        "subject": "Подтверждение заказа №{order_number}",
        "body_html": """\
<p>Добрый день, {contact_name}!</p>
<p>Подтверждаем получение вашего заказа №<strong>{order_number}</strong>
от {order_date} на сумму <strong>{total_amount} {currency}</strong>.</p>
<p>Ожидаемый срок поставки: <strong>{delivery_date}</strong>.<br/>
Условия оплаты: {payment_terms}.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день, {contact_name}!\n\n"
            "Подтверждаем получение вашего заказа №{order_number} "
            "от {order_date} на сумму {total_amount} {currency}.\n\n"
            "Ожидаемый срок поставки: {delivery_date}.\n"
            "Условия оплаты: {payment_terms}.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "order_number", "order_date",
            "total_amount", "currency", "delivery_date", "payment_terms",
            "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "delivery_confirmation",
        "name": "Подтверждение получения товара",
        "category": "confirmation",
        "language": "ru",
        "subject": "Подтверждение получения по накладной №{waybill_number}",
        "body_html": """\
<p>Добрый день, {contact_name}!</p>
<p>Настоящим подтверждаем получение товара по накладной
№<strong>{waybill_number}</strong> от {delivery_date}.</p>
<p>Принято: <strong>{received_quantity} {unit}</strong> позиций
на сумму <strong>{total_amount} {currency}</strong>.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день, {contact_name}!\n\n"
            "Подтверждаем получение товара по накладной №{waybill_number} "
            "от {delivery_date}.\n"
            "Принято: {received_quantity} {unit} позиций "
            "на сумму {total_amount} {currency}.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "waybill_number", "delivery_date",
            "received_quantity", "unit", "total_amount", "currency",
            "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "document_request",
        "name": "Запрос документов",
        "category": "request",
        "language": "ru",
        "subject": "Запрос документов по сделке {deal_reference}",
        "body_html": """\
<p>Добрый день, {contact_name}!</p>
<p>Просим направить следующие документы по сделке
<strong>{deal_reference}</strong>:</p>
<ul>{document_list}</ul>
<p>Срок предоставления: <strong>{deadline}</strong>.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день, {contact_name}!\n\n"
            "Просим направить следующие документы по сделке {deal_reference}:\n"
            "{document_list}\n\n"
            "Срок предоставления: {deadline}.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "deal_reference", "document_list",
            "deadline", "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
    {
        "slug": "reconciliation_request",
        "name": "Запрос акта сверки",
        "category": "request",
        "language": "ru",
        "subject": "Запрос акта сверки взаимных расчётов за {period}",
        "body_html": """\
<p>Добрый день, {contact_name}!</p>
<p>В целях подтверждения взаимных расчётов просим направить
акт сверки за период <strong>{period}</strong>.</p>
<p>Наши данные по состоянию на {date}: задолженность составляет
<strong>{balance} {currency}</strong>.</p>
<p>С уважением,<br/>{sender_name}<br/>{company_name}</p>""",
        "body_text": (
            "Добрый день, {contact_name}!\n\n"
            "Просим направить акт сверки взаимных расчётов за период {period}.\n"
            "Наши данные на {date}: задолженность {balance} {currency}.\n\n"
            "С уважением,\n{sender_name}\n{company_name}"
        ),
        "variables": [
            "contact_name", "period", "date", "balance", "currency",
            "sender_name", "company_name",
        ],
        "is_builtin": True,
    },
]


async def seed_builtin_templates(db) -> None:
    """Insert built-in templates if they don't exist yet. Safe to call on every startup."""
    from sqlalchemy import select
    from app.db.models import EmailTemplateDB

    for tpl in BUILTIN_TEMPLATES:
        existing = await db.execute(
            select(EmailTemplateDB).where(EmailTemplateDB.slug == tpl["slug"])
        )
        if existing.scalar_one_or_none() is None:
            db.add(EmailTemplateDB(**tpl))

    await db.commit()
