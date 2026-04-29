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

=== CRITICAL: Russian Payment Order Format ===
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
  "notes": "<delivery notes, special conditions, any text after line items, or null>",

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

  "subtotal": <float Итого without НДС, or null>,
  "tax_amount": <float total НДС, or null>,
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
- "Итого" or "Итого без НДС" = subtotal (before tax)
- "В т.ч. НДС" or "В том числе НДС" = tax_amount
- "Всего к оплате" or "Итого с НДС" = total_amount
- "Резерв до" or "Срок действия счёта" → validity_date
- Extract ALL line items without exception — never stop at first few
- If article/SKU is in a separate column ("Артикул", "Код"), put it in "sku"
- Supplier phone may appear in address string after "тел" or "тел/факс"
- Supplier email appears after "E-mail:" or "email:"
- payment_id: look for "Идентификатор платежа", "Назначение платежа", invoice payment reference code"""


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
