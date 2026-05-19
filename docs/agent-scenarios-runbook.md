# Agent Scenarios Runbook

This document describes the 8 main agent scenarios implemented in the system. Each section covers: trigger, expected flow, what can fail, and how to debug.

---

## Scenario 1: Email Triage

**Trigger**: New email arrives (IMAP poll or webhook) → `email.fetch_new` capability  
**Goal**: Classify email, extract linked invoice/document, create anomaly card if needed

**Flow**:
1. IMAP poller (Celery beat, every 60s) fetches new messages
2. Agent classifies: invoice, question, complaint, spam, delivery notice
3. If invoice: `documents.ingest` → `invoices.validate` → `anomalies.check_all`
4. If anomaly found: `anomalies.create_card` → notify in workspace
5. Email thread linked to invoice in DB

**What can fail**:
- IMAP credentials not configured → emails not fetched → `POST /api/mailboxes` to set up
- LLM classification times out → falls back to heuristic keyword rules
- Invoice already exists (duplicate) → AnomalyCard created with type `duplicate`

**Debug**:
```bash
docker logs infra-celery-worker-1 --since 1h | grep imap
curl http://localhost/api/mailboxes
```

---

## Scenario 2: Assisted Review

**Trigger**: User opens document in review mode (keyboard shortcut `R`)  
**Goal**: Agent provides extracted fields, flags anomalies, suggests corrections

**Flow**:
1. User presses `R` on document in Inbox
2. Frontend sends `POST /api/documents/{id}/classify` then `extract`
3. Agent streams: classification result → extraction fields → anomaly check
4. Side-by-side diff shown: agent extraction vs original document text
5. User corrects fields (approval gate for protected fields)

**What can fail**:
- OCR model offline → fallback to text extraction only
- Document in wrong status (`needs_review` required) → 400 from API

**Debug**:
```bash
curl http://localhost/api/documents/{id}  # check status
curl -X POST http://localhost/api/documents/{id}/classify
```

---

## Scenario 3: Draft Email

**Trigger**: User types "подготовь письмо поставщику X"  
**Goal**: Agent drafts reply email, awaits approval before sending

**Flow**:
1. Agent identifies supplier X via `suppliers.search`
2. Agent fetches relevant invoice context via `invoices.list`
3. Agent generates draft via `email.draft` capability
4. Draft stored with `status=draft`, shown to user
5. User reviews → approves → `email.send` **[GATE]**
6. After approval: Celery task sends via SMTP, marks `sent`

**What can fail**:
- SMTP not configured → send queued but never sent; check `EMAIL_SMTP_HOST` in .env
- Risk check flags critical issue → sending blocked; user sees flags in draft
- Approval expires (48h) → draft stays in `pending_approval` state

**Debug**:
```bash
curl http://localhost/api/email/drafts?status=draft
curl -X POST http://localhost/api/email/drafts/{id}/risk-check
docker logs infra-celery-worker-1 | grep email_sender
```

---

## Scenario 4: Compare КП (Commercial Proposals)

**Trigger**: User types "сравни предложения по заказу X" or opens Compare view  
**Goal**: Align line items from multiple supplier quotes, suggest best option

**Flow**:
1. Agent searches invoices for the order via `invoices.list` with filters
2. `analytics.compare_create` creates CompareSession with invoice IDs
3. `analytics.compare_align` normalizes line items (canonical names via NormalizationRule)
4. Alignment shown in compare view with sparklines for price history
5. User selects preferred supplier → `analytics.compare_decide` [GATE for email drafts]
6. Agent optionally drafts rejection letters to other suppliers

**What can fail**:
- Canonical item names not normalized → line items don't align
- Less than 2 invoices found → compare_create returns 422

**Debug**:
```bash
curl http://localhost/api/compare  # list sessions
curl -X POST http://localhost/api/compare/{id}/align
```

---

## Scenario 5: Proactive Follow-up

**Trigger**: Celery beat job (every 60 min) checks upcoming calendar events  
**Goal**: Send proactive reminder 3 days before payment/delivery deadline

**Flow**:
1. `proactive.check_upcoming` task runs hourly
2. Finds Reminders with `remind_at <= now` and `is_sent=False`
3. For each: agent generates follow-up draft via `email.draft`
4. Draft sent to workspace + notification
5. Reminder marked `is_sent=True`

**What can fail**:
- No reminders created → run `POST /api/calendar/extract-dates` on invoices
- Proactive task disabled → check Celery beat schedule in `celery_app.py`
- No upcoming deadlines in seed data → run `make seed`

**Debug**:
```bash
curl http://localhost/api/calendar/upcoming
docker exec infra-celery-beat-1 celery -A app.tasks.celery_app inspect scheduled
```

---

## Scenario 6: Anomaly Resolution

**Trigger**: User opens AnomalyCard ("Объяснить" button)  
**Goal**: Agent explains anomaly context, suggests resolution actions

**Flow**:
1. User clicks "🤖 Объяснить" on AnomalyCard
2. Frontend calls `GET /api/anomalies/{id}/explain`
3. Agent fetches invoice, supplier history, price data
4. Returns: explanation text + suggested_actions list
5. User acts on suggestions (e.g., approve with note, reject, contact supplier)

**What can fail**:
- LLM not available → explanation is rule-based fallback text
- Anomaly entity_id doesn't link to existing invoice → explanation is generic

**Debug**:
```bash
curl http://localhost/api/anomalies?status=open
curl http://localhost/api/anomalies/{id}/explain
```

---

## Scenario 7: NL Query + Action

**Trigger**: Command palette (Ctrl+K) or chat query  
**Goal**: Natural language → structured query → action chips

**Flow**:
1. User types "покажи неоплаченные счета за прошлый месяц" in palette or chat
2. `search.nl_to_query` converts NL → structured filters
3. `search.hybrid` executes across SQL + vector + graph
4. Results shown with action chips: "📋 Список", "✓ Утвердить все", "📁 Коллекция"
5. User selects action chip → executes corresponding capability call

**What can fail**:
- Qdrant not running → vector search falls back to SQL only
- LLM NL-to-query times out → raw keyword search used

**Debug**:
```bash
curl -X POST http://localhost/api/search/nl-to-query -H "Content-Type: application/json" \
  -d '{"query":"счета за прошлый месяц","entity_type":"invoice"}'
```

---

## Scenario 8: Smart Ingest

**Trigger**: User uploads file via chat or drag-drop  
**Goal**: Auto-classify, extract data, trigger anomaly check

**Flow**:
1. User sends file via WebSocket chat message with `attachment_doc_ids`
2. `POST /api/documents/ingest` called with `source_channel=chat`
3. Celery task `classify_document` triggered automatically (agent.py:193)
4. Classification result streamed to user in chat
5. If invoice: `extract_invoice` → `check_all_anomalies` → workspace block published

**What can fail**:
- File too large → 413 from upload endpoint (configurable `MAX_UPLOAD_SIZE`)
- Unknown file type → quarantined (FileExtensionAllowlist)
- Celery worker offline → task stays pending; check worker logs

**Debug**:
```bash
docker logs infra-celery-worker-1 | grep classify_document
curl http://localhost/api/documents?status=ingested
```

---

## Common Debugging Commands

```bash
# Check all containers
make ps

# View logs
make logs

# Run agent integration tests
make agent-test

# Run backend unit tests
make test

# Check Redis for workspace blocks
docker exec infra-redis-1 redis-cli keys "workspace:block*" | wc -l

# Check Celery beat schedule
docker exec infra-celery-beat-1 celery -A app.tasks.celery_app inspect scheduled

# Trigger manual anomaly check on an invoice
curl -X POST http://localhost/api/anomalies/check \
  -H "Content-Type: application/json" \
  -d '{"invoice_id":"<UUID>"}'
```
