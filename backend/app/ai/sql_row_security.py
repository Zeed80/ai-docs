"""E39: row-level security for the agent's SQL readers.

The agent's SQL (``workspace.sql_table``) runs as ``agent_sql_reader`` (seven
tables) or ``agent_sql_reader_full`` (every non-secret table). Table grants
answer "which tables"; these policies answer "which rows" — the same rules the
API applies (``app.domain.access``, ``app.domain.email_access``, memory
scopes), enforced by Postgres itself, so a query written by the model cannot
reach rows its user may not see.

Who the user is. A custom setting (``SET LOCAL app.user_sub``) would not do:
any role may call ``set_config``, so the model's own SQL could rewrite it.
Instead the pipeline inserts one row into ``agent_sql_actors`` keyed by the
transaction id *before* switching role; the readers have no grant on that
table, the policy functions read it as its owner, and the pipeline's rollback
removes the row. No row — the transaction sees only rows without an owner.

Two kinds of rules:

* **Company rules** (documents and what derives from them, cases): admin and
  manager see everything, others their own, their department subtree and
  rows with neither owner nor department.
* **Personal rules** (chats, work orders, memory ``owner:``, personal
  mailboxes, notifications): only the owner, for admins too — reading a
  colleague's private chat or mail is not an admin power (as in the API).

Every table with an owner or a link to one must be in ``RULES`` or
``COMPANY_WIDE``; ``tests/test_sql_row_security.py`` checks the coverage.
"""

from __future__ import annotations

import hashlib
import uuid

import structlog

logger = structlog.get_logger()

ACTOR_TABLE = "agent_sql_actors"
READER_ROLES = ("agent_sql_reader", "agent_sql_reader_full")
POLICY = "aiw_agent_read"

_DOC = "aiw_rls_document"

# table -> USING expression, evaluated as the reader for every row.
RULES: dict[str, str] = {
    # Documents and what is derived from them.
    "documents": "aiw_rls_can_see(owner_sub, department_id) OR aiw_rls_shared_type(doc_type::text)",
    "document_artifacts": f"{_DOC}(document_id)",
    "document_chunks": f"{_DOC}(document_id)",
    "document_extractions": f"{_DOC}(document_id)",
    "document_links": f"{_DOC}(document_id)",
    "document_processing_jobs": f"{_DOC}(document_id)",
    "document_versions": f"{_DOC}(document_id)",
    "evidence_spans": f"{_DOC}(document_id)",
    "entity_mentions": f"{_DOC}(document_id) AND aiw_rls_node(node_id)",
    "graph_build_statuses": f"{_DOC}(document_id)",
    "graph_review_items": f"{_DOC}(document_id)",
    "memory_embedding_records": f"{_DOC}(document_id)",
    "ntd_check_findings": f"{_DOC}(document_id)",
    "ntd_check_runs": f"{_DOC}(document_id)",
    "quarantine_entries": f"{_DOC}(document_id)",
    "supplier_contracts": f"{_DOC}(document_id)",
    "extraction_fields": "aiw_rls_extraction(extraction_id)",
    "knowledge_nodes": "aiw_rls_node(id)",
    "knowledge_edges": (
        f"aiw_rls_node(source_node_id) AND aiw_rls_node(target_node_id)"
        f" AND {_DOC}(source_document_id) AND aiw_rls_evidence(evidence_span_id)"
    ),
    # Drawings, BOMs, process plans: as their document.
    "drawings": f"{_DOC}(document_id)",
    "drawing_assembly_boms": "aiw_rls_drawing(drawing_id)",
    "drawing_feature_corrections": "aiw_rls_drawing(drawing_id)",
    "drawing_features": "aiw_rls_drawing(drawing_id)",
    "drawing_view_sections": "aiw_rls_drawing(drawing_id)",
    "drawing_tp_links": "aiw_rls_drawing(drawing_id)",
    "feature_contours": "aiw_rls_feature(feature_id)",
    "feature_dimensions": "aiw_rls_feature(feature_id)",
    "feature_gdt": "aiw_rls_feature(feature_id)",
    "feature_surfaces": "aiw_rls_feature(feature_id)",
    "feature_tool_bindings": "aiw_rls_feature(feature_id)",
    "boms": f"{_DOC}(document_id)",
    "bom_lines": "aiw_rls_bom(bom_id)",
    "manufacturing_process_plans": (
        f"{_DOC}(document_id) AND aiw_rls_drawing(drawing_id) AND aiw_rls_bom(bom_id)"
    ),
    "manufacturing_operations": "aiw_rls_process_plan(process_plan_id)",
    "blank_specs": "aiw_rls_process_plan(process_plan_id)",
    "gost_form_data": "aiw_rls_process_plan(process_plan_id)",
    "manufacturing_check_results": "aiw_rls_process_plan(process_plan_id)",
    "manufacturing_norm_estimates": "aiw_rls_process_plan(process_plan_id)",
    "normcontrol_checks": "aiw_rls_process_plan(process_plan_id)",
    "surface_machining_specs": "aiw_rls_process_plan(process_plan_id)",
    "technology_corrections": (
        f"aiw_rls_process_plan(process_plan_id) AND {_DOC}(source_document_id)"
        " AND aiw_rls_entity(entity_type, entity_id)"
    ),
    # Invoices: as their document.
    "invoices": f"{_DOC}(document_id)",
    "invoice_lines": "aiw_rls_invoice(invoice_id)",
    "payment_schedules": "aiw_rls_invoice(invoice_id)",
    "price_history_entries": "aiw_rls_invoice(invoice_id)",
    "warehouse_receipts": f"{_DOC}(document_id) AND aiw_rls_invoice(invoice_id)",
    "warehouse_receipt_lines": "aiw_rls_receipt(receipt_id)",
    # Rows about another entity: as that entity.
    "anomaly_cards": "aiw_rls_entity(entity_type, entity_id)",
    "approvals": "aiw_rls_entity(entity_type, entity_id)",
    "audit_logs": "aiw_rls_entity(entity_type, entity_id)",
    "audit_timeline_events": "aiw_rls_entity(entity_type, entity_id)",
    "comments": "aiw_rls_entity(entity_type, entity_id)",
    # An e-mail draft is its author's or its mailbox's (email_access.may_access_draft).
    "draft_actions": (
        "(action_type NOT LIKE 'email.%' OR coalesce("
        "draft_data::jsonb ->> 'created_by_sub' = aiw_rls_sub()"
        " OR (draft_data::jsonb ->> 'mailbox' IS NOT NULL"
        " AND aiw_rls_mailbox(draft_data::jsonb ->> 'mailbox')), false))"
        " AND aiw_rls_entity(entity_type, entity_id)"
    ),
    "export_jobs": "aiw_rls_entity(entity_type, entity_id)",
    "handovers": "aiw_rls_entity(entity_type, entity_id)",
    "engineering_projections": "aiw_rls_entity(entity_type, entity_id)",
    "calendar_events": (
        "aiw_rls_own_or_shared(user_id) AND aiw_rls_entity(entity_type, entity_id)"
    ),
    # Cases.
    "work_cases": "aiw_rls_case(id)",
    "case_members": "aiw_rls_case(case_id)",
    "case_documents": f"aiw_rls_case(case_id) AND {_DOC}(document_id)",
    "image_generations": (
        f"aiw_rls_own_or_shared(owner_sub) AND aiw_rls_case(case_id) AND {_DOC}(source_document_id)"
    ),
    # Mail: personal mailboxes belong to their owner only.
    "email_messages": "aiw_rls_mailbox(mailbox)",
    "email_threads": "aiw_rls_mailbox(mailbox)",
    "mailbox_folders": "aiw_rls_mailbox(mailbox)",
    "email_sync_ops": "aiw_rls_mailbox(mailbox)",
    "email_auto_replies": "aiw_rls_mailbox(mailbox)",
    "email_labels": "aiw_rls_mailbox(mailbox) AND aiw_rls_own_or_shared(owner_sub)",
    "email_rules": "aiw_rls_mailbox(mailbox) AND aiw_rls_own_or_shared(owner_sub)",
    "email_signatures": "aiw_rls_mailbox(mailbox) AND aiw_rls_own_or_shared(owner_sub)",
    "email_triage_results": "aiw_rls_mailbox(mailbox) AND aiw_rls_work_order(work_order_id)",
    "email_attachments": f"aiw_rls_email(message_id) AND {_DOC}(document_id)",
    "email_rule_logs": "aiw_rls_email(message_id)",
    "email_thread_labels": "aiw_rls_thread(thread_id)",
    "email_contacts": "aiw_rls_own_or_shared(owner_sub)",
    "email_templates": "aiw_rls_email(source_email_id)",
    # Memory: owner, own department, own sessions, project and global.
    "memory_facts": "aiw_rls_memory_scope(scope, metadata::jsonb ->> 'session_id')",
    # Chats.
    "chat_sessions": "aiw_rls_own(user_key)",
    "chat_messages": "aiw_rls_session(session_id)",
    "chat_message_attachments": f"aiw_rls_session(session_id) AND {_DOC}(document_id)",
    "agent_actions": "aiw_rls_session_text(session_id)",
    "message_ratings": "aiw_rls_session_text(session_id)",
    "rooms": "aiw_rls_room(id)",
    "room_members": "aiw_rls_room(room_id)",
    "room_messages": "aiw_rls_room(room_id)",
    "room_message_attachments": f"aiw_rls_room_message(message_id) AND {_DOC}(document_id)",
    # Durable work: the order's owner.
    "work_orders": "aiw_rls_own(owner_key)",
    "durable_chat_runs": "aiw_rls_own(owner_key)",
    "work_budget_ledgers": "aiw_rls_own(owner_key)",
    "action_receipts": "aiw_rls_own(owner_key)",
    "agent_channel_identities": "aiw_rls_own(owner_key)",
    "agent_crons": "aiw_rls_own(owner_key)",
    "agent_delegation_grants": "aiw_rls_own(owner_key)",
    "agent_outbox": "aiw_rls_own(owner_key)",
    "agent_script_runs": "aiw_rls_own(owner_key)",
    "archived_conversation_imports": "aiw_rls_own(owner_key)",
    "owned_workspace_blocks": "aiw_rls_own(owner_key)",
    "owned_workspace_block_versions": "aiw_rls_own(owner_key)",
    "verified_commit_decisions": "aiw_rls_own(owner_key)",
    "work_acceptance_criteria": "aiw_rls_work_order(work_order_id)",
    "work_artifacts": "aiw_rls_work_order(work_order_id)",
    "work_budget_reservations": "aiw_rls_work_order(work_order_id)",
    "work_effect_receipts": "aiw_rls_work_order(work_order_id)",
    "work_events": "aiw_rls_work_order(work_order_id)",
    "work_evidence": "aiw_rls_work_order(work_order_id)",
    "work_learnings": "aiw_rls_work_order(work_order_id)",
    "work_plans": "aiw_rls_work_order(work_order_id)",
    "work_steps": "aiw_rls_work_order(work_order_id)",
    "work_tool_calls": "aiw_rls_work_order(work_order_id)",
    "work_step_attempts": "aiw_rls_step(step_id)",
    "computer_use_actions": "aiw_rls_work_order(work_order_id)",
    "computer_use_grants": "aiw_rls_work_order(work_order_id)",
    "chat_logical_actions": "aiw_rls_work_order(work_order_id)",
    # Personal settings and lists.
    "notifications": "aiw_rls_own(user_sub)",
    "user_notification_prefs": "aiw_rls_own(user_sub)",
    "user_notification_settings": "aiw_rls_own(user_sub)",
    "proactive_task_feedback": "aiw_rls_own(user_sub)",
    "collections": "aiw_rls_own(user_id)",
    "collection_items": "aiw_rls_collection(collection_id)",
    "saved_queries": "aiw_rls_own(user_id)",
    "saved_views": "aiw_rls_own(user_id)",
    "snoozes": "aiw_rls_own(user_id)",
    "reminders": "aiw_rls_own_or_shared(user_id) AND aiw_rls_entity(entity_type, entity_id)",
    "comfyui_workflows": "aiw_rls_own_or_shared(owner_sub)",
    "lora_datasets": "aiw_rls_own_or_shared(owner_sub)",
    "lora_training_runs": "aiw_rls_own_or_shared(owner_sub)",
    "studio_jobs": "aiw_rls_own_or_shared(owner_sub)",
    "workspace_sheets": "aiw_rls_own_or_shared(owner_sub)",
}

# Tables with an owner-like column that are company data on purpose.
COMPANY_WIDE: dict[str, str] = {
    "users": "сотрудники и их отделы — справочник предприятия",
    "departments": "оргструктура",
    "tool_catalog_entries": "каталог поставщика — справочник; API каталога не фильтрует по владельцу",
    "catalog_pages": "страницы каталога поставщика, как сам каталог",
    "normative_documents": "НТД предприятия",
    "normative_document_versions": "НТД предприятия",
    "auto_approval_rules": "правила компании, created_by — автор правила",
    "recipe_skills": "рецепты агента общие; created_by — автор",
    "source_connectors": "подключения источников настраивает администратор",
    "model_assignment_revisions": "журнал назначения моделей",
    "engineering_change_requests": "инженерный контур — общий для КБ",
    "engineering_revisions": "инженерный контур — общий для КБ",
    "cad_ir_revisions": "инженерный контур — общий для КБ",
    "agent_tasks": "реестр задач отдела ИИ",
    "purchase_requests": "закупочные заявки — общий процесс",
    "stock_movements": "склад — общий",
}

_FUNCTIONS = f"""
CREATE TABLE IF NOT EXISTS {ACTOR_TABLE} (
    xid xid8 PRIMARY KEY,
    user_sub text,
    unrestricted boolean NOT NULL DEFAULT false,
    department_id uuid,
    departments uuid[] NOT NULL DEFAULT '{{}}'
);
REVOKE ALL ON {ACTOR_TABLE} FROM PUBLIC;

CREATE OR REPLACE FUNCTION aiw_rls_sub() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT a.user_sub FROM {ACTOR_TABLE} a WHERE a.xid = pg_current_xact_id_if_assigned()
$$;
CREATE OR REPLACE FUNCTION aiw_rls_unrestricted() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT a.unrestricted FROM {ACTOR_TABLE} a
                    WHERE a.xid = pg_current_xact_id_if_assigned()), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_departments() RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT a.departments FROM {ACTOR_TABLE} a
                    WHERE a.xid = pg_current_xact_id_if_assigned()), '{{}}'::uuid[])
$$;
CREATE OR REPLACE FUNCTION aiw_rls_department() RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT a.department_id FROM {ACTOR_TABLE} a WHERE a.xid = pg_current_xact_id_if_assigned()
$$;

CREATE OR REPLACE FUNCTION aiw_rls_can_see(owner text, dept uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT CASE WHEN owner IS NULL AND dept IS NULL THEN true
              ELSE coalesce(aiw_rls_unrestricted()
                            OR owner = aiw_rls_sub()
                            OR dept = ANY(aiw_rls_departments()), false) END
$$;
CREATE OR REPLACE FUNCTION aiw_rls_own(owner text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce(owner = aiw_rls_sub(), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_own_or_shared(owner text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT owner IS NULL OR aiw_rls_own(owner)
$$;

CREATE OR REPLACE FUNCTION aiw_rls_shared_type(kind text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  -- app.domain.access.SHARED_DOCUMENT_TYPES: company reference material.
  SELECT coalesce(kind IN ('supplier_catalog'), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_document(doc uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT doc IS NULL OR coalesce((SELECT aiw_rls_can_see(d.owner_sub, d.department_id)
                                         OR aiw_rls_shared_type(d.doc_type::text)
                                    FROM documents d WHERE d.id = doc), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_invoice(inv uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT inv IS NULL OR coalesce((SELECT aiw_rls_document(i.document_id)
                                    FROM invoices i WHERE i.id = inv), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_receipt(rid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT rid IS NULL OR coalesce((SELECT aiw_rls_document(r.document_id)
                                         AND aiw_rls_invoice(r.invoice_id)
                                    FROM warehouse_receipts r WHERE r.id = rid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_extraction(eid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT eid IS NULL OR coalesce((SELECT aiw_rls_document(e.document_id)
                                    FROM document_extractions e WHERE e.id = eid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_drawing(did uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT did IS NULL OR coalesce((SELECT aiw_rls_document(d.document_id)
                                    FROM drawings d WHERE d.id = did), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_feature(fid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT fid IS NULL OR coalesce((SELECT aiw_rls_drawing(f.drawing_id)
                                    FROM drawing_features f WHERE f.id = fid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_bom(bid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT bid IS NULL OR coalesce((SELECT aiw_rls_document(b.document_id)
                                    FROM boms b WHERE b.id = bid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_process_plan(pid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT pid IS NULL OR coalesce((SELECT aiw_rls_document(p.document_id)
                                         AND aiw_rls_drawing(p.drawing_id)
                                         AND aiw_rls_bom(p.bom_id)
                                    FROM manufacturing_process_plans p WHERE p.id = pid), false)
$$;

CREATE OR REPLACE FUNCTION aiw_rls_mailbox(box text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  -- A personal mailbox: only its owner, and only after they let the agent
  -- read it (sweep_enabled), as app.domain.email_access does for the agent.
  SELECT box IS NULL OR NOT EXISTS (
    SELECT 1 FROM mailbox_configs m
     WHERE m.name = box AND m.mailbox_type = 'personal'
       AND NOT coalesce(m.owner_sub = aiw_rls_sub() AND m.sweep_enabled, false))
$$;
CREATE OR REPLACE FUNCTION aiw_rls_email(mid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT mid IS NULL OR coalesce((SELECT aiw_rls_mailbox(e.mailbox)
                                    FROM email_messages e WHERE e.id = mid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_thread(tid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT tid IS NULL OR coalesce((SELECT aiw_rls_mailbox(t.mailbox)
                                    FROM email_threads t WHERE t.id = tid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_mailbox_id(bid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT bid IS NULL OR coalesce((SELECT aiw_rls_mailbox(m.name)
                                    FROM mailbox_configs m WHERE m.id = bid), false)
$$;

CREATE OR REPLACE FUNCTION aiw_rls_session(sid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT aiw_rls_own(s.user_key) FROM chat_sessions s WHERE s.id = sid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_session_text(sid text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT aiw_rls_own(s.user_key) FROM chat_sessions s
                    WHERE s.id::text = sid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_work_order(wid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT wid IS NULL OR coalesce((SELECT aiw_rls_own(w.owner_key)
                                    FROM work_orders w WHERE w.id = wid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_step(sid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT aiw_rls_work_order(s.work_order_id)
                     FROM work_steps s WHERE s.id = sid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_case(cid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT cid IS NULL OR coalesce((
    SELECT aiw_rls_can_see(c.created_by, c.department_id)
           OR EXISTS (SELECT 1 FROM case_members m
                       WHERE m.case_id = c.id AND m.user_sub = aiw_rls_sub())
      FROM work_cases c WHERE c.id = cid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_room(rid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce(EXISTS (SELECT 1 FROM room_members m
                           WHERE m.room_id = rid AND m.user_sub = aiw_rls_sub())
                  OR EXISTS (SELECT 1 FROM rooms r
                              WHERE r.id = rid AND r.created_by = aiw_rls_sub()), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_room_message(mid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT aiw_rls_room(m.room_id) FROM room_messages m WHERE m.id = mid), false)
$$;
CREATE OR REPLACE FUNCTION aiw_rls_collection(cid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT coalesce((SELECT aiw_rls_own(c.user_id) FROM collections c WHERE c.id = cid), false)
$$;

CREATE OR REPLACE FUNCTION aiw_rls_memory_scope(scope text, session_id text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT CASE
    WHEN scope IN ('project', 'global') THEN true
    WHEN scope = 'session' THEN aiw_rls_session_text(session_id)
    WHEN scope LIKE 'session:%' THEN aiw_rls_session_text(substr(scope, 9))
    WHEN scope LIKE 'owner:%' THEN aiw_rls_own(substr(scope, 7))
    WHEN scope LIKE 'department:%'
      THEN coalesce(substr(scope, 12) = aiw_rls_department()::text, false)
    ELSE false END
$$;

CREATE OR REPLACE FUNCTION aiw_rls_entity(kind text, eid uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  inner_kind text;
  inner_id uuid;
BEGIN
  IF kind IS NULL OR eid IS NULL THEN
    RETURN true;
  END IF;
  CASE lower(kind)
    WHEN 'document' THEN RETURN aiw_rls_document(eid);
    WHEN 'invoice' THEN RETURN aiw_rls_invoice(eid);
    WHEN 'payment_schedule' THEN
      RETURN coalesce((SELECT aiw_rls_invoice(p.invoice_id)
                         FROM payment_schedules p WHERE p.id = eid), false);
    WHEN 'warehouse_receipt' THEN RETURN aiw_rls_receipt(eid);
    WHEN 'email', 'email_message' THEN
      IF EXISTS (SELECT 1 FROM email_messages e WHERE e.id = eid) THEN
        RETURN aiw_rls_email(eid);
      END IF;
      -- An approval to send refers to its draft (email_access.may_access_draft).
      RETURN coalesce((
        SELECT d.draft_data::jsonb ->> 'created_by_sub' = aiw_rls_sub()
               OR (d.draft_data::jsonb ->> 'mailbox' IS NOT NULL
                   AND aiw_rls_mailbox(d.draft_data::jsonb ->> 'mailbox'))
          FROM draft_actions d WHERE d.id = eid), false);
    WHEN 'email_thread' THEN RETURN aiw_rls_thread(eid);
    WHEN 'mailbox' THEN RETURN aiw_rls_mailbox_id(eid);
    WHEN 'drawing' THEN RETURN aiw_rls_drawing(eid);
    WHEN 'bom' THEN RETURN aiw_rls_bom(eid);
    WHEN 'manufacturing_process_plan' THEN RETURN aiw_rls_process_plan(eid);
    WHEN 'case', 'work_case' THEN RETURN aiw_rls_case(eid);
    WHEN 'work_order' THEN RETURN aiw_rls_work_order(eid);
    WHEN 'collection' THEN RETURN aiw_rls_collection(eid);
    WHEN 'delegation' THEN
      RETURN coalesce((SELECT aiw_rls_own(g.owner_key)
                         FROM agent_delegation_grants g WHERE g.id = eid), false);
    WHEN 'anomaly' THEN
      SELECT a.entity_type, a.entity_id INTO inner_kind, inner_id
        FROM anomaly_cards a WHERE a.id = eid;
      IF NOT FOUND THEN RETURN false; END IF;
      IF lower(coalesce(inner_kind, '')) IN ('anomaly', 'approval') THEN RETURN false; END IF;
      RETURN aiw_rls_entity(inner_kind, inner_id);
    WHEN 'approval' THEN
      SELECT a.entity_type, a.entity_id INTO inner_kind, inner_id
        FROM approvals a WHERE a.id = eid;
      IF NOT FOUND THEN RETURN false; END IF;
      IF lower(coalesce(inner_kind, '')) IN ('anomaly', 'approval') THEN RETURN false; END IF;
      RETURN aiw_rls_entity(inner_kind, inner_id);
    ELSE
      -- Suppliers, catalog entries, engineering objects: company data.
      RETURN true;
  END CASE;
END
$$;

CREATE OR REPLACE FUNCTION aiw_rls_evidence(sid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT sid IS NULL OR coalesce((SELECT aiw_rls_document(e.document_id)
                                    FROM evidence_spans e WHERE e.id = sid), false)
$$;

CREATE OR REPLACE FUNCTION aiw_rls_node(nid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  -- E40, as app.domain.graph_access: a node of a document or an entity follows
  -- them; a node built only from mentions is visible through at least one
  -- visible document that mentions it, or is company data when none does.
  SELECT nid IS NULL OR coalesce((
    SELECT CASE
      WHEN n.source_document_id IS NOT NULL OR n.entity_id IS NOT NULL
        THEN aiw_rls_document(n.source_document_id)
             AND aiw_rls_entity(n.entity_type, n.entity_id)
      ELSE (coalesce(n.metadata::jsonb ->> 'method', '') <> 'deterministic_regex'
            AND NOT EXISTS (
             SELECT 1 FROM entity_mentions m WHERE m.node_id = n.id AND m.document_id IS NOT NULL
             UNION ALL
             SELECT 1 FROM knowledge_edges k
              WHERE (k.source_node_id = n.id OR k.target_node_id = n.id)
                AND k.source_document_id IS NOT NULL))
        OR EXISTS (
             SELECT 1 FROM entity_mentions m
              WHERE m.node_id = n.id AND aiw_rls_document(m.document_id)
                AND m.document_id IS NOT NULL
             UNION ALL
             SELECT 1 FROM knowledge_edges k
              WHERE (k.source_node_id = n.id OR k.target_node_id = n.id)
                AND k.source_document_id IS NOT NULL
                AND aiw_rls_document(k.source_document_id))
    END
    FROM knowledge_nodes n WHERE n.id = nid), false)
$$;
"""


def _policy_statements(existing: set[str]) -> list[str]:
    roles = ", ".join(READER_ROLES)
    statements: list[str] = []
    for table, using in sorted(RULES.items()):
        if table not in existing:
            continue
        statements += [
            f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY',
            f'DROP POLICY IF EXISTS {POLICY} ON public."{table}"',
            f'CREATE POLICY {POLICY} ON public."{table}" FOR SELECT TO {roles} USING ({using})',
        ]
    return statements


def _role_statements() -> list[str]:
    statements: list[str] = []
    for role in READER_ROLES:
        statements += [
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} NOLOGIN; END IF; END $$;",
            f"GRANT {role} TO CURRENT_USER",
        ]
    return statements


def row_security_version() -> str:
    """Changes whenever the functions or the rules change."""
    payload = _FUNCTIONS + repr(sorted(RULES.items()))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def install_row_security(db, *, force: bool = False) -> bool:
    """Create the actor table, the functions and the policies; idempotent.

    Skipped when the installed version (a comment on ``aiw_rls_sub``) matches
    and every ruled table that exists has its policy, so startup does not take
    table locks on every boot, yet a table a later migration created gets its
    policy. Returns whether anything was installed. The caller commits.
    """
    from sqlalchemy import text

    version = row_security_version()
    existing = set(
        (
            await db.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
        ).scalars()
    )
    ruled = existing & set(RULES)
    if not force:
        installed = (
            await db.execute(
                text(
                    "SELECT obj_description(p.oid, 'pg_proc') FROM pg_proc p "
                    "WHERE p.proname = 'aiw_rls_sub'"
                )
            )
        ).scalar()
        covered = set(
            (
                await db.execute(
                    text(
                        "SELECT tablename FROM pg_policies "
                        "WHERE schemaname = 'public' AND policyname = :name"
                    ),
                    {"name": POLICY},
                )
            ).scalars()
        )
        if installed == version and ruled <= covered:
            return False
    await db.execute(text("SET LOCAL lock_timeout = '10s'"))
    for statement in _role_statements():
        await db.execute(text(statement))
    # The seven-table reader's grants (migration 20260905_0001), repeated
    # here: a database created from metadata never ran that migration, and
    # without its role the pipeline refuses to query.
    from app.ai.table_sql_pipeline import _READER_ROLE, ALLOWED_TABLES

    await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {_READER_ROLE}"))
    for table in sorted(ALLOWED_TABLES & existing):
        await db.execute(text(f'GRANT SELECT ON public."{table}" TO {_READER_ROLE}'))
    # asyncpg runs one statement per execute.
    for statement in _split(_FUNCTIONS):
        await db.execute(text(statement))
    for statement in _policy_statements(existing):
        await db.execute(text(statement))
    await db.execute(text(f"COMMENT ON FUNCTION aiw_rls_sub() IS '{version}'"))
    logger.info("sql_row_security_installed", version=version, tables=len(ruled))
    return True


def _split(block: str) -> list[str]:
    """Split on ``;`` at line end outside ``$$`` bodies."""
    statements, current, in_body = [], [], False
    for line in block.strip().splitlines():
        current.append(line)
        in_body ^= line.count("$$") % 2 == 1
        if not in_body and line.rstrip().endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


async def bind_actor(db, actor_sub: str | None) -> None:
    """Record who this transaction reads for; call BEFORE ``SET LOCAL ROLE``.

    The row lives only in this transaction (the pipeline rolls back). No
    actor, or one unknown to ``users``, binds nothing: only rows without an
    owner stay visible.
    """
    from sqlalchemy import select, text

    from app.db.models import User
    from app.domain.org import get_department_descendants

    if not actor_sub:
        return
    user = await db.scalar(select(User).where(User.sub == actor_sub, User.is_active))
    if user is None:
        await db.execute(
            text(f"INSERT INTO {ACTOR_TABLE} (xid, user_sub) VALUES (pg_current_xact_id(), :sub)"),
            {"sub": actor_sub},
        )
        return
    departments: set[uuid.UUID] = set()
    if user.department_id is not None:
        departments = await get_department_descendants(db, user.department_id)
    await db.execute(
        text(
            f"INSERT INTO {ACTOR_TABLE} (xid, user_sub, unrestricted, department_id, departments) "
            "VALUES (pg_current_xact_id(), :sub, :unrestricted, :dept, CAST(:depts AS text[])::uuid[])"
        ),
        {
            "sub": actor_sub,
            "unrestricted": user.role in {"admin", "manager"},
            "dept": user.department_id,
            "depts": [str(d) for d in sorted(departments, key=str)],
        },
    )
