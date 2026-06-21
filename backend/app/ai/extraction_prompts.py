"""Prompts for document classification and invoice extraction.

Used with gemma4:e4b (local Ollama) for confidential document processing.
"""

CLASSIFY_SYSTEM = """You are a document classification system for a manufacturing company.
Classify documents into exactly one category. Respond in JSON only."""

CLASSIFY_PROMPT = """Classify this document into one of these types:
- invoice (счёт, счёт-фактура)
- letter (деловое письмо)
- contract (договор, соглашение)
- drawing (чертёж, техническая документация)
- commercial_offer (коммерческое предложение, КП)
- act (акт выполненных работ)
- waybill (накладная, товарная накладная, ТОРГ-12)
- other (другое)

Document text:
---
{text}
---

Respond with JSON:
{{"type": "<type>", "confidence": <0.0-1.0>, "reasoning": "<brief explanation>"}}"""


EXTRACT_INVOICE_SYSTEM = """You are an invoice data extraction system for a Russian manufacturing company.
Extract ALL structured data from invoice text. All monetary values in the original currency.
Respond in valid JSON only. If a field is not found, use null. Never truncate line items."""

EXTRACT_INVOICE_PROMPT = """Extract ALL fields from this Russian invoice (счёт / счёт-фактура).

Document text:
---
{text}
---

=== STEP 1 — FIRST: Find the invoice title line ===
Before anything else, scan the ENTIRE text for a line like:
  "Счёт на оплату № KA-15203 от 4 октября 2024 г."
  "Счёт-фактура № 1019 от 19.11.2024"
  "Счёт № 42 от 01.02.2024"
This is the INVOICE TITLE — it gives invoice_number and invoice_date.
DO NOT confuse with "Сч. №" (abbreviated bank account number in the payment slip block).
Russian months: января=01 февраля=02 марта=03 апреля=04 мая=05 июня=06
                июля=07 августа=08 сентября=09 октября=10 ноября=11 декабря=12

=== STEP 2 — THEN: Russian Payment Order Format ===
Russian invoices contain a payment slip ("Образец заполнения платёжного поручения").
Read this section carefully to extract SUPPLIER bank details:

Layout in the payment slip:
  [BANK NAME] [CITY]      → supplier.bank_name
  БИК [digits]            → supplier.bank_bik
  Сч. № [20 digits]       → supplier.corr_account (корреспондентский счёт банка, starts with 301)
  Банк получателя
  ИНН [digits]            → supplier.inn
  КПП [digits]            → supplier.kpp
  Сч. № [20 digits]       → supplier.bank_account (расчётный счёт получателя, starts with 407)
  [SUPPLIER NAME]
  Получатель

The FIRST "Сч. №" after the bank name = corr_account (starts with 301...).
The SECOND "Сч. №" after ИНН/КПП = bank_account (расчётный счёт, starts with 407...).
БИК is always 9 digits. ИНН is 10 or 12 digits. КПП is 9 digits.

CRITICAL — NEVER use bank account numbers as monetary amounts:
Any number that starts with 301, 407, 408 and is 20 digits long is a bank account (расчётный счёт or корреспондентский счёт).
It is NOT a price, subtotal, tax or total amount. Monetary amounts are at most 10 digits before the decimal point.

CRITICAL — Invoice number vs bank account "Сч. №":
"Сч. №" in the payment slip = abbreviated "счёт №" = BANK ACCOUNT NUMBER (20 digits), NOT the invoice number.
The INVOICE NUMBER appears in the TITLE line like:
  "Счёт на оплату № KA-15203 от 4 октября 2024 г."   → invoice_number="KA-15203"
  "Счёт-фактура № 1019 от 19.11.2024"                 → invoice_number="1019"
  "Счёт № 42 от 01.02.2024"                           → invoice_number="42"
This title line usually appears AFTER the payment slip block. Scan the FULL text for it.

CRITICAL — Russian month names in dates (convert to YYYY-MM-DD):
января=01, февраля=02, марта=03, апреля=04, мая=05, июня=06,
июля=07, августа=08, сентября=09, октября=10, ноября=11, декабря=12
Examples: "4 октября 2024 г." → "2024-10-04";  "19 ноября 2024 г." → "2024-11-19"

CRITICAL: The line "ИНН XXXXXXXXXX" that appears AFTER "Банк получателя" label
and BEFORE the supplier company name IS the supplier's INN — extract it as supplier.inn.
Example: "Банк получателя\nИНН 2222852056\nКПП 222201001\n...\nООО Компания\nПолучатель"
→ supplier.inn = "2222852056", supplier.kpp = "222201001"

The buyer (Плательщик/Покупатель) is a DIFFERENT entity with its own ИНН/КПП.
Do NOT mix up supplier INN/KPP with buyer INN/KPP.

=== OUTPUT JSON STRUCTURE ===
{{
  "invoice_number": "<string or null>",
  "invoice_date": "<YYYY-MM-DD or null>",
  "due_date": "<YYYY-MM-DD or null>",
  "validity_date": "<YYYY-MM-DD — срок действия счёта / резерв до, or null>",
  "currency": "<3-letter code, default RUB>",
  "payment_id": "<идентификатор платежа / payment reference string or null>",
  "special_marks": "<delivery notes, special conditions, payment terms, any free text after line items, or null>",

  "supplier": {{
    "name": "<full legal name or null>",
    "inn": "<10 or 12 digits or null>",
    "kpp": "<9 digits or null>",
    "address": "<full address or null>",
    "phone": "<phone number(s) or null>",
    "email": "<email address or null>",
    "bank_name": "<full bank name or null>",
    "bank_bik": "<9 digits or null>",
    "bank_account": "<20 digits, расчётный счёт starts with 407, or null>",
    "corr_account": "<20 digits, корреспондентский счёт starts with 301, or null>"
  }},

  "buyer": {{
    "name": "<full legal name or null>",
    "inn": "<10 or 12 digits or null>",
    "kpp": "<9 digits or null>",
    "address": "<full address or null>"
  }},

  "lines": [
    {{
      "line_number": <int>,
      "sku": "<артикул/код товара or null>",
      "description": "<товар/услуга name, without the SKU if SKU is separate>",
      "quantity": <float or null>,
      "unit": "<ед. изм. or null>",
      "unit_price": <float or null>,
      "amount": <float without НДС, or null>,
      "tax_rate": <0.2 for НДС 20%, 0.1 for 10%, 0.0 for without НДС, or null>,
      "tax_amount": <float НДС amount or null>,
      "weight": <float кг per line or null>
    }}
  ],

  "subtotal": <float — see VAT rules below, or null>,
  "tax_amount": <float total НДС / В т.ч. НДС, or null>,
  "total_amount": <float Всего к оплате / Итого с НДС, or null>,

  "field_confidences": {{
    "invoice_number": <0.0-1.0>,
    "invoice_date": <0.0-1.0>,
    "supplier_inn": <0.0-1.0>,
    "supplier_bank": <0.0-1.0>,
    "buyer_inn": <0.0-1.0>,
    "lines": <0.0-1.0>,
    "total_amount": <0.0-1.0>
  }}
}}

=== PARSING RULES ===
- Amount format: "1 500,00" or "1500.00" → 1500.0; "29 920" → 29920.0
- invoice_number: look for "Счёт на оплату №", "Счёт-фактура №", "Счёт №", or just "№" near document header
- invoice_date: look for "от DD.MM.YYYY", "от D месяца YYYY г." near the invoice number
- Extract ALL line items without exception — scan the ENTIRE document, never stop early

=== LINE ITEM AMOUNTS — DISCOUNT INVOICES ===
Some Russian invoices have a "Скидка" (discount) column. The table may be:
  № | Товар | Кол-во | Цена | Сумма без скидки | Скидка | Ставка НДС | Сумма НДС | Сумма
In this case:
- "amount" for each line = the FINAL "Сумма" column (post-discount), NOT "Сумма без скидки"
- The "Итого" row also has multiple columns — use the final "Сумма" total, NOT the "Сумма без скидки" total

=== VAT CONVENTIONS ===
Two Russian invoice formats exist:
1. "НДС сверху" (VAT added on top):
   "Итого без НДС: X" → subtotal = X
   "НДС Y%: T" → tax_amount = T
   "Итого с НДС: Z" → total_amount = Z  (Z = X + T)

2. "В т.ч. НДС" (VAT included / gross pricing):
   "Итого: X" → subtotal = X (gross amount, tax is inside)
   "В т.ч. НДС Y%: T" → tax_amount = T
   "Всего к оплате: X" → total_amount = X  (same as subtotal)

When the invoice shows only ONE total row like "Итого: 27 340,00" and "В т.ч. НДС: 4 556,67":
  → subtotal = 27340.0, tax_amount = 4556.67, total_amount = 27340.0

=== OTHER RULES ===
- "Резерв до" or "Срок действия счёта" → validity_date
- If article/SKU is in a separate column ("Артикул", "Код"), put it in "sku"
- Supplier phone may appear in address string after "тел" or "тел/факс"
- Supplier email appears after "E-mail:" or "email:"
- payment_id: look for "Идентификатор платежа", "Назначение платежа", invoice payment reference code"""


EXTRACT_INVOICE_VISION_PROMPT = """You are extracting structured data from a Russian invoice image.

LOOK AT THE FULL IMAGE carefully before answering.

=== STEP 1: Find the Invoice Title ===
Scan the ENTIRE image for a title line like:
  "Счёт на оплату № KA-15203 от 4 октября 2024 г."
  "Счёт-фактура № 1019 от 19.11.2024"
  "Счёт № 42 от 01.02.2024"
→ invoice_number = the alphanumeric after "№"
→ invoice_date = the date after "от" in YYYY-MM-DD
Russian months: января=01, февраля=02, марта=03, апреля=04, мая=05, июня=06,
                июля=07, августа=08, сентября=09, октября=10, ноября=11, декабря=12
DO NOT confuse "Сч. №" (bank account, 20 digits) with the invoice number.

=== STEP 2: Read the Table ===
If the table has: № | Товар | Кол-во | Цена | Сумма без скидки | Скидка | Сумма НДС | Сумма
→ "amount" per line = the LAST "Сумма" column (post-discount), NOT "Сумма без скидки"

=== STEP 3: Find Totals ===
- "Итого без НДС" → subtotal
- "НДС 20%" or "В т.ч. НДС" → tax_amount
- "Итого с НДС" or "Всего к оплате" → total_amount
If only "Итого: X" and "В т.ч. НДС: Y" → subtotal=X, total_amount=X, tax_amount=Y

=== Bank Accounts ≠ Money ===
20-digit numbers starting with 301, 407, 408 = bank account numbers, NOT monetary amounts.

Return ONLY valid JSON:
{{
  "invoice_number": "<string or null>",
  "invoice_date": "<YYYY-MM-DD or null>",
  "currency": "RUB",
  "supplier": {{
    "name": "<string or null>",
    "inn": "<10-12 digits or null>",
    "kpp": "<9 digits or null>",
    "bank_bik": "<9 digits or null>",
    "bank_account": "<20 digits or null>",
    "corr_account": "<20 digits or null>"
  }},
  "buyer": {{"name": "<string or null>", "inn": "<10-12 digits or null>"}},
  "lines": [
    {{
      "line_number": 1,
      "description": "<string>",
      "quantity": 0.0,
      "unit": "<string>",
      "unit_price": 0.0,
      "amount": 0.0,
      "tax_rate": 0.2,
      "tax_amount": 0.0
    }}
  ],
  "subtotal": 0.0,
  "tax_amount": 0.0,
  "total_amount": 0.0,
  "field_confidences": {{
    "invoice_number": 0.9,
    "invoice_date": 0.9,
    "supplier_inn": 0.9,
    "supplier_bank": 0.9,
    "buyer_inn": 0.9,
    "lines": 0.9,
    "total_amount": 0.9
  }}
}}"""


SUMMARIZE_SYSTEM = """You are a document summarization system for a Russian manufacturing company.
Produce concise summaries in Russian. Respond in JSON only."""

SUMMARIZE_PROMPT = """Summarize this document in Russian. Focus on:
- Document type and purpose
- Key parties involved
- Important amounts, dates, deadlines
- Action items or decisions needed

Document text:
---
{text}
---

Respond with JSON:
{{
  "summary": "<2-3 sentence summary in Russian>",
  "key_facts": ["<fact1>", "<fact2>", ...],
  "action_required": "<action description or null>",
  "urgency": "<low|medium|high>"
}}"""


VALIDATE_INVOICE_SYSTEM = """You are an invoice validation system. Check the extracted invoice data
for errors and inconsistencies. Respond in JSON only."""

VALIDATE_INVOICE_PROMPT = """Validate this extracted invoice data:

{extracted_json}

Check:
1. Arithmetic: sum of line amounts should equal subtotal
2. Tax: tax_amount should be consistent with tax_rate and subtotal
3. Total: subtotal + tax_amount should equal total_amount
4. Line items: quantity × unit_price should equal amount for each line
5. Format: INN should be 10 or 12 digits, KPP should be 9 digits
6. Dates: invoice_date should be valid and not in the future

Respond with JSON:
{{
  "is_valid": <bool>,
  "errors": [
    {{
      "field": "<field_name>",
      "error_type": "<arithmetic|format|consistency|missing>",
      "message": "<description>",
      "expected": "<expected value or null>",
      "actual": "<actual value or null>",
      "severity": "<error|warning>"
    }}
  ],
  "corrected_fields": {{
    "<field_name>": "<corrected_value>"
  }},
  "overall_confidence": <0.0-1.0>
}}"""


# ── Generic (non-invoice) document field extraction ──────────────────────────
# Used for letters, contracts, acts, waybills and commercial offers. Produces a
# flat, editable field list stored as ExtractionField rows (same review UI as
# invoices). Field hints are per document type; the model may add relevant
# fields it finds and omit absent ones (value=null).

EXTRACT_GENERIC_SYSTEM = """You are a document data-extraction system for a Russian manufacturing company.
Extract structured, verifiable fields from the document text. Respond in valid JSON only.
Use null for fields that are not present. Do not invent values. Keep field names in snake_case (English)."""

GENERIC_FIELD_HINTS: dict[str, str] = {
    "letter": (
        "sender, recipient, letter_date, outgoing_number, subject, "
        "reference_number, key_request, deadline, signatory"
    ),
    "contract": (
        "contract_number, contract_date, party_supplier, party_buyer, subject, "
        "total_amount, currency, valid_from, valid_until, payment_terms, signatories"
    ),
    "act": (
        "act_number, act_date, party_executor, party_customer, subject, "
        "total_amount, currency, period, basis_document"
    ),
    "waybill": (
        "waybill_number, waybill_date, shipper, consignee, carrier, "
        "total_quantity, total_amount, currency, vehicle"
    ),
    "commercial_offer": (
        "offer_number, offer_date, supplier, buyer, subject, "
        "total_amount, currency, valid_until, delivery_terms, payment_terms"
    ),
}

EXTRACT_GENERIC_PROMPT = """Extract structured fields from this Russian document of type "{doc_type}".

Document text:
---
{text}
---

Suggested fields to look for (extract those present, add other clearly relevant ones):
{field_hints}

Return JSON in EXACTLY this shape:
{{
  "summary": "<one-sentence Russian summary>",
  "fields": [
    {{"name": "<snake_case_field>", "value": "<string value or null>", "confidence": <0.0-1.0>}}
  ]
}}"""
