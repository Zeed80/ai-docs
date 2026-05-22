# AiAgent Skills API Reference

*Auto-generated from FastAPI Pydantic schemas. Version 2. Total: 229 skills.*

> **Usage**: agent calls `POST /api/agent/cap/{capability}` with `{"action": "..."}`. See [ADR 001](adrs/001-capability-routing.md).

## Table of Contents

- [Anomalies (5)](#anomalies)
- [Approvals (3)](#approvals)
- [BOMs (7)](#boms)
- [Calendar (8)](#calendar)
- [Canvas (1)](#canvas)
- [Collections (7)](#collections)
- [Compare (КП) (6)](#compare-кп)
- [Dashboard (1)](#dashboard)
- [Documents (22)](#documents)
- [Email (11)](#email)
- [Email Templates (7)](#email-templates)
- [Graph (10)](#graph)
- [Invoices (11)](#invoices)
- [Mailboxes (6)](#mailboxes)
- [Memory (8)](#memory)
- [Normalization (11)](#normalization)
- [NTD / Technology (15)](#ntd--technology)
- [Payments (6)](#payments)
- [Procurement (9)](#procurement)
- [Quarantine (1)](#quarantine)
- [Search & NL (5)](#search--nl)
- [Suppliers (9)](#suppliers)
- [Tables & Export (10)](#tables--export)
- [Technology Cards (28)](#technology-cards)
- [Warehouse (15)](#warehouse)
- [Workspace (7)](#workspace)

## Anomalies

### `anomaly.check_all`

anomaly.check_all

**`POST /api/anomalies/check`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `invoice_id` | `string` |  | Invoice Id |

### `anomaly.create_card`

anomaly.create_card

**`POST /api/anomalies`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `anomaly_type` | `string` | ✓ | Anomaly Type |
| `description` | `string` |  | Description |
| `details` | `object` |  | Details |
| `entity_id` | `string` | ✓ | Entity Id |
| `entity_type` | `string` | ✓ | Entity Type |
| `severity` | `string` |  | Severity |
| `title` | `string` | ✓ | Title |

### `anomaly.explain`

anomaly.explain

**`GET /api/anomalies/{anomaly_id}/explain`**

### `anomaly.list`

anomaly.list

**`GET /api/anomalies`**

### `anomaly.resolve` ⛔ **approval gate**

anomaly.resolve

**`POST /api/anomalies/{anomaly_id}/resolve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `comment` | `string` |  | Comment |
| `resolution` | `string` | ✓ | Resolution |


## Approvals

### `approval.list_pending`

approval.list_pending

**`GET /api/approvals/pending`**

### `approval.request`

approval.request

**`POST /api/approvals`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action_type` | `any` | ✓ |  |
| `assigned_to` | `string` |  | Assigned To |
| `context` | `object` |  | Context |
| `entity_id` | `string` | ✓ | Entity Id |
| `entity_type` | `string` | ✓ | Entity Type |
| `expires_at` | `string` |  | Expires At |
| `requested_by` | `string` |  | Requested By |

### `approval.status`

approval.status

**`GET /api/approvals/{approval_id}`**


## BOMs

### `bom.approve` ⛔ **approval gate**

bom.approve

**`POST /api/boms/{bom_id}/approve`**

### `bom.create`

bom.create

**`POST /api/boms`**

### `bom.create_purchase_request` ⛔ **approval gate**

bom.create_purchase_request

**`POST /api/boms/{bom_id}/create-purchase-request`**

### `bom.get`

bom.get

**`GET /api/boms/{bom_id}`**

### `bom.list`

bom.list

**`GET /api/boms`**

### `bom.stock_check`

bom.stock_check

**`GET /api/boms/{bom_id}/stock-check`**

### `bom.update`

bom.update

**`PATCH /api/boms/{bom_id}`**


## Calendar

### `calendar.create_event`

calendar.create_event

**`POST /api/calendar/events`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | `string` |  | Entity Id |
| `entity_type` | `string` |  | Entity Type |
| `event_date` | `string` | ✓ | Event Date |
| `event_type` | `string` | ✓ | Event Type |
| `source` | `string` |  | Source |
| `title` | `string` | ✓ | Title |

### `calendar.create_reminder`

calendar.create_reminder

**`POST /api/calendar/reminders`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `calendar_event_id` | `string` |  | Calendar Event Id |
| `entity_id` | `string` | ✓ | Entity Id |
| `entity_type` | `string` | ✓ | Entity Type |
| `message` | `string` | ✓ | Message |
| `remind_at` | `string` | ✓ | Remind At |

### `calendar.extract_dates`

calendar.extract_dates

**`POST /api/calendar/extract-dates`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invoice_id` | `string` | ✓ | Invoice Id |

### `calendar.generate_followup`

calendar.generate_followup

**`POST /api/calendar/reminders/{reminder_id}/generate-followup`**

### `calendar.list_events`

calendar.list_events

**`GET /api/calendar/events`**

### `calendar.list_reminders`

calendar.list_reminders

**`GET /api/calendar/reminders`**

### `calendar.mark_sent`

calendar.mark_sent

**`POST /api/calendar/reminders/{reminder_id}/mark-sent`**

### `calendar.upcoming`

calendar.upcoming

**`GET /api/calendar/upcoming`**


## Canvas

### `canvas.publish`

canvas.publish

**`POST /api/canvas/publish`**


## Collections

### `collection.add`

collection.add

**`POST /api/collections/{collection_id}/items`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | `string` | ✓ | Entity Id |
| `entity_type` | `string` | ✓ | Entity Type |
| `note` | `string` |  | Note |

### `collection.create`

collection.create

**`POST /api/collections`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | `string` |  | Description |
| `name` | `string` | ✓ | Name |

### `collection.get`

collection.get

**`GET /api/collections/{collection_id}`**

### `collection.list`

collection.list

**`GET /api/collections`**

### `collection.search`

collection.search

**`GET /api/collections/{collection_id}/search`**

### `collection.summarize`

collection.summarize

**`POST /api/collections/{collection_id}/summarize`**

### `collection.timeline`

collection.timeline

**`GET /api/collections/{collection_id}/timeline`**


## Compare (КП)

### `compare.align`

compare.align

**`POST /api/compare/{session_id}/align`**

### `compare.create`

compare.create

**`POST /api/compare`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invoice_ids` | `array` | ✓ | Invoice Ids |
| `name` | `string` | ✓ | Name |

### `compare.decide` ⛔ **approval gate**

compare.decide

**`POST /api/compare/{session_id}/decide`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `chosen_supplier_id` | `string` | ✓ | Chosen Supplier Id |
| `reasoning` | `string` |  | Reasoning |

### `compare.get`

compare.get

**`GET /api/compare/{session_id}`**

### `compare.list`

compare.list

**`GET /api/compare`**

### `compare.summary`

compare.summary

**`GET /api/compare/{session_id}/summary`**


## Dashboard

### `dashboard.today`

dashboard.today

**`GET /api/dashboard/today`**


## Documents

### `doc.batch_classify`

doc.batch_classify

**`POST /api/documents/batch/classify`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `build_scope` | `string` |  | Build Scope |
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |

### `doc.batch_embeddings_reindex`

doc.batch_embeddings_reindex

**`POST /api/documents/batch/embeddings-reindex`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `build_scope` | `string` |  | Build Scope |
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |

### `doc.batch_memory_rebuild`

doc.batch_memory_rebuild

**`POST /api/documents/batch/memory-rebuild`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `build_scope` | `string` |  | Build Scope |
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |

### `doc.batch_ntd_check` ⛔ **approval gate**

doc.batch_ntd_check

**`POST /api/documents/batch/ntd-check`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `build_scope` | `string` |  | Build Scope |
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |

### `doc.batch_process`

doc.batch_process

**`POST /api/documents/batch/process`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `build_scope` | `string` |  | Build Scope |
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |

### `doc.bulk_delete` ⛔ **approval gate**

doc.bulk_delete

**`DELETE /api/documents/bulk-delete`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `delete_files` | `boolean` |  | Delete Files |
| `document_ids` | `array` | ✓ | Document Ids |

### `doc.classify`

doc.classify

**`POST /api/documents/{document_id}/classify`**

### `doc.correct_field`

doc.correct_field

**`POST /api/documents/{document_id}/correct-field`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `corrected_value` | `string` | ✓ | Corrected Value |
| `field_name` | `string` | ✓ | Field Name |

### `doc.delete`

doc.delete

**`DELETE /api/documents/{document_id}`**

### `doc.dependencies`

doc.dependencies

**`GET /api/documents/{document_id}/dependencies`**

### `doc.extract`

doc.extract

**`POST /api/documents/{document_id}/extract`**

### `doc.get`

doc.get

**`GET /api/documents/{document_id}`**

### `doc.ingest`

doc.ingest

**`POST /api/documents/ingest`**

### `doc.link`

doc.link

**`POST /api/documents/{document_id}/links`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `link_type` | `string` |  | Link Type |
| `linked_entity_id` | `string` | ✓ | Linked Entity Id |
| `linked_entity_type` | `string` | ✓ | Linked Entity Type |

### `doc.link_delete`

doc.link_delete

**`DELETE /api/documents/{document_id}/links/{link_id}`**

### `doc.link_update`

doc.link_update

**`PATCH /api/documents/{document_id}/links/{link_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `link_type` | `string` |  | Link Type |
| `linked_entity_id` | `string` |  | Linked Entity Id |
| `linked_entity_type` | `string` |  | Linked Entity Type |

### `doc.list`

doc.list

**`GET /api/documents`**

### `doc.management`

doc.management

**`GET /api/documents/{document_id}/management`**

### `doc.memory_rebuild`

doc.memory_rebuild

**`POST /api/documents/{document_id}/memory/rebuild`**

### `doc.summarize`

doc.summarize

**`POST /api/documents/{document_id}/summarize`**

### `doc.update`

doc.update

**`PATCH /api/documents/{document_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `doc_type` | `any` |  |  |
| `file_name` | `string` |  | File Name |
| `manual_doc_type_override` | `boolean` |  | Manual Doc Type Override |
| `metadata_` | `object` |  | Metadata |
| `source_channel` | `string` |  | Source Channel |
| `status` | `any` |  |  |

### `doc.workspace`

doc.workspace

**`GET /api/documents/workspace`**


## Email

### `email.draft`

email.draft

**`POST /api/email/drafts`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `body_html` | `string` | ✓ | Body Html |
| `body_text` | `string` |  | Body Text |
| `cc_addresses` | `array` |  | Cc Addresses |
| `context` | `object` |  | Context |
| `subject` | `string` | ✓ | Subject |
| `supplier_id` | `string` |  | Supplier Id |
| `thread_id` | `string` |  | Thread Id |
| `to_addresses` | `array` | ✓ | To Addresses |

### `email.fetch_new`

email.fetch_new

**`POST /api/email/fetch`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mailbox` | `string` |  | Mailbox |

### `email.get_thread`

email.get_thread

**`GET /api/email/threads/{thread_id}`**

### `email.list_drafts`

email.list_drafts

**`GET /api/email/drafts`**

### `email.list_threads`

email.list_threads

**`GET /api/email/threads`**

### `email.read`

email.read

**`GET /api/email/{email_id}`**

### `email.risk_check`

email.risk_check

**`POST /api/email/drafts/{draft_id}/risk-check`**

### `email.search`

email.search

**`POST /api/email/search`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email_address` | `string` |  | Email Address |
| `limit` | `integer` |  | Limit |
| `mailbox` | `string` |  | Mailbox |
| `query` | `string` |  | Query |
| `supplier_id` | `string` |  | Supplier Id |

### `email.send` ⛔ **approval gate**

email.send

**`POST /api/email/drafts/{draft_id}/send`**

### `email.style_match`

email.style_match

**`POST /api/email/style-analyze`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email_address` | `string` |  | Email Address |
| `sample_count` | `integer` |  | Sample Count |
| `supplier_id` | `string` |  | Supplier Id |

### `email.suggest_template`

email.suggest_template

**`POST /api/email/suggest-template`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_type` | `string` | ✓ | Context Type |
| `invoice_id` | `string` |  | Invoice Id |
| `language` | `string` |  | Language |
| `supplier_id` | `string` |  | Supplier Id |


## Email Templates

### `email.templates.create`

email.templates.create

**`POST /api/email-templates/`**

### `email.templates.delete` ⛔ **approval gate**

email.templates.delete

**`DELETE /api/email-templates/{template_id}`**

### `email.templates.from_message`

email.templates.from_message

**`POST /api/email-templates/from-message`**

### `email.templates.get`

email.templates.get

**`GET /api/email-templates/{template_id}`**

### `email.templates.list`

email.templates.list

**`GET /api/email-templates/`**

### `email.templates.render`

email.templates.render

**`POST /api/email-templates/{template_id}/render`**

### `email.templates.update`

email.templates.update

**`PATCH /api/email-templates/{template_id}`**


## Graph

### `graph.chunk_create`

graph.chunk_create

**`POST /api/graph/chunks`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bbox_data` | `object` |  | Bbox Data |
| `chunk_index` | `integer` | ✓ | Chunk Index |
| `document_id` | `string` | ✓ | Document Id |
| `document_version_id` | `string` |  | Document Version Id |
| `embedding_id` | `string` |  | Embedding Id |
| `metadata_` | `object` |  | Metadata |
| `page_number` | `integer` |  | Page Number |
| `text` | `string` | ✓ | Text |
| `token_count` | `integer` |  | Token Count |

### `graph.edge_create`

graph.edge_create

**`POST /api/graph/edges`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `confidence` | `number` |  | Confidence |
| `edge_type` | `string` | ✓ | Edge Type |
| `evidence_span_id` | `string` |  | Evidence Span Id |
| `metadata_` | `object` |  | Metadata |
| `reason` | `string` |  | Reason |
| `source_document_id` | `string` |  | Source Document Id |
| `source_document_version_id` | `string` |  | Source Document Version Id |
| `source_node_id` | `string` | ✓ | Source Node Id |
| `target_node_id` | `string` | ✓ | Target Node Id |

### `graph.evidence_create`

graph.evidence_create

**`POST /api/graph/evidence`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bbox_data` | `object` |  | Bbox Data |
| `chunk_id` | `string` |  | Chunk Id |
| `confidence` | `number` |  | Confidence |
| `document_id` | `string` | ✓ | Document Id |
| `document_version_id` | `string` |  | Document Version Id |
| `field_name` | `string` |  | Field Name |
| `metadata_` | `object` |  | Metadata |
| `page_number` | `integer` |  | Page Number |
| `text` | `string` | ✓ | Text |

### `graph.mention_create`

graph.mention_create

**`POST /api/graph/mentions`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `chunk_id` | `string` |  | Chunk Id |
| `confidence` | `number` |  | Confidence |
| `document_id` | `string` | ✓ | Document Id |
| `document_version_id` | `string` |  | Document Version Id |
| `end_offset` | `integer` |  | End Offset |
| `entity_type` | `string` | ✓ | Entity Type |
| `evidence_span_id` | `string` |  | Evidence Span Id |
| `extraction_method` | `string` |  | Extraction Method |
| `mention_text` | `string` | ✓ | Mention Text |
| `metadata_` | `object` |  | Metadata |
| `node_id` | `string` |  | Node Id |
| `start_offset` | `integer` |  | Start Offset |

### `graph.neighborhood`

graph.neighborhood

**`GET /api/graph/nodes/{node_id}/neighborhood`**

### `graph.node_create`

graph.node_create

**`POST /api/graph/nodes`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `aliases` | `array` |  | Aliases |
| `canonical_key` | `string` |  | Canonical Key |
| `confidence` | `number` |  | Confidence |
| `entity_id` | `string` |  | Entity Id |
| `entity_type` | `string` |  | Entity Type |
| `metadata_` | `object` |  | Metadata |
| `node_type` | `string` | ✓ | Node Type |
| `summary` | `string` |  | Summary |
| `title` | `string` | ✓ | Title |

### `graph.node_get`

graph.node_get

**`GET /api/graph/nodes/{node_id}`**

### `graph.path`

graph.path

**`GET /api/graph/path`**

### `graph.review_decide`

graph.review_decide

**`POST /api/graph/review/{item_id}/decide`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `string` | ✓ | Action |
| `comment` | `string` |  | Comment |
| `decided_by` | `string` |  | Decided By |

### `graph.review_list`

graph.review_list

**`GET /api/graph/review`**


## Invoices

### `invoice.approve` ⛔ **approval gate**

invoice.approve

**`POST /api/invoices/{invoice_id}/approve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `comment` | `string` |  | Comment |

### `invoice.bulk_delete` ⛔ **approval gate**

invoice.bulk_delete

**`DELETE /api/invoices`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `delete_all` | `boolean` |  | Delete All |
| `ids` | `array` |  | Ids |
| `status` | `any` |  |  |
| `supplier_id` | `string` |  | Supplier Id |

### `invoice.compare_prices`

invoice.compare_prices

**`GET /api/invoices/{invoice_id}/price-check`**

### `invoice.delete` ⛔ **approval gate**

invoice.delete

**`DELETE /api/invoices/{invoice_id}`**

### `invoice.extract`

invoice.extract

**`POST /api/invoices/{invoice_id}/re-extract`**

### `invoice.get`

invoice.get

**`GET /api/invoices/{invoice_id}`**

### `invoice.list`

invoice.list

**`GET /api/invoices`**

### `invoice.receive`

invoice.receive

**`POST /api/invoices/{invoice_id}/receive`**

### `invoice.reject` ⛔ **approval gate**

invoice.reject

**`POST /api/invoices/{invoice_id}/reject`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | `string` | ✓ | Reason |

### `invoice.update`

invoice.update

**`PATCH /api/invoices/{invoice_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `currency` | `string` |  | Currency |
| `due_date` | `string` |  | Due Date |
| `invoice_date` | `string` |  | Invoice Date |
| `invoice_number` | `string` |  | Invoice Number |
| `notes` | `string` |  | Notes |
| `payment_id` | `string` |  | Payment Id |
| `subtotal` | `number` |  | Subtotal |
| `tax_amount` | `number` |  | Tax Amount |
| `total_amount` | `number` |  | Total Amount |
| `validity_date` | `string` |  | Validity Date |

### `invoice.validate`

invoice.validate

**`POST /api/invoices/{invoice_id}/validate`**


## Mailboxes

### `mailbox.create` ⛔ **approval gate**

mailbox.create

**`POST /api/mailbox/configs`**

### `mailbox.delete` ⛔ **approval gate**

mailbox.delete

**`DELETE /api/mailbox/configs/{mailbox_id}`**

### `mailbox.get`

mailbox.get

**`GET /api/mailbox/configs/{mailbox_id}`**

### `mailbox.list`

mailbox.list

**`GET /api/mailbox/configs`**

### `mailbox.test` ⛔ **approval gate**

mailbox.test

**`POST /api/mailbox/configs/{mailbox_id}/test`**

### `mailbox.update`

mailbox.update

**`PATCH /api/mailbox/configs/{mailbox_id}`**


## Memory

### `memory.embeddings_index_active`

memory.embeddings_index_active

**`POST /api/memory/embeddings/index-active`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `limit` | `integer` |  | Limit |
| `statuses` | `array` |  | Statuses |

### `memory.embeddings_rebuild`

memory.embeddings_rebuild

**`POST /api/memory/embeddings/rebuild`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `collection_name` | `string` |  | Collection Name |
| `content_types` | `array` |  | Content Types |
| `document_id` | `string` |  | Document Id |
| `embedding_model` | `string` |  | Embedding Model |
| `limit` | `integer` |  | Limit |
| `mark_stale_existing` | `boolean` |  | Mark Stale Existing |
| `vector_size` | `integer` |  | Vector Size |

### `memory.embeddings_rebuild_active`

memory.embeddings_rebuild_active

**`POST /api/memory/embeddings/rebuild-active`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `collection_name` | `string` |  | Collection Name |
| `content_types` | `array` |  | Content Types |
| `document_id` | `string` |  | Document Id |
| `embedding_model` | `string` |  | Embedding Model |
| `limit` | `integer` |  | Limit |
| `mark_stale_existing` | `boolean` |  | Mark Stale Existing |
| `vector_size` | `integer` |  | Vector Size |

### `memory.embeddings_stats`

memory.embeddings_stats

**`GET /api/memory/embeddings/stats`**

### `memory.explain`

memory.explain

**`POST /api/memory/explain`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `include_explain` | `boolean` |  | Include Explain |
| `limit` | `integer` |  | Limit |
| `neighborhood_depth` | `integer` |  | Neighborhood Depth |
| `node_types` | `array` |  | Node Types |
| `query` | `string` | ✓ | Query |
| `retrieval_mode` | `string` |  | Retrieval Mode |

### `memory.prune`

memory.prune

**`POST /api/memory/prune`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `dry_run` | `boolean` |  | Dry Run |
| `kinds` | `array` |  | Kinds |
| `older_than_days` | `integer` |  | Older Than Days |
| `scope` | `string` |  | Scope |

### `memory.reindex`

memory.reindex

**`POST /api/memory/reindex`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` |  | Document Ids |
| `limit` | `integer` |  | Limit |
| `rebuild` | `boolean` |  | Rebuild |

### `memory.search`

memory.search

**`POST /api/memory/search`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cursor` | `string` |  | Cursor |
| `document_id` | `string` |  | Document Id |
| `entity_hints` | `array` |  | Entity Hints |
| `include_explain` | `boolean` |  | Include Explain |
| `intent` | `string` |  | Intent |
| `limit` | `integer` |  | Limit |
| `need_full_coverage` | `boolean` |  | Need Full Coverage |
| `node_types` | `array` |  | Node Types |
| `query` | `string` | ✓ | Query |
| `retrieval_mode` | `string` |  | Retrieval Mode |
| `scope` | `string` |  | Scope |


## Normalization

### `norm.activate_rule` ⛔ **approval gate**

norm.activate_rule

**`POST /api/normalization/rules/{rule_id}/activate`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `activated_by` | `string` |  | Activated By |

### `norm.apply_rules` ⛔ **approval gate**

norm.apply_rules

**`POST /api/normalization/apply`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` | ✓ | Document Id |

### `norm.create_norm_card`

norm.create_norm_card

**`POST /api/normalization/norm-cards`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | `string` |  | Approved By |
| `canonical_item_id` | `string` | ✓ | Canonical Item Id |
| `loss_factor` | `number` |  | Loss Factor |
| `norm_qty` | `number` | ✓ | Norm Qty |
| `notes` | `string` |  | Notes |
| `product_code` | `string` |  | Product Code |
| `unit` | `string` | ✓ | Unit |
| `valid_from` | `string` |  | Valid From |
| `valid_to` | `string` |  | Valid To |

### `norm.get_canonical_item`

norm.get_canonical_item

**`GET /api/normalization/canonical-items/{item_id}`**

### `norm.get_item_norm_cards`

norm.get_item_norm_cards

**`GET /api/normalization/canonical-items/{item_id}/norm-cards`**

### `norm.list_canonical_items`

norm.list_canonical_items

**`GET /api/normalization/canonical-items`**

### `norm.list_norm_cards`

norm.list_norm_cards

**`GET /api/normalization/norm-cards`**

### `norm.list_rules`

norm.list_rules

**`GET /api/normalization/rules`**

### `norm.suggest_rule`

norm.suggest_rule

**`POST /api/normalization/suggest`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `field_name` | `string` |  | Field Name |
| `min_corrections` | `integer` |  | Minimum repeated corrections to suggest a rule |

### `norm.update_canonical_item`

norm.update_canonical_item

**`PATCH /api/normalization/canonical-items/{item_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `gost` | `string` |  | Gost |
| `hazard_class` | `string` |  | Hazard Class |
| `okpd2_code` | `string` |  | Okpd2 Code |

### `norm.update_norm_card`

norm.update_norm_card

**`PATCH /api/normalization/norm-cards/{card_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | `string` |  | Approved By |
| `loss_factor` | `number` |  | Loss Factor |
| `norm_qty` | `number` |  | Norm Qty |
| `notes` | `string` |  | Notes |
| `product_code` | `string` |  | Product Code |
| `unit` | `string` |  | Unit |
| `valid_from` | `string` |  | Valid From |
| `valid_to` | `string` |  | Valid To |


## NTD / Technology

### `ntd.check_availability`

ntd.check_availability

**`GET /api/documents/{document_id}/ntd-check/availability`**

### `ntd.check_get`

ntd.check_get

**`GET /api/ntd/checks/{check_id}`**

### `ntd.check_list`

ntd.check_list

**`GET /api/documents/{document_id}/ntd-checks`**

### `ntd.clause_create`

ntd.clause_create

**`POST /api/ntd/clauses`**

### `ntd.control_settings_get`

ntd.control_settings_get

**`GET /api/settings/ntd-control`**

### `ntd.control_settings_update` ⛔ **approval gate**

ntd.control_settings_update

**`PATCH /api/settings/ntd-control`**

### `ntd.document_create`

ntd.document_create

**`POST /api/ntd/documents`**

### `ntd.document_create_from_source`

ntd.document_create_from_source

**`POST /api/ntd/documents/from-source`**

### `ntd.document_index`

ntd.document_index

**`POST /api/ntd/documents/{normative_document_id}/index`**

### `ntd.document_list`

ntd.document_list

**`GET /api/ntd/documents`**

### `ntd.finding_decide` ⛔ **approval gate**

ntd.finding_decide

**`POST /api/ntd/checks/{check_id}/findings/{finding_id}/decide`**

### `ntd.norm_control_run` ⛔ **approval gate**

ntd.norm_control_run

**`POST /api/documents/{document_id}/ntd-check`**

### `ntd.norm_control_run_payload` ⛔ **approval gate**

ntd.norm_control_run_payload

**`POST /api/ntd/checks/run`**

### `ntd.requirement_create`

ntd.requirement_create

**`POST /api/ntd/requirements`**

### `ntd.requirement_search`

ntd.requirement_search

**`GET /api/ntd/requirements/search`**


## Payments

### `payment.create_schedule`

payment.create_schedule

**`POST /api/payment-schedules`**

### `payment.list_schedule`

payment.list_schedule

**`GET /api/payment-schedules`**

### `payment.mark_paid` ⛔ **approval gate**

payment.mark_paid

**`POST /api/payment-schedules/{schedule_id}/mark-paid`**

### `payment.overdue`

payment.overdue

**`GET /api/payment-schedules/overdue`**

### `payment.schedule_from_invoice`

payment.schedule_from_invoice

**`POST /api/invoices/{invoice_id}/schedule-payment`**

### `payment.upcoming`

payment.upcoming

**`GET /api/payment-schedules/upcoming`**


## Procurement

### `procurement.create_contract`

procurement.create_contract

**`POST /api/supplier-contracts`**

### `procurement.create_request`

procurement.create_request

**`POST /api/purchase-requests`**

### `procurement.get_contract`

procurement.get_contract

**`GET /api/supplier-contracts/{contract_id}`**

### `procurement.get_request`

procurement.get_request

**`GET /api/purchase-requests/{req_id}`**

### `procurement.list_contracts`

procurement.list_contracts

**`GET /api/supplier-contracts`**

### `procurement.list_requests`

procurement.list_requests

**`GET /api/purchase-requests`**

### `procurement.send_rfq` ⛔ **approval gate**

procurement.send_rfq

**`POST /api/purchase-requests/{req_id}/send-rfq`**

### `procurement.update_contract`

procurement.update_contract

**`PATCH /api/supplier-contracts/{contract_id}`**

### `procurement.update_request`

procurement.update_request

**`PATCH /api/purchase-requests/{req_id}`**


## Quarantine

### `quarantine.list`

quarantine.list

**`GET /api/quarantine`**


## Search & NL

### `doc.search`

doc.search

**`POST /api/search/documents`**

### `search.hybrid`

search.hybrid

**`POST /api/search/hybrid`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `doc_type` | `string` |  | Doc Type |
| `limit` | `integer` |  | Limit |
| `query` | `string` | ✓ | Query |
| `status` | `string` |  | Status |

### `search.nl`

search.nl

**`POST /api/search/nl`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `limit` | `integer` |  | Limit |
| `query` | `string` | ✓ | Query |

### `search.nl_to_query`

search.nl_to_query

**`POST /api/search/nl`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `limit` | `integer` |  | Limit |
| `query` | `string` | ✓ | Query |

### `search.similar`

search.similar

**`GET /api/search/similar/{entity_type}/{entity_id}`**


## Suppliers

### `supplier.alerts`

supplier.alerts

**`GET /api/suppliers/{supplier_id}/alerts`**

### `supplier.check_requisites`

supplier.check_requisites

**`POST /api/suppliers/{supplier_id}/check-requisites`**

### `supplier.get`

supplier.get

**`GET /api/suppliers/{supplier_id}`**

### `supplier.list`

supplier.list

**`GET /api/suppliers`**

### `supplier.list`

supplier.list

**`GET /api/suppliers`**

### `supplier.price_history`

supplier.price_history

**`GET /api/suppliers/{supplier_id}/price-history`**

### `supplier.search`

supplier.search

**`POST /api/suppliers/search`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `limit` | `integer` |  | Limit |
| `query` | `string` | ✓ | Query |

### `supplier.trust_score`

supplier.trust_score

**`GET /api/suppliers/{supplier_id}/trust-score`**

### `supplier.update`

supplier.update

**`PATCH /api/suppliers/{supplier_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `address` | `string` |  | Address |
| `bank_account` | `string` |  | Bank Account |
| `bank_bik` | `string` |  | Bank Bik |
| `bank_name` | `string` |  | Bank Name |
| `contact_email` | `string` |  | Contact Email |
| `contact_phone` | `string` |  | Contact Phone |
| `corr_account` | `string` |  | Corr Account |
| `inn` | `string` |  | Inn |
| `kpp` | `string` |  | Kpp |
| `name` | `string` |  | Name |
| `notes` | `string` |  | Notes |
| `ogrn` | `string` |  | Ogrn |
| `user_notes` | `string` |  | User Notes |
| `user_rating` | `integer` |  | User Rating |


## Tables & Export

### `table.apply_diff` ⛔ **approval gate**

table.apply_diff

**`POST /api/tables/apply-diff`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `rows` | `array` | ✓ | Rows |

### `table.batch_action`

table.batch_action

**`POST /api/tables/batch`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `string` | ✓ | Action |
| `entity_ids` | `array` | ✓ | Entity Ids |
| `reason` | `string` |  | Reason |

### `table.create_view`

table.create_view

**`POST /api/tables/views`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `columns` | `array` |  | Columns |
| `filters` | `array` |  | Filters |
| `is_shared` | `boolean` |  | Is Shared |
| `name` | `string` | ✓ | Name |
| `sort` | `array` |  | Sort |
| `table` | `string` |  | Table |

### `table.delete_view`

table.delete_view

**`DELETE /api/tables/views/{view_id}`**

### `table.export_1c`

table.export_1c

**`POST /api/tables/export-1c`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `filters` | `array` |  | Filters |
| `format` | `string` |  | Format |
| `invoice_ids` | `array` |  | Invoice Ids |

### `table.export_excel`

table.export_excel

**`POST /api/tables/export`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `columns` | `array` |  | Columns |
| `filters` | `array` |  | Filters |
| `format` | `string` |  | Format |
| `table` | `string` |  | Table |

### `table.import_excel` ⛔ **approval gate**

table.import_excel

**`POST /api/tables/import`**

### `table.inline_edit`

table.inline_edit

**`POST /api/tables/inline-edit`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | `string` | ✓ | Entity Id |
| `field` | `string` | ✓ | Field |
| `value` | `string` | ✓ | Value |

### `table.list_views`

table.list_views

**`GET /api/tables/views`**

### `table.query`

table.query

**`POST /api/tables/query`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `columns` | `array` |  | Columns |
| `filters` | `array` |  | Filters |
| `limit` | `integer` |  | Limit |
| `offset` | `integer` |  | Offset |
| `search` | `string` |  | Search |
| `sort` | `array` |  | Sort |
| `table` | `string` |  | Table |


## Technology Cards

### `tech.analyze_surfaces`

tech.analyze_surfaces

**`POST /api/technology/process-plans/{plan_id}/analyze-surfaces`**

### `tech.blank_spec_set`

tech.blank_spec_set

**`POST /api/technology/process-plans/{plan_id}/blank-spec`**

### `tech.calculate_cutting_params`

tech.calculate_cutting_params

**`POST /api/technology/process-plans/{plan_id}/calculate-cutting-params`**

### `tech.correction_record`

tech.correction_record

**`POST /api/technology/corrections`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `corrected_by` | `string` | ✓ | Corrected By |
| `correction_type` | `string` |  | Correction Type |
| `entity_id` | `string` | ✓ | Entity Id |
| `entity_type` | `string` | ✓ | Entity Type |
| `field_name` | `string` | ✓ | Field Name |
| `metadata_` | `object` |  | Metadata |
| `new_value` | `string` |  | New Value |
| `old_value` | `string` |  | Old Value |
| `operation_id` | `string` |  | Operation Id |
| `process_plan_id` | `string` |  | Process Plan Id |
| `reason` | `string` |  | Reason |
| `source_document_id` | `string` |  | Source Document Id |

### `tech.export_gost_forms`

tech.export_gost_forms

**`POST /api/technology/process-plans/{plan_id}/export-gost`**

### `tech.generate_tp_from_drawing`

tech.generate_tp_from_drawing

**`POST /api/technology/process-plans/generate-from-drawing`**

### `tech.learning_rule_activate` ⛔ **approval gate**

tech.learning_rule_activate

**`POST /api/technology/learning-rules/{rule_id}/activate`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `activated_by` | `string` | ✓ | Activated By |
| `comment` | `string` |  | Comment |

### `tech.learning_rule_create`

tech.learning_rule_create

**`POST /api/technology/learning-rules`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `confidence` | `number` |  | Confidence |
| `entity_type` | `string` | ✓ | Entity Type |
| `field_name` | `string` | ✓ | Field Name |
| `match_old_value` | `string` |  | Match Old Value |
| `metadata_` | `object` |  | Metadata |
| `occurrences` | `integer` |  | Occurrences |
| `replacement_value` | `string` |  | Replacement Value |
| `rule_type` | `string` |  | Rule Type |
| `status` | `string` |  | Status |
| `suggested_by` | `string` |  | Suggested By |

### `tech.learning_rule_list`

tech.learning_rule_list

**`GET /api/technology/learning-rules`**

### `tech.learning_suggest`

tech.learning_suggest

**`GET /api/technology/learning-suggestions`**

### `tech.norm_estimate_approve` ⛔ **approval gate**

tech.norm_estimate_approve

**`POST /api/technology/norm-estimates/{estimate_id}/approve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | `string` | ✓ | Approved By |
| `comment` | `string` |  | Comment |

### `tech.norm_estimate_create`

tech.norm_estimate_create

**`POST /api/technology/process-plans/{plan_id}/norm-estimates`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `assumptions` | `array` |  | Assumptions |
| `batch_size` | `number` |  | Batch Size |
| `confidence` | `number` |  | Confidence |
| `created_by` | `string` |  | Created By |
| `labor_minutes` | `number` |  | Labor Minutes |
| `machine_minutes` | `number` |  | Machine Minutes |
| `metadata_` | `object` |  | Metadata |
| `method` | `string` |  | Method |
| `operation_id` | `string` |  | Operation Id |
| `setup_minutes` | `number` |  | Setup Minutes |

### `tech.norm_estimate_suggest`

tech.norm_estimate_suggest

**`POST /api/technology/process-plans/{plan_id}/estimate-norms`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `batch_size` | `number` |  | Batch Size |
| `created_by` | `string` |  | Created By |
| `overwrite_existing` | `boolean` |  | Overwrite Existing |

### `tech.normcontrol_check`

tech.normcontrol_check

**`POST /api/technology/process-plans/{plan_id}/normcontrol`**

### `tech.normcontrol_resolve`

tech.normcontrol_resolve

**`POST /api/technology/process-plans/{plan_id}/normcontrol/{check_id}/resolve`**

### `tech.operation_add`

tech.operation_add

**`POST /api/technology/process-plans/{plan_id}/operations`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `control_requirements` | `string` |  | Control Requirements |
| `cutting_parameters` | `object` |  | Cutting Parameters |
| `fixture_resource_id` | `string` |  | Fixture Resource Id |
| `labor_minutes` | `number` |  | Labor Minutes |
| `machine_minutes` | `number` |  | Machine Minutes |
| `machine_resource_id` | `string` |  | Machine Resource Id |
| `metadata_` | `object` |  | Metadata |
| `name` | `string` | ✓ | Name |
| `operation_code` | `string` |  | Operation Code |
| `operation_type` | `string` |  | Operation Type |
| `safety_requirements` | `string` |  | Safety Requirements |
| `sequence_no` | `integer` | ✓ | Sequence No |
| `setup_description` | `string` |  | Setup Description |
| `setup_minutes` | `number` |  | Setup Minutes |
| `tool_resource_id` | `string` |  | Tool Resource Id |
| `transition_text` | `string` |  | Transition Text |

### `tech.operation_template_create`

tech.operation_template_create

**`POST /api/technology/operation-templates`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `default_control_requirements` | `string` |  | Default Control Requirements |
| `default_operation_code` | `string` |  | Default Operation Code |
| `default_safety_requirements` | `string` |  | Default Safety Requirements |
| `default_transition_text` | `string` |  | Default Transition Text |
| `is_active` | `boolean` |  | Is Active |
| `metadata_` | `object` |  | Metadata |
| `name` | `string` | ✓ | Name |
| `operation_type` | `string` | ✓ | Operation Type |
| `parameters_schema` | `object` |  | Parameters Schema |
| `required_resource_types` | `array` |  | Required Resource Types |
| `standard_system` | `string` |  | Standard System |

### `tech.operation_template_list`

tech.operation_template_list

**`GET /api/technology/operation-templates`**

### `tech.process_plan_approve` ⛔ **approval gate**

tech.process_plan_approve

**`POST /api/technology/process-plans/{plan_id}/approve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | `string` | ✓ | Approved By |
| `comment` | `string` |  | Comment |

### `tech.process_plan_create`

tech.process_plan_create

**`POST /api/technology/process-plans`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `blank_type` | `string` |  | Blank Type |
| `bom_id` | `string` |  | Bom Id |
| `created_by` | `string` |  | Created By |
| `document_id` | `string` |  | Document Id |
| `material` | `string` |  | Material |
| `metadata_` | `object` |  | Metadata |
| `product_code` | `string` |  | Product Code |
| `product_name` | `string` | ✓ | Product Name |
| `quality_requirements` | `string` |  | Quality Requirements |
| `route_summary` | `string` |  | Route Summary |
| `standard_system` | `string` |  | Standard System |
| `status` | `string` |  | Status |
| `version` | `string` |  | Version |

### `tech.process_plan_draft_from_document`

tech.process_plan_draft_from_document

**`POST /api/technology/process-plans/draft-from-document`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `created_by` | `string` |  | Created By |
| `document_id` | `string` | ✓ | Document Id |
| `product_code` | `string` |  | Product Code |
| `product_name` | `string` |  | Product Name |
| `rebuild_existing` | `boolean` |  | Rebuild Existing |

### `tech.process_plan_get`

tech.process_plan_get

**`GET /api/technology/process-plans/{plan_id}`**

### `tech.process_plan_list`

tech.process_plan_list

**`GET /api/technology/process-plans`**

### `tech.process_plan_validate`

tech.process_plan_validate

**`POST /api/technology/process-plans/{plan_id}/validate`**

### `tech.resource_create`

tech.resource_create

**`POST /api/technology/resources`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `capabilities` | `object` |  | Capabilities |
| `code` | `string` |  | Code |
| `location` | `string` |  | Location |
| `metadata_` | `object` |  | Metadata |
| `model` | `string` |  | Model |
| `name` | `string` | ✓ | Name |
| `notes` | `string` |  | Notes |
| `resource_type` | `string` | ✓ | Resource Type |
| `standard` | `string` |  | Standard |
| `status` | `string` |  | Status |

### `tech.resource_list`

tech.resource_list

**`GET /api/technology/resources`**

### `tech.select_equipment_for_op`

tech.select_equipment_for_op

**`POST /api/technology/process-plans/{plan_id}/select-equipment`**

### `tech.surface_specs_list`

tech.surface_specs_list

**`GET /api/technology/process-plans/{plan_id}/surface-specs`**


## Warehouse

### `warehouse.adjust_stock`

warehouse.adjust_stock

**`POST /api/warehouse/inventory/{item_id}/adjust`**

### `warehouse.bulk_confirm`

warehouse.bulk_confirm

**`POST /api/warehouse/receipts/bulk-confirm`**

### `warehouse.confirm_receipt` ⛔ **approval gate**

warehouse.confirm_receipt

**`POST /api/warehouse/receipts/{receipt_id}/confirm`**

### `warehouse.create_item`

warehouse.create_item

**`POST /api/warehouse/inventory`**

### `warehouse.create_receipt`

warehouse.create_receipt

**`POST /api/warehouse/receipts`**

### `warehouse.delete_item` ⛔ **approval gate**

warehouse.delete_item

**`DELETE /api/warehouse/inventory/{item_id}`**

### `warehouse.get_item`

warehouse.get_item

**`GET /api/warehouse/inventory/{item_id}`**

### `warehouse.get_receipt`

warehouse.get_receipt

**`GET /api/warehouse/receipts/{receipt_id}`**

### `warehouse.issue_stock` ⛔ **approval gate**

warehouse.issue_stock

**`POST /api/warehouse/inventory/{item_id}/issue`**

### `warehouse.list_inventory`

warehouse.list_inventory

**`GET /api/warehouse/inventory`**

### `warehouse.list_movements`

warehouse.list_movements

**`GET /api/warehouse/movements`**

### `warehouse.list_receipts`

warehouse.list_receipts

**`GET /api/warehouse/receipts`**

### `warehouse.low_stock`

warehouse.low_stock

**`GET /api/warehouse/inventory/low-stock`**

### `warehouse.update_item`

warehouse.update_item

**`PATCH /api/warehouse/inventory/{item_id}`**

### `warehouse.update_status`

warehouse.update_status

**`PATCH /api/warehouse/receipts/{receipt_id}/status`**


## Workspace

### `workspace.general`

workspace.general

**`POST /api/workspace/agent/generated/general`**

### `workspace.invoice_items_by_supplier_table`

workspace.invoice_items_by_supplier_table

**`POST /api/workspace/agent/invoices/items-by-supplier-table`**

### `workspace.invoice_items_grouped_table`

workspace.invoice_items_grouped_table

**`POST /api/workspace/agent/invoices/items-grouped-table`**

### `workspace.invoice_items_table`

workspace.invoice_items_table

**`POST /api/workspace/agent/invoices/items-table`**

### `workspace.invoice_table`

workspace.invoice_table

**`POST /api/workspace/agent/invoices/table`**

### `workspace.sql_table`

workspace.sql_table

**`POST /api/workspace/agent/generated/sql-table`**

### `workspace.verify_block`

workspace.verify_block

**`POST /api/workspace/agent/verify-block`**

