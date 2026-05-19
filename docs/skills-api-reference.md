# AiAgent Skills API Reference

*Auto-generated from FastAPI Pydantic schemas. Version 1. Total: 220 skills.*

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
- [Technology Cards (19)](#technology-cards)
- [Warehouse (15)](#warehouse)
- [Workspace (7)](#workspace)

## Anomalies

### `anomaly.check_all`

Run all anomaly detectors on an invoice.

**`POST /api/anomalies/check`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invoice_id` | `string` |  | Invoice Id |
| `document_id` | `string` |  | Document Id |

### `anomaly.create_card`

Manually create an anomaly card.

**`POST /api/anomalies`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `anomaly_type` | `string` | ✓ | Anomaly Type |
| `severity` | `string` |  | Severity |
| `entity_type` | `string` | ✓ | Entity Type |
| `entity_id` | `string` | ✓ | Entity Id |
| `title` | `string` | ✓ | Title |
| `description` | `string` |  | Description |
| `details` | `object` |  | Details |

### `anomaly.list`

List anomaly cards.

**`GET /api/anomalies`**

### `anomaly.resolve` ⛔ **approval gate**

Resolve an anomaly (approval gate).

**`POST /api/anomalies/{anomaly_id}/resolve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `resolution` | `string` | ✓ | Resolution |
| `comment` | `string` |  | Comment |

### `anomaly.explain`

Get human-readable explanation of an anomaly.

**`GET /api/anomalies/{anomaly_id}/explain`**

## Approvals

### `approval.request`

Request human approval. Blocks agent.

**`POST /api/approvals`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action_type` | `?` | ✓ |  |
| `entity_type` | `string` | ✓ | Entity Type |
| `entity_id` | `string` | ✓ | Entity Id |
| `requested_by` | `string` |  | Requested By |
| `assigned_to` | `string` |  | Assigned To |
| `context` | `object` |  | Context |
| `expires_at` | `string` |  | Expires At |

### `approval.list_pending`

List pending approvals (excludes dormant chain steps).

**`GET /api/approvals/pending`**

### `approval.status`

Check approval status.

**`GET /api/approvals/{approval_id}`**

## BOMs

### `bom.list`

List BOMs (bill of materials).

**`GET /api/boms`**

### `bom.create`

Create a new BOM.

**`POST /api/boms`**

### `bom.get`

Get BOM with all lines.

**`GET /api/boms/{bom_id}`**

### `bom.update`

Update BOM metadata.

**`PATCH /api/boms/{bom_id}`**

### `bom.approve` ⛔ **approval gate**

Approve BOM (approval gate).

**`POST /api/boms/{bom_id}/approve`**

### `bom.stock_check`

Check inventory availability for BOM production.

**`GET /api/boms/{bom_id}/stock-check`**

### `bom.create_purchase_request` ⛔ **approval gate**

Create purchase request from BOM shortage (approval gate).

**`POST /api/boms/{bom_id}/create-purchase-request`**

## Calendar

### `calendar.create_event`

Create a calendar event.

**`POST /api/calendar/events`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | `string` | ✓ | Title |
| `event_date` | `string` | ✓ | Event Date |
| `event_type` | `string` | ✓ | Event Type |
| `entity_type` | `string` |  | Entity Type |
| `entity_id` | `string` |  | Entity Id |
| `source` | `string` |  | Source |

### `calendar.list_events`

List calendar events with filters.

**`GET /api/calendar/events`**

### `calendar.extract_dates`

Extract dates from invoice and create events.

**`POST /api/calendar/extract-dates`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invoice_id` | `string` | ✓ | Invoice Id |

### `calendar.upcoming`

Get upcoming events and pending reminders.

**`GET /api/calendar/upcoming`**

### `calendar.create_reminder`

Create a reminder.

**`POST /api/calendar/reminders`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | `string` | ✓ | Entity Type |
| `entity_id` | `string` | ✓ | Entity Id |
| `remind_at` | `string` | ✓ | Remind At |
| `message` | `string` | ✓ | Message |
| `calendar_event_id` | `string` |  | Calendar Event Id |

### `calendar.list_reminders`

List reminders.

**`GET /api/calendar/reminders`**

### `calendar.generate_followup`

Create a follow-up draft email for an invoice reminder.

**`POST /api/calendar/reminders/{reminder_id}/generate-followup`**

### `calendar.mark_sent`

Mark a reminder as sent.

**`POST /api/calendar/reminders/{reminder_id}/mark-sent`**

## Canvas

### `canvas.publish`

Publish a rich content block to the existing Workspace.

**`POST /api/canvas/publish`**

## Collections

### `collection.create`

Create a new collection.

**`POST /api/collections`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | ✓ | Name |
| `description` | `string` |  | Description |

### `collection.list`

List collections.

**`GET /api/collections`**

### `collection.get`

Get collection with items.

**`GET /api/collections/{collection_id}`**

### `collection.add`

Add item to collection.

**`POST /api/collections/{collection_id}/items`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | `string` | ✓ | Entity Type |
| `entity_id` | `string` | ✓ | Entity Id |
| `note` | `string` |  | Note |

### `collection.summarize`

Summarize collection contents.

**`POST /api/collections/{collection_id}/summarize`**

### `collection.timeline`

Get timeline of events for items in collection.

**`GET /api/collections/{collection_id}/timeline`**

### `collection.search`

Search within a collection's items.

**`GET /api/collections/{collection_id}/search`**

## Compare (КП)

### `compare.create`

Create a comparison session for commercial offers.

**`POST /api/compare`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | ✓ | Name |
| `invoice_ids` | `array` | ✓ | Invoice Ids |

### `compare.list`

List comparison sessions.

**`GET /api/compare`**

### `compare.get`

Get a comparison session.

**`GET /api/compare/{session_id}`**

### `compare.align`

Align line items across invoices for comparison.

**`POST /api/compare/{session_id}/align`**

### `compare.decide` ⛔ **approval gate**

Choose a supplier (approval gate).

**`POST /api/compare/{session_id}/decide`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `chosen_supplier_id` | `string` | ✓ | Chosen Supplier Id |
| `reasoning` | `string` |  | Reasoning |

### `compare.summary`

Get comparison summary with recommendation.

**`GET /api/compare/{session_id}/summary`**

## Dashboard

### `dashboard.today`

Read counters and recent activity for today.

**`GET /api/dashboard/today`**

## Documents

### `doc.ingest`

Accept file, store, create Document record.

**`POST /api/documents/ingest`**

### `doc.list`

List documents with filters.

**`GET /api/documents`**

### `doc.workspace`

List documents with compact pipeline summaries.

**`GET /api/documents/workspace`**

### `doc.get`

Get document with extractions and links.

**`GET /api/documents/{document_id}`**

### `doc.bulk_delete` ⛔ **approval gate**

Hard-delete selected documents and derived records.

**`DELETE /api/documents/bulk-delete`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` | ✓ | Document Ids |
| `delete_files` | `boolean` |  | Delete Files |

### `doc.batch_process`

Trigger full processing for selected documents.

**`POST /api/documents/batch/process`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |
| `build_scope` | `string` |  | Build Scope |

### `doc.batch_classify`

Trigger classification for selected documents.

**`POST /api/documents/batch/classify`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |
| `build_scope` | `string` |  | Build Scope |

### `doc.batch_embeddings_reindex`

Queue embedding rebuild for selected documents.

**`POST /api/documents/batch/embeddings-reindex`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |
| `build_scope` | `string` |  | Build Scope |

### `doc.batch_memory_rebuild`

Rebuild graph memory for selected documents.

**`POST /api/documents/batch/memory-rebuild`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |
| `build_scope` | `string` |  | Build Scope |

### `doc.batch_ntd_check` ⛔ **approval gate**

Run manual NTD checks for selected documents.

**`POST /api/documents/batch/ntd-check`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` | ✓ | Document Ids |
| `force` | `boolean` |  | Force |
| `build_scope` | `string` |  | Build Scope |

### `doc.management`

Read document pipeline, memory, graph and NTD status.

**`GET /api/documents/{document_id}/management`**

### `doc.update`

Update document fields.

**`PATCH /api/documents/{document_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_name` | `string` |  | File Name |
| `doc_type` | `?` |  |  |
| `status` | `?` |  |  |
| `source_channel` | `string` |  | Source Channel |
| `manual_doc_type_override` | `boolean` |  | Manual Doc Type Override |
| `metadata` | `object` |  | Metadata |

### `doc.link`

Link document to an entity.

**`POST /api/documents/{document_id}/links`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `linked_entity_type` | `string` | ✓ | Linked Entity Type |
| `linked_entity_id` | `string` | ✓ | Linked Entity Id |
| `link_type` | `string` |  | Link Type |

### `doc.link_update`

Edit an explicit document dependency link.

**`PATCH /api/documents/{document_id}/links/{link_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `linked_entity_type` | `string` |  | Linked Entity Type |
| `linked_entity_id` | `string` |  | Linked Entity Id |
| `link_type` | `string` |  | Link Type |

### `doc.link_delete`

Remove an explicit document dependency link.

**`DELETE /api/documents/{document_id}/links/{link_id}`**

### `doc.dependencies`

Search explicit links and graph dependencies for a document.

**`GET /api/documents/{document_id}/dependencies`**

### `doc.classify`

Trigger document classification via AI.

**`POST /api/documents/{document_id}/classify`**

### `doc.extract`

Trigger full extraction pipeline (classify → extract → validate).

**`POST /api/documents/{document_id}/extract`**

### `doc.memory_rebuild`

Rebuild graph memory for one document.

**`POST /api/documents/{document_id}/memory/rebuild`**

### `doc.delete`

Hard-delete a document and all derived records.

**`DELETE /api/documents/{document_id}`**

### `doc.correct_field`

Human correction of an extracted field.

**`POST /api/documents/{document_id}/correct-field`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `field_name` | `string` | ✓ | Field Name |
| `corrected_value` | `string` | ✓ | Corrected Value |

### `doc.summarize`

Generate AI summary of a document.

**`POST /api/documents/{document_id}/summarize`**

## Email

### `email.fetch_new`

Check for new emails via IMAP.

**`POST /api/email/fetch`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mailbox` | `string` |  | Mailbox |

### `email.search`

Search emails by query, supplier, or address.

**`POST /api/email/search`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` |  | Query |
| `supplier_id` | `string` |  | Supplier Id |
| `email_address` | `string` |  | Email Address |
| `mailbox` | `string` |  | Mailbox |
| `limit` | `integer` |  | Limit |

### `email.list_threads`

List email threads.

**`GET /api/email/threads`**

### `email.get_thread`

Get thread with all messages.

**`GET /api/email/threads/{thread_id}`**

### `email.draft`

Create email draft.

**`POST /api/email/drafts`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `to_addresses` | `array` | ✓ | To Addresses |
| `cc_addresses` | `array` |  | Cc Addresses |
| `subject` | `string` | ✓ | Subject |
| `body_html` | `string` | ✓ | Body Html |
| `body_text` | `string` |  | Body Text |
| `thread_id` | `string` |  | Thread Id |
| `supplier_id` | `string` |  | Supplier Id |
| `context` | `object` |  | Context |

### `email.list_drafts`

List email drafts.

**`GET /api/email/drafts`**

### `email.style_match`

Analyze communication style with a counterparty.

**`POST /api/email/style-analyze`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `supplier_id` | `string` |  | Supplier Id |
| `email_address` | `string` |  | Email Address |
| `sample_count` | `integer` |  | Sample Count |

### `email.risk_check`

Check email draft for risks before sending.

**`POST /api/email/drafts/{draft_id}/risk-check`**

### `email.send` ⛔ **approval gate**

Send email draft via SMTP (approval gate).

**`POST /api/email/drafts/{draft_id}/send`**

### `email.suggest_template`

Suggest email template by context.

**`POST /api/email/suggest-template`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_type` | `string` | ✓ | Context Type |
| `supplier_id` | `string` |  | Supplier Id |
| `invoice_id` | `string` |  | Invoice Id |
| `language` | `string` |  | Language |

### `email.read`

Read email message with attachments.

**`GET /api/email/{email_id}`**

## Email Templates

### `email.templates.list`

List email templates with optional filters.

**`GET /api/email-templates/`**

### `email.templates.create`

Create a new email template.

**`POST /api/email-templates/`**

### `email.templates.get`

Get a template by ID.

**`GET /api/email-templates/{template_id}`**

### `email.templates.update`

Update a custom template.

**`PATCH /api/email-templates/{template_id}`**

### `email.templates.delete` ⛔ **approval gate**

Delete a custom template.

**`DELETE /api/email-templates/{template_id}`**

### `email.templates.from_message`

Create a template from an existing email.

**`POST /api/email-templates/from-message`**

### `email.templates.render`

Render a template with variable substitution.

**`POST /api/email-templates/{template_id}/render`**

## Graph

### `graph.node_create`

Create a graph memory node.

**`POST /api/graph/nodes`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `node_type` | `string` | ✓ | Node Type |
| `title` | `string` | ✓ | Title |
| `canonical_key` | `string` |  | Canonical Key |
| `entity_type` | `string` |  | Entity Type |
| `entity_id` | `string` |  | Entity Id |
| `summary` | `string` |  | Summary |
| `aliases` | `array` |  | Aliases |
| `confidence` | `number` |  | Confidence |
| *…+1 more* | | | |

### `graph.node_get`

Get a graph memory node.

**`GET /api/graph/nodes/{node_id}`**

### `graph.edge_create`

Link two graph memory nodes.

**`POST /api/graph/edges`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_node_id` | `string` | ✓ | Source Node Id |
| `target_node_id` | `string` | ✓ | Target Node Id |
| `edge_type` | `string` | ✓ | Edge Type |
| `confidence` | `number` |  | Confidence |
| `reason` | `string` |  | Reason |
| `source_document_id` | `string` |  | Source Document Id |
| `source_document_version_id` | `string` |  | Source Document Version Id |
| `evidence_span_id` | `string` |  | Evidence Span Id |
| *…+1 more* | | | |

### `graph.neighborhood`

Get connected graph memory around a node.

**`GET /api/graph/nodes/{node_id}/neighborhood`**

### `graph.path`

Find a short relationship path between two nodes.

**`GET /api/graph/path`**

### `graph.chunk_create`

Create a memory chunk for a document.

**`POST /api/graph/chunks`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` | ✓ | Document Id |
| `document_version_id` | `string` |  | Document Version Id |
| `chunk_index` | `integer` | ✓ | Chunk Index |
| `text` | `string` | ✓ | Text |
| `token_count` | `integer` |  | Token Count |
| `page_number` | `integer` |  | Page Number |
| `bbox_data` | `object` |  | Bbox Data |
| `embedding_id` | `string` |  | Embedding Id |
| *…+1 more* | | | |

### `graph.evidence_create`

Create source evidence span.

**`POST /api/graph/evidence`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` | ✓ | Document Id |
| `document_version_id` | `string` |  | Document Version Id |
| `chunk_id` | `string` |  | Chunk Id |
| `field_name` | `string` |  | Field Name |
| `text` | `string` | ✓ | Text |
| `page_number` | `integer` |  | Page Number |
| `bbox_data` | `object` |  | Bbox Data |
| `confidence` | `number` |  | Confidence |
| *…+1 more* | | | |

### `graph.mention_create`

Create a document entity mention.

**`POST /api/graph/mentions`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` | ✓ | Document Id |
| `document_version_id` | `string` |  | Document Version Id |
| `chunk_id` | `string` |  | Chunk Id |
| `node_id` | `string` |  | Node Id |
| `mention_text` | `string` | ✓ | Mention Text |
| `entity_type` | `string` | ✓ | Entity Type |
| `start_offset` | `integer` |  | Start Offset |
| `end_offset` | `integer` |  | End Offset |
| *…+4 more* | | | |

### `graph.review_list`

List graph memory links that need review.

**`GET /api/graph/review`**

### `graph.review_decide`

Approve or reject a graph memory suggestion.

**`POST /api/graph/review/{item_id}/decide`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `string` | ✓ | Action |
| `decided_by` | `string` |  | Decided By |
| `comment` | `string` |  | Comment |

## Invoices

### `invoice.list`

List invoices with filters.

**`GET /api/invoices`**

### `invoice.get`

Get invoice with lines.

**`GET /api/invoices/{invoice_id}`**

### `invoice.extract`

Re-run extraction on invoice's document.

**`POST /api/invoices/{invoice_id}/re-extract`**

### `invoice.validate`

Run arithmetic and format validation on invoice.

**`POST /api/invoices/{invoice_id}/validate`**

### `invoice.update`

Update invoice fields after human review.

**`PATCH /api/invoices/{invoice_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invoice_number` | `string` |  | Invoice Number |
| `invoice_date` | `string` |  | Invoice Date |
| `due_date` | `string` |  | Due Date |
| `validity_date` | `string` |  | Validity Date |
| `currency` | `string` |  | Currency |
| `subtotal` | `number` |  | Subtotal |
| `tax_amount` | `number` |  | Tax Amount |
| `total_amount` | `number` |  | Total Amount |
| *…+2 more* | | | |

### `invoice.approve` ⛔ **approval gate**

Approve invoice (approval gate).

**`POST /api/invoices/{invoice_id}/approve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `comment` | `string` |  | Comment |

### `invoice.reject` ⛔ **approval gate**

Reject invoice (approval gate).

**`POST /api/invoices/{invoice_id}/reject`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | `string` | ✓ | Reason |

### `invoice.compare_prices`

Compare line prices with previous invoices from same supplier.

**`GET /api/invoices/{invoice_id}/price-check`**

### `invoice.delete` ⛔ **approval gate**

Delete a single invoice and its lines.

**`DELETE /api/invoices/{invoice_id}`**

### `invoice.bulk_delete` ⛔ **approval gate**

Bulk delete invoices by ids list, filter, or all.

**`DELETE /api/invoices`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ids` | `array` |  | Ids |
| `status` | `?` |  |  |
| `supplier_id` | `string` |  | Supplier Id |
| `delete_all` | `boolean` |  | Delete All |

### `invoice.receive`

Create warehouse receipt from this invoice's lines.

**`POST /api/invoices/{invoice_id}/receive`**

## Mailboxes

### `mailbox.create` ⛔ **approval gate**

Add a new IMAP/SMTP mailbox configuration.

**`POST /api/mailbox/configs`**

### `mailbox.list`

List all configured mailboxes.

**`GET /api/mailbox/configs`**

### `mailbox.get`

Get a mailbox configuration by ID.

**`GET /api/mailbox/configs/{mailbox_id}`**

### `mailbox.update`

Update mailbox settings.

**`PATCH /api/mailbox/configs/{mailbox_id}`**

### `mailbox.delete` ⛔ **approval gate**

Remove a mailbox configuration.

**`DELETE /api/mailbox/configs/{mailbox_id}`**

### `mailbox.test` ⛔ **approval gate**

Test IMAP and SMTP connectivity.

**`POST /api/mailbox/configs/{mailbox_id}/test`**

## Memory

### `memory.search`

Search graph nodes, chunks, and evidence spans.

**`POST /api/memory/search`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | ✓ | Query |
| `node_types` | `array` |  | Node Types |
| `document_id` | `string` |  | Document Id |
| `scope` | `string` |  | Scope |
| `limit` | `integer` |  | Limit |
| `cursor` | `string` |  | Cursor |
| `intent` | `string` |  | Intent |
| `entity_hints` | `array` |  | Entity Hints |
| *…+3 more* | | | |

### `memory.prune`

Delete old episodic memory facts by scope and kind.

**`POST /api/memory/prune`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `scope` | `string` |  | Scope |
| `kinds` | `array` |  | Kinds |
| `older_than_days` | `integer` |  | Older Than Days |
| `dry_run` | `boolean` |  | Dry Run |

### `memory.explain`

Search memory and return evidence with graph context.

**`POST /api/memory/explain`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | ✓ | Query |
| `document_id` | `string` |  | Document Id |
| `node_types` | `array` |  | Node Types |
| `limit` | `integer` |  | Limit |
| `neighborhood_depth` | `integer` |  | Neighborhood Depth |
| `retrieval_mode` | `string` |  | Retrieval Mode |
| `include_explain` | `boolean` |  | Include Explain |

### `memory.embeddings_rebuild`

Prepare chunk/evidence embeddings for Qdrant.

**`POST /api/memory/embeddings/rebuild`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `content_types` | `array` |  | Content Types |
| `collection_name` | `string` |  | Collection Name |
| `embedding_model` | `string` |  | Embedding Model |
| `vector_size` | `integer` |  | Vector Size |
| `limit` | `integer` |  | Limit |
| `mark_stale_existing` | `boolean` |  | Mark Stale Existing |

### `memory.embeddings_stats`

Show active embedding profile and record statuses.

**`GET /api/memory/embeddings/stats`**

### `memory.embeddings_rebuild_active`

Rebuild records for active profile.

**`POST /api/memory/embeddings/rebuild-active`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `content_types` | `array` |  | Content Types |
| `collection_name` | `string` |  | Collection Name |
| `embedding_model` | `string` |  | Embedding Model |
| `vector_size` | `integer` |  | Vector Size |
| `limit` | `integer` |  | Limit |
| `mark_stale_existing` | `boolean` |  | Mark Stale Existing |

### `memory.embeddings_index_active`

Index queued/stale memory records into Qdrant.

**`POST /api/memory/embeddings/index-active`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `statuses` | `array` |  | Statuses |
| `limit` | `integer` |  | Limit |

### `memory.reindex`

Rebuild graph memory for existing documents.

**`POST /api/memory/reindex`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_ids` | `array` |  | Document Ids |
| `rebuild` | `boolean` |  | Rebuild |
| `limit` | `integer` |  | Limit |

## Normalization

### `norm.list_rules`

List normalization rules.

**`GET /api/normalization/rules`**

### `norm.suggest_rule`

Detect repeated human corrections and propose rules.

**`POST /api/normalization/suggest`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `field_name` | `string` |  | Field Name |
| `min_corrections` | `integer` |  | Minimum repeated corrections to suggest a rule |

### `norm.activate_rule` ⛔ **approval gate**

Activate a proposed rule (approval gate).

**`POST /api/normalization/rules/{rule_id}/activate`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `activated_by` | `string` |  | Activated By |

### `norm.apply_rules` ⛔ **approval gate**

Apply active normalization rules to a document's extraction.

**`POST /api/normalization/apply`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` | ✓ | Document Id |

### `norm.list_norm_cards`

List norm cards.

**`GET /api/normalization/norm-cards`**

### `norm.create_norm_card`

Create a norm card for a canonical item.

**`POST /api/normalization/norm-cards`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `canonical_item_id` | `string` | ✓ | Canonical Item Id |
| `norm_qty` | `number` | ✓ | Norm Qty |
| `unit` | `string` | ✓ | Unit |
| `product_code` | `string` |  | Product Code |
| `loss_factor` | `number` |  | Loss Factor |
| `valid_from` | `string` |  | Valid From |
| `valid_to` | `string` |  | Valid To |
| `approved_by` | `string` |  | Approved By |
| *…+1 more* | | | |

### `norm.update_norm_card`

Update norm card values.

**`PATCH /api/normalization/norm-cards/{card_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `norm_qty` | `number` |  | Norm Qty |
| `unit` | `string` |  | Unit |
| `product_code` | `string` |  | Product Code |
| `loss_factor` | `number` |  | Loss Factor |
| `valid_from` | `string` |  | Valid From |
| `valid_to` | `string` |  | Valid To |
| `approved_by` | `string` |  | Approved By |
| `notes` | `string` |  | Notes |

### `norm.list_canonical_items`

List canonical items.

**`GET /api/normalization/canonical-items`**

### `norm.get_canonical_item`

Get canonical item with classification fields.

**`GET /api/normalization/canonical-items/{item_id}`**

### `norm.update_canonical_item`

Update OKPD2, GOST, hazard class.

**`PATCH /api/normalization/canonical-items/{item_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `okpd2_code` | `string` |  | Okpd2 Code |
| `gost` | `string` |  | Gost |
| `hazard_class` | `string` |  | Hazard Class |

### `norm.get_item_norm_cards`

Get all norm cards for a canonical item.

**`GET /api/normalization/canonical-items/{item_id}/norm-cards`**

## NTD / Technology

### `ntd.control_settings_get`

Read NTD norm-control mode.

**`GET /api/settings/ntd-control`**

### `ntd.control_settings_update` ⛔ **approval gate**

Set manual or automatic NTD norm-control mode.

**`PATCH /api/settings/ntd-control`**

### `ntd.document_list`

List normative documents.

**`GET /api/ntd/documents`**

### `ntd.document_create`

Create a normative document record.

**`POST /api/ntd/documents`**

### `ntd.document_create_from_source`

Create and optionally index NTD from an uploaded document.

**`POST /api/ntd/documents/from-source`**

### `ntd.document_index`

Parse source document text into NTD clauses and requirements.

**`POST /api/ntd/documents/{normative_document_id}/index`**

### `ntd.clause_create`

Create a normative document clause.

**`POST /api/ntd/clauses`**

### `ntd.requirement_create`

Create a normative requirement.

**`POST /api/ntd/requirements`**

### `ntd.requirement_search`

Search SQL-first NTD requirements.

**`GET /api/ntd/requirements/search`**

### `ntd.norm_control_run` ⛔ **approval gate**

Check one document against applicable NTD.

**`POST /api/documents/{document_id}/ntd-check`**

### `ntd.check_availability`

Explain whether NTD check can run for a document.

**`GET /api/documents/{document_id}/ntd-check/availability`**

### `ntd.norm_control_run_payload` ⛔ **approval gate**

Check a document against applicable NTD.

**`POST /api/ntd/checks/run`**

### `ntd.check_list`

List NTD checks for a document.

**`GET /api/documents/{document_id}/ntd-checks`**

### `ntd.check_get`

Get NTD check details and findings.

**`GET /api/ntd/checks/{check_id}`**

### `ntd.finding_decide` ⛔ **approval gate**

Record a human decision for an NTD finding.

**`POST /api/ntd/checks/{check_id}/findings/{finding_id}/decide`**

## Payments

### `payment.list_schedule`

List payment schedules with filters.

**`GET /api/payment-schedules`**

### `payment.create_schedule`

Create payment schedule entry.

**`POST /api/payment-schedules`**

### `payment.overdue`

List overdue payments.

**`GET /api/payment-schedules/overdue`**

### `payment.upcoming`

List upcoming payments within N days.

**`GET /api/payment-schedules/upcoming`**

### `payment.mark_paid` ⛔ **approval gate**

Mark payment as paid (approval gate).

**`POST /api/payment-schedules/{schedule_id}/mark-paid`**

### `payment.schedule_from_invoice`

Create payment schedule from invoice due date and total.

**`POST /api/invoices/{invoice_id}/schedule-payment`**

## Procurement

### `procurement.list_requests`

List purchase requests.

**`GET /api/purchase-requests`**

### `procurement.create_request`

Create a purchase request.

**`POST /api/purchase-requests`**

### `procurement.get_request`

Get purchase request details.

**`GET /api/purchase-requests/{req_id}`**

### `procurement.update_request`

Update purchase request.

**`PATCH /api/purchase-requests/{req_id}`**

### `procurement.send_rfq` ⛔ **approval gate**

Generate RFQ draft emails to suppliers (approval gate).

**`POST /api/purchase-requests/{req_id}/send-rfq`**

### `procurement.list_contracts`

List supplier contracts.

**`GET /api/supplier-contracts`**

### `procurement.create_contract`

Create supplier contract.

**`POST /api/supplier-contracts`**

### `procurement.get_contract`

Get contract details.

**`GET /api/supplier-contracts/{contract_id}`**

### `procurement.update_contract`

Update contract details.

**`PATCH /api/supplier-contracts/{contract_id}`**

## Quarantine

### `quarantine.list`

List files waiting for quarantine review.

**`GET /api/quarantine`**

## Search & NL

### `doc.search`

Hybrid search: Postgres FTS + ILIKE fallback.

**`POST /api/search/documents`**

### `search.nl_to_query`

Convert natural language to structured query.

**`POST /api/search/nl`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | ✓ | Query |
| `limit` | `integer` |  | Limit |

### `search.nl`

Skill: search.nl_to_query — Convert natural language to structured query.

**`POST /api/search/nl`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | ✓ | Query |
| `limit` | `integer` |  | Limit |

### `search.hybrid`

Vector similarity search via Qdrant + SQL filter fallback.

**`POST /api/search/hybrid`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | ✓ | Query |
| `doc_type` | `string` |  | Doc Type |
| `status` | `string` |  | Status |
| `limit` | `integer` |  | Limit |

### `search.similar`

Find entities similar to the given entity using vector search.

**`GET /api/search/similar/{entity_type}/{entity_id}`**

## Suppliers

### `supplier.get`

Get supplier profile with aggregated stats.

**`GET /api/suppliers/{supplier_id}`**

### `supplier.list`

List suppliers/parties with trust score and invoice aggregates.

**`GET /api/suppliers`**

### `supplier.search`

Search suppliers by name, INN, or address.

**`POST /api/suppliers/search`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | ✓ | Query |
| `limit` | `integer` |  | Limit |

### `supplier.price_history`

Get price history for all items from this supplier.

**`GET /api/suppliers/{supplier_id}/price-history`**

### `supplier.check_requisites`

Validate supplier requisites.

**`POST /api/suppliers/{supplier_id}/check-requisites`**

### `supplier.trust_score`

Calculate supplier trust score.

**`GET /api/suppliers/{supplier_id}/trust-score`**

### `supplier.alerts`

Get alerts for a supplier.

**`GET /api/suppliers/{supplier_id}/alerts`**

### `supplier.update`

Update supplier details.

**`PATCH /api/suppliers/{supplier_id}`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` |  | Name |
| `inn` | `string` |  | Inn |
| `kpp` | `string` |  | Kpp |
| `ogrn` | `string` |  | Ogrn |
| `address` | `string` |  | Address |
| `contact_email` | `string` |  | Contact Email |
| `contact_phone` | `string` |  | Contact Phone |
| `bank_name` | `string` |  | Bank Name |
| *…+6 more* | | | |

### `supplier.list`

List all suppliers/parties.

**`GET /api/suppliers`**

## Tables & Export

### `table.query`

Query table with filters, sort, pagination.

**`POST /api/tables/query`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `table` | `string` |  | Table |
| `columns` | `array` |  | Columns |
| `filters` | `array` |  | Filters |
| `sort` | `array` |  | Sort |
| `search` | `string` |  | Search |
| `offset` | `integer` |  | Offset |
| `limit` | `integer` |  | Limit |

### `table.export_excel`

Export table to Excel/CSV.

**`POST /api/tables/export`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `table` | `string` |  | Table |
| `filters` | `array` |  | Filters |
| `columns` | `array` |  | Columns |
| `format` | `string` |  | Format |

### `table.export_1c`

Export invoices to 1С CommerceML XML format.

**`POST /api/tables/export-1c`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invoice_ids` | `array` |  | Invoice Ids |
| `filters` | `array` |  | Filters |
| `format` | `string` |  | Format |

### `table.list_views`

List saved table views.

**`GET /api/tables/views`**

### `table.create_view`

Create a saved table view.

**`POST /api/tables/views`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | ✓ | Name |
| `table` | `string` |  | Table |
| `columns` | `array` |  | Columns |
| `filters` | `array` |  | Filters |
| `sort` | `array` |  | Sort |
| `is_shared` | `boolean` |  | Is Shared |

### `table.delete_view`

Delete a saved view.

**`DELETE /api/tables/views/{view_id}`**

### `table.import_excel` ⛔ **approval gate**

Upload Excel, build diff for review.

**`POST /api/tables/import`**

### `table.apply_diff` ⛔ **approval gate**

Apply import diff rows to the database.

**`POST /api/tables/apply-diff`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `rows` | `array` | ✓ | Rows |

### `table.inline_edit`

Edit a single cell value.

**`POST /api/tables/inline-edit`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | `string` | ✓ | Entity Id |
| `field` | `string` | ✓ | Field |
| `value` | `string` | ✓ | Value |

### `table.batch_action`

Apply action to multiple invoices.

**`POST /api/tables/batch`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | `string` | ✓ | Action |
| `entity_ids` | `array` | ✓ | Entity Ids |
| `reason` | `string` |  | Reason |

## Technology Cards

### `tech.resource_list`

List machines, tools, fixtures, and equipment.

**`GET /api/technology/resources`**

### `tech.resource_create`

Create a manufacturing resource.

**`POST /api/technology/resources`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `resource_type` | `string` | ✓ | Resource Type |
| `name` | `string` | ✓ | Name |
| `code` | `string` |  | Code |
| `model` | `string` |  | Model |
| `standard` | `string` |  | Standard |
| `capabilities` | `object` |  | Capabilities |
| `location` | `string` |  | Location |
| `status` | `string` |  | Status |
| *…+2 more* | | | |

### `tech.operation_template_list`

List technology operation templates.

**`GET /api/technology/operation-templates`**

### `tech.operation_template_create`

Create an operation template.

**`POST /api/technology/operation-templates`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `operation_type` | `string` | ✓ | Operation Type |
| `name` | `string` | ✓ | Name |
| `standard_system` | `string` |  | Standard System |
| `default_operation_code` | `string` |  | Default Operation Code |
| `required_resource_types` | `array` |  | Required Resource Types |
| `default_transition_text` | `string` |  | Default Transition Text |
| `default_control_requirements` | `string` |  | Default Control Requirements |
| `default_safety_requirements` | `string` |  | Default Safety Requirements |
| *…+3 more* | | | |

### `tech.process_plan_list`

List manufacturing process plans.

**`GET /api/technology/process-plans`**

### `tech.process_plan_create`

Create a manufacturing process plan.

**`POST /api/technology/process-plans`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` |  | Document Id |
| `bom_id` | `string` |  | Bom Id |
| `product_name` | `string` | ✓ | Product Name |
| `product_code` | `string` |  | Product Code |
| `version` | `string` |  | Version |
| `status` | `string` |  | Status |
| `standard_system` | `string` |  | Standard System |
| `route_summary` | `string` |  | Route Summary |
| *…+5 more* | | | |

### `tech.process_plan_draft_from_document`

Draft process plan from memory.

**`POST /api/technology/process-plans/draft-from-document`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `document_id` | `string` | ✓ | Document Id |
| `product_name` | `string` |  | Product Name |
| `product_code` | `string` |  | Product Code |
| `created_by` | `string` |  | Created By |
| `rebuild_existing` | `boolean` |  | Rebuild Existing |

### `tech.process_plan_get`

Get process plan with operations and norms.

**`GET /api/technology/process-plans/{plan_id}`**

### `tech.process_plan_approve` ⛔ **approval gate**

Approve process plan after review.

**`POST /api/technology/process-plans/{plan_id}/approve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | `string` | ✓ | Approved By |
| `comment` | `string` |  | Comment |

### `tech.process_plan_validate`

Validate manufacturability and completeness.

**`POST /api/technology/process-plans/{plan_id}/validate`**

### `tech.norm_estimate_suggest`

Suggest operation time and cutting parameters.

**`POST /api/technology/process-plans/{plan_id}/estimate-norms`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `batch_size` | `number` |  | Batch Size |
| `overwrite_existing` | `boolean` |  | Overwrite Existing |
| `created_by` | `string` |  | Created By |

### `tech.operation_add`

Add a process operation and graph links.

**`POST /api/technology/process-plans/{plan_id}/operations`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sequence_no` | `integer` | ✓ | Sequence No |
| `operation_code` | `string` |  | Operation Code |
| `name` | `string` | ✓ | Name |
| `operation_type` | `string` |  | Operation Type |
| `machine_resource_id` | `string` |  | Machine Resource Id |
| `tool_resource_id` | `string` |  | Tool Resource Id |
| `fixture_resource_id` | `string` |  | Fixture Resource Id |
| `setup_description` | `string` |  | Setup Description |
| *…+8 more* | | | |

### `tech.norm_estimate_create`

Create labor and machine time estimate.

**`POST /api/technology/process-plans/{plan_id}/norm-estimates`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `operation_id` | `string` |  | Operation Id |
| `setup_minutes` | `number` |  | Setup Minutes |
| `machine_minutes` | `number` |  | Machine Minutes |
| `labor_minutes` | `number` |  | Labor Minutes |
| `batch_size` | `number` |  | Batch Size |
| `confidence` | `number` |  | Confidence |
| `method` | `string` |  | Method |
| `assumptions` | `array` |  | Assumptions |
| *…+2 more* | | | |

### `tech.norm_estimate_approve` ⛔ **approval gate**

Approve labor and machine time estimate.

**`POST /api/technology/norm-estimates/{estimate_id}/approve`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_by` | `string` | ✓ | Approved By |
| `comment` | `string` |  | Comment |

### `tech.correction_record`

Record a human correction for learning.

**`POST /api/technology/corrections`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | `string` | ✓ | Entity Type |
| `entity_id` | `string` | ✓ | Entity Id |
| `field_name` | `string` | ✓ | Field Name |
| `old_value` | `string` |  | Old Value |
| `new_value` | `string` |  | New Value |
| `correction_type` | `string` |  | Correction Type |
| `corrected_by` | `string` | ✓ | Corrected By |
| `reason` | `string` |  | Reason |
| *…+4 more* | | | |

### `tech.learning_suggest`

Suggest rules from repeated corrections.

**`GET /api/technology/learning-suggestions`**

### `tech.learning_rule_list`

List proposed and active learning rules.

**`GET /api/technology/learning-rules`**

### `tech.learning_rule_create`

Save a proposed learning rule.

**`POST /api/technology/learning-rules`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `rule_type` | `string` |  | Rule Type |
| `entity_type` | `string` | ✓ | Entity Type |
| `field_name` | `string` | ✓ | Field Name |
| `match_old_value` | `string` |  | Match Old Value |
| `replacement_value` | `string` |  | Replacement Value |
| `confidence` | `number` |  | Confidence |
| `occurrences` | `integer` |  | Occurrences |
| `status` | `string` |  | Status |
| *…+2 more* | | | |

### `tech.learning_rule_activate` ⛔ **approval gate**

Activate a proposed learning rule.

**`POST /api/technology/learning-rules/{rule_id}/activate`**

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `activated_by` | `string` | ✓ | Activated By |
| `comment` | `string` |  | Comment |

## Warehouse

### `warehouse.list_inventory`

List inventory items.

**`GET /api/warehouse/inventory`**

### `warehouse.create_item`

Create inventory position.

**`POST /api/warehouse/inventory`**

### `warehouse.low_stock`

Items below minimum quantity.

**`GET /api/warehouse/inventory/low-stock`**

### `warehouse.get_item`

Get item card with recent movements.

**`GET /api/warehouse/inventory/{item_id}`**

### `warehouse.update_item`

Update inventory item fields.

**`PATCH /api/warehouse/inventory/{item_id}`**

### `warehouse.delete_item` ⛔ **approval gate**

Delete inventory item with all movements (approval gate).

**`DELETE /api/warehouse/inventory/{item_id}`**

### `warehouse.issue_stock` ⛔ **approval gate**

Issue stock from warehouse (approval gate).

**`POST /api/warehouse/inventory/{item_id}/issue`**

### `warehouse.adjust_stock`

Adjust inventory quantity (±).

**`POST /api/warehouse/inventory/{item_id}/adjust`**

### `warehouse.list_movements`

List all stock movements with filters.

**`GET /api/warehouse/movements`**

### `warehouse.list_receipts`

List warehouse receipts.

**`GET /api/warehouse/receipts`**

### `warehouse.bulk_confirm`

Bulk accept pending/draft receipts.

**`POST /api/warehouse/receipts/bulk-confirm`**

### `warehouse.create_receipt`

Create receipt from invoice lines.

**`POST /api/warehouse/receipts`**

### `warehouse.get_receipt`

Get receipt with lines.

**`GET /api/warehouse/receipts/{receipt_id}`**

### `warehouse.confirm_receipt` ⛔ **approval gate**

Confirm receipt, update stock (approval gate).

**`POST /api/warehouse/receipts/{receipt_id}/confirm`**

### `warehouse.update_status`

Transition receipt status.

**`PATCH /api/warehouse/receipts/{receipt_id}/status`**

## Workspace

### `workspace.verify_block`

Verify that a block exists on the Workspace.

**`POST /api/workspace/agent/verify-block`**

### `workspace.invoice_table`

Build and publish the full invoice table.

**`POST /api/workspace/agent/invoices/table`**

### `workspace.invoice_items_table`

Build and publish invoice line items.

**`POST /api/workspace/agent/invoices/items-table`**

### `workspace.invoice_items_grouped_table`

Group invoice items by invoice.

**`POST /api/workspace/agent/invoices/items-grouped-table`**

### `workspace.invoice_items_by_supplier_table`

Group invoice items by supplier.

**`POST /api/workspace/agent/invoices/items-by-supplier-table`**

### `workspace.sql_table`

Build and publish a table using SQL-first pipeline.

**`POST /api/workspace/agent/generated/sql-table`**

### `workspace.general`

Publish any custom table or block to the Workspace.

**`POST /api/workspace/agent/generated/general`**

---

*Generated by `make skills`. Do not edit manually.*