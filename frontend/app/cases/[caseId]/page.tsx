import { notFound } from "next/navigation";
import { ApprovalActions, DocumentActions, ScenarioActions, TaskActions, UploadCard } from "../../../components/CaseForms";
import { EmptyState, Shell, SvetaPanel } from "../../../components/Shell";
import { getCaseBundle, type ApprovalGate, type AuditEvent, type TaskJob, type WorkspaceDocument } from "../../../lib/api";

const tabs = ["обзор", "документы", "чертеж", "техпроцесс", "инструмент", "нормы", "письма", "закупка", "история"];

export default async function CasePage({ params }: { params: Promise<{ caseId: string }> }) {
  const { caseId } = await params;
  const bundle = await safeBundle(caseId);
  if (!bundle) {
    notFound();
  }

  const invoices = bundle.documents.filter((item) => item.document_type === "invoice");
  const drawings = bundle.documents.filter((item) => item.document_type === "drawing");

  return (
    <Shell
      rail={
        <SvetaPanel
          focus={`Кейс "${bundle.item.title}": вижу ${bundle.documents.length} документов и ${bundle.audit.length} audit events. Следующий безопасный шаг — обработать новые файлы и подсветить риски.`}
          actions={["Process all", "Invoice risks", "Draft email"]}
        />
      }
    >
      <section className="glass-panel case-header">
        <div>
          <p className="eyebrow">ManufacturingCase</p>
          <h1 className="hero-title">{bundle.item.title}</h1>
          <p className="hero-copy">
            {bundle.item.description || "Кейс готов к наполнению документами, чертежами, письмами и закупкой."}
          </p>
        </div>
        <div className="tabs">
          {tabs.map((tab, index) => (
            <span className="tab-link" data-active={index === 0} key={tab}>
              {tab}
            </span>
          ))}
        </div>
      </section>

      <section className="metric-grid">
        <Metric label="документов" value={bundle.documents.length} />
        <Metric label="счетов" value={invoices.length} />
        <Metric label="чертежей" value={drawings.length} />
        <Metric label="approvals" value={bundle.approvals.length} />
        <Metric label="audit events" value={bundle.audit.length} />
      </section>

      <section className="case-grid">
        <div className="content-card">
          <p className="eyebrow">Документы</p>
          <h2 className="section-title">Входящие и обработка</h2>
          {bundle.documents.length ? (
            <div style={{ marginTop: 14 }}>
              {bundle.documents.map((document) => (
                <DocumentRow document={document} key={document.id} />
              ))}
            </div>
          ) : (
            <EmptyState title="Документов пока нет" text="Загрузите PDF, JPG, STEP, XLSX или текстовый файл." />
          )}
        </div>
        <UploadCard caseId={bundle.item.id} />
      </section>

      <section className="case-grid">
        <Timeline audit={bundle.audit} />
        <ApprovalPanel approvals={bundle.approvals} />
      </section>

      <section className="case-grid">
        <TaskPanel tasks={bundle.tasks} />
        <AgentPanel caseId={bundle.item.id} />
      </section>

      <section className="case-grid">
        <ProcurementPanel documents={bundle.documents} />
        <AnomalyPanel documents={bundle.documents} />
      </section>
    </Shell>
  );
}

function DocumentRow({ document }: { document: WorkspaceDocument }) {
  return (
    <div className="document-row">
      <div>
        <h3 className="case-title" style={{ fontSize: 23 }}>
          {document.filename}
        </h3>
        <p className="case-meta">
          {formatBytes(document.size_bytes)} · {document.document_type ?? "unknown"} · {document.status}
        </p>
        {document.status === "suspicious" ? (
          <p className="small-muted">Файл помещен в quarantine. Обработка заблокирована до ручной проверки.</p>
        ) : null}
        {document.ai_summary ? <p className="small-muted">{document.ai_summary}</p> : null}
      </div>
      <div style={{ display: "grid", gap: 10, justifyItems: "end" }}>
        <span className="status-pill">{document.status}</span>
        <DocumentActions document={document} />
      </div>
    </div>
  );
}

function ApprovalPanel({ approvals }: { approvals: ApprovalGate[] }) {
  return (
    <div className="content-card">
      <p className="eyebrow">Approval cockpit</p>
      <h2 className="section-title">Согласования</h2>
      <div className="timeline">
        {approvals.length ? (
          approvals.map((gate) => (
            <div className="timeline-item" key={gate.id}>
              <strong>{gate.gate_type}</strong>
              <span className="small-muted">{gate.reason}</span>
              <br />
              <span className="case-meta">{gate.status}</span>
              <ApprovalActions gate={gate} />
            </div>
          ))
        ) : (
          <EmptyState title="Нет pending approvals" text="Здесь появятся отправка писем, 1С export и внешние действия." />
        )}
      </div>
    </div>
  );
}

function TaskPanel({ tasks }: { tasks: TaskJob[] }) {
  return (
    <div className="content-card">
      <p className="eyebrow">Очередь</p>
      <h2 className="section-title">Task jobs</h2>
      <div className="timeline">
        {tasks.length ? (
          tasks.slice(0, 8).map((task) => (
            <div className="timeline-item" key={task.id}>
              <strong>{task.task_type}</strong>
              <span className="small-muted">{task.error_message ?? task.status}</span>
              <br />
              <span className="case-meta">{new Date(task.created_at).toLocaleString("ru-RU")}</span>
              <TaskActions task={task} />
            </div>
          ))
        ) : (
          <EmptyState title="Очередь пуста" text="Задачи появятся после запуска processing или approval." />
        )}
      </div>
    </div>
  );
}

function AgentPanel({ caseId }: { caseId: string }) {
  return (
    <div className="content-card">
      <p className="eyebrow">AiAgent</p>
      <h2 className="section-title">Сценарии</h2>
      <p className="small-muted">
        Запуск сценария создает allowlisted agent actions, задачи или approval gates. Внешние действия остаются
        заблокированными до согласования.
      </p>
      <ScenarioActions caseId={caseId} />
    </div>
  );
}

function Timeline({ audit }: { audit: AuditEvent[] }) {
  return (
    <div className="content-card">
      <p className="eyebrow">История</p>
      <h2 className="section-title">Audit timeline</h2>
      <div className="timeline">
        {audit.length ? (
          audit.slice(0, 8).map((event) => (
            <div className="timeline-item" key={event.id}>
              <strong>{event.event_type}</strong>
              <span className="small-muted">{event.message}</span>
              <br />
              <span className="case-meta">
                {event.actor} · {new Date(event.created_at).toLocaleString("ru-RU")}
              </span>
            </div>
          ))
        ) : (
          <EmptyState title="Audit пуст" text="События появятся после загрузки и обработки документов." />
        )}
      </div>
    </div>
  );
}

function ProcurementPanel({ documents }: { documents: WorkspaceDocument[] }) {
  const invoiceDocs = documents.filter((item) => item.document_type === "invoice");
  return (
    <div className="content-card">
      <p className="eyebrow">Закупка</p>
      <h2 className="section-title">Счета и поставщики</h2>
      <p className="small-muted">
        Здесь показываются документы, которые pipeline классифицировал как счета. Anomaly card, дубли и Excel/1C export
        создаются backend actions.
      </p>
      <div className="timeline">
        {invoiceDocs.length ? (
          invoiceDocs.map((item) => (
            <div className="timeline-item" key={item.id}>
              <strong>{item.filename}</strong>
              <span className="small-muted">{item.ai_summary ?? "готов к invoice extraction"}</span>
            </div>
          ))
        ) : (
          <EmptyState title="Счетов нет" text="Загрузите PDF/JPG счета или запустите extraction на документе." />
        )}
      </div>
    </div>
  );
}

function AnomalyPanel({ documents }: { documents: WorkspaceDocument[] }) {
  const riskyDocuments = documents.filter((item) => item.status === "suspicious" || item.document_type === "invoice");
  return (
    <div className="content-card">
      <p className="eyebrow">Риски</p>
      <h2 className="section-title">Anomaly cards</h2>
      <div className="timeline">
        {riskyDocuments.length ? (
          riskyDocuments.map((item) => (
            <div className="timeline-item" key={item.id}>
              <strong>{item.filename}</strong>
              <span className="small-muted">
                {item.status === "suspicious"
                  ? "Quarantine: обработка заблокирована"
                  : item.ai_summary ?? "Invoice anomaly card доступна после extraction"}
              </span>
            </div>
          ))
        ) : (
          <EmptyState title="Аномалий нет" text="Риски появятся после invoice extraction или quarantine." />
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="metric-card">
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
    </div>
  );
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function safeBundle(caseId: string) {
  try {
    return await getCaseBundle(caseId);
  } catch {
    return null;
  }
}
