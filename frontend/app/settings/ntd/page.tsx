"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface NormativeDocument {
  id: string;
  code: string;
  title: string;
  document_type: string;
  status: string;
  scope: string | null;
}

interface NormativeRequirement {
  id: string;
  normative_document_id: string;
  requirement_code: string;
  requirement_type: string;
  text: string;
  required_keywords: string[] | null;
  severity: string;
  is_active: boolean;
}

export default function NtdSettingsPage() {
  const [documents, setDocuments] = useState<NormativeDocument[]>([]);
  const [requirements, setRequirements] = useState<NormativeRequirement[]>([]);
  const [query, setQuery] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [docForm, setDocForm] = useState({
    code: "",
    title: "",
    document_type: "ГОСТ",
    version: "current",
    status: "active",
    scope: "",
  });
  const [sourceForm, setSourceForm] = useState({
    source_document_id: "",
    requirement_type: "generic",
    index_immediately: true,
  });
  const [requirementForm, setRequirementForm] = useState({
    normative_document_id: "",
    requirement_code: "",
    requirement_type: "generic",
    text: "",
    required_keywords: "",
    severity: "warning",
  });

  async function loadDocuments() {
    const res = await fetch(`${API}/api/ntd/documents`).catch(() => null);
    if (!res?.ok) {
      setDocuments([]);
      return;
    }
    const data = await res.json();
    setDocuments(data);
    setRequirementForm((prev) => ({
      ...prev,
      normative_document_id: prev.normative_document_id || data[0]?.id || "",
    }));
  }

  async function searchRequirements(search = query) {
    const q = search.trim() || " ";
    const res = await fetch(
      `${API}/api/ntd/requirements/search?query=${encodeURIComponent(q)}&limit=50`,
    ).catch(() => null);
    if (!res?.ok) {
      setRequirements([]);
      return;
    }
    const data = await res.json();
    setRequirements(data.requirements ?? []);
  }

  useEffect(() => {
    loadDocuments();
    searchRequirements(" ");
  }, []);

  async function createDocument() {
    setMessage(null);
    const res = await fetch(`${API}/api/ntd/documents`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...docForm,
        scope: docForm.scope || null,
      }),
    });
    if (!res.ok) {
      setMessage("Не удалось создать НТД");
      return;
    }
    const created = await res.json();
    setMessage(`Добавлен НТД ${created.code}`);
    setDocForm({ code: "", title: "", document_type: "ГОСТ", version: "current", status: "active", scope: "" });
    await loadDocuments();
  }

  async function createFromSource() {
    setMessage(null);
    const res = await fetch(`${API}/api/ntd/documents/from-source`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...sourceForm,
        actor: "user",
      }),
    });
    if (!res.ok) {
      setMessage("Не удалось создать НТД из документа");
      return;
    }
    const data = await res.json();
    const index = data.index_result;
    setMessage(
      index
        ? `Создан ${data.normative_document.code}: пунктов ${index.clauses_created}, требований ${index.requirements_created}`
        : `Создан ${data.normative_document.code}`,
    );
    setSourceForm({ source_document_id: "", requirement_type: "generic", index_immediately: true });
    await loadDocuments();
    await searchRequirements();
  }

  async function uploadNtdFile(file: File | null) {
    if (!file) return;
    setUploading(true);
    setMessage(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const ingest = await fetch(`${API}/api/documents/ingest?source_channel=ntd`, {
        method: "POST",
        body: form,
      });
      const ingested = await ingest.json();
      const documentId = ingested.id ?? ingested.document_id;
      if (!ingest.ok || !documentId) {
        setMessage("Не удалось загрузить НТД");
        return;
      }
      const create = await fetch(`${API}/api/ntd/documents/from-source`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_document_id: documentId,
          requirement_type: "generic",
          index_immediately: true,
          actor: "user",
        }),
      });
      if (!create.ok) {
        setSourceForm((prev) => ({ ...prev, source_document_id: documentId }));
        setMessage(`Файл загружен как документ ${documentId}. Дождитесь обработки и создайте НТД из этого ID.`);
        return;
      }
      const data = await create.json();
      const index = data.index_result;
      setMessage(
        index
          ? `Загружен и создан ${data.normative_document.code}: пунктов ${index.clauses_created}, требований ${index.requirements_created}`
          : `Загружен и создан ${data.normative_document.code}`,
      );
      await loadDocuments();
      await searchRequirements();
    } finally {
      setUploading(false);
    }
  }

  async function createRequirement() {
    setMessage(null);
    const keywords = requirementForm.required_keywords
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const res = await fetch(`${API}/api/ntd/requirements`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...requirementForm,
        required_keywords: keywords,
        applies_to: [requirementForm.requirement_type],
      }),
    });
    if (!res.ok) {
      setMessage("Не удалось создать требование");
      return;
    }
    const created = await res.json();
    setMessage(`Добавлено требование ${created.requirement_code}`);
    setRequirementForm((prev) => ({
      ...prev,
      requirement_code: "",
      text: "",
      required_keywords: "",
    }));
    await searchRequirements();
  }

  async function indexDocument(documentId: string) {
    setMessage(null);
    const res = await fetch(`${API}/api/ntd/documents/${documentId}/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requirement_type: "generic",
        replace_existing: false,
        actor: "user",
      }),
    });
    if (!res.ok) {
      setMessage("Не удалось индексировать НТД");
      return;
    }
    const data = await res.json();
    setMessage(`Индексация: пунктов ${data.clauses_created}, требований ${data.requirements_created}`);
    await searchRequirements();
  }

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold">НТД</h1>
        <p className="mt-1 text-sm text-slate-400">
          SQL-база нормативных документов, пунктов и требований для нормоконтроля.
        </p>
      </div>

      {message && (
        <div className="rounded-md border border-slate-700 bg-slate-800 p-3 text-sm text-slate-200">
          {message}
        </div>
      )}

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-700 bg-slate-800 p-5 lg:col-span-2">
          <h2 className="text-lg font-semibold">Загрузить НТД PDF/DOCX/TXT</h2>
          <p className="mt-1 text-sm text-slate-400">
            Файл проходит общий pipeline документов. Если текст уже доступен, карточка НТД создается и индексируется сразу.
          </p>
          <label className="mt-4 flex cursor-pointer items-center justify-center rounded-lg border border-dashed border-slate-600 bg-slate-900/40 px-4 py-8 text-sm text-slate-300 hover:border-blue-500">
            <input
              type="file"
              className="hidden"
              accept=".pdf,.docx,.txt,.md"
              disabled={uploading}
              onChange={(e) => uploadNtdFile(e.target.files?.[0] ?? null)}
            />
            {uploading ? "Загружаю..." : "Выберите файл НТД"}
          </label>
        </div>

        <div className="rounded-lg border border-slate-700 bg-slate-800 p-5">
          <h2 className="text-lg font-semibold">Добавить НТД</h2>
          <div className="mt-4 grid grid-cols-1 gap-3">
            <input className="input" placeholder="Код, например ГОСТ 3.1105-2011" value={docForm.code} onChange={(e) => setDocForm({ ...docForm, code: e.target.value })} />
            <input className="input" placeholder="Название" value={docForm.title} onChange={(e) => setDocForm({ ...docForm, title: e.target.value })} />
            <div className="grid grid-cols-2 gap-3">
              <input className="input" placeholder="Тип" value={docForm.document_type} onChange={(e) => setDocForm({ ...docForm, document_type: e.target.value })} />
              <input className="input" placeholder="Версия" value={docForm.version} onChange={(e) => setDocForm({ ...docForm, version: e.target.value })} />
            </div>
            <textarea className="input min-h-20" placeholder="Область применения" value={docForm.scope} onChange={(e) => setDocForm({ ...docForm, scope: e.target.value })} />
            <button className="btn-primary" onClick={createDocument} disabled={!docForm.code || !docForm.title}>
              Добавить НТД
            </button>
          </div>
        </div>

        <div className="rounded-lg border border-slate-700 bg-slate-800 p-5">
          <h2 className="text-lg font-semibold">Добавить требование</h2>
          <div className="mt-4 grid grid-cols-1 gap-3">
            <select className="input" value={requirementForm.normative_document_id} onChange={(e) => setRequirementForm({ ...requirementForm, normative_document_id: e.target.value })}>
              <option value="">Выберите НТД</option>
              {documents.map((doc) => (
                <option key={doc.id} value={doc.id}>{doc.code}</option>
              ))}
            </select>
            <div className="grid grid-cols-2 gap-3">
              <input className="input" placeholder="Код требования" value={requirementForm.requirement_code} onChange={(e) => setRequirementForm({ ...requirementForm, requirement_code: e.target.value })} />
              <input className="input" placeholder="Тип: process_plan, drawing..." value={requirementForm.requirement_type} onChange={(e) => setRequirementForm({ ...requirementForm, requirement_type: e.target.value })} />
            </div>
            <textarea className="input min-h-20" placeholder="Текст требования" value={requirementForm.text} onChange={(e) => setRequirementForm({ ...requirementForm, text: e.target.value })} />
            <input className="input" placeholder="Обязательные слова через запятую" value={requirementForm.required_keywords} onChange={(e) => setRequirementForm({ ...requirementForm, required_keywords: e.target.value })} />
            <select className="input" value={requirementForm.severity} onChange={(e) => setRequirementForm({ ...requirementForm, severity: e.target.value })}>
              <option value="info">info</option>
              <option value="warning">warning</option>
              <option value="error">error</option>
              <option value="critical">critical</option>
            </select>
            <button className="btn-primary" onClick={createRequirement} disabled={!requirementForm.normative_document_id || !requirementForm.requirement_code || !requirementForm.text}>
              Добавить требование
            </button>
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-5">
        <h2 className="text-lg font-semibold">Создать НТД из загруженного документа</h2>
        <p className="mt-1 text-sm text-slate-400">
          Используйте ID документа, который уже загружен и обработан. Код, тип и версия определяются по тексту.
        </p>
        <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-[1fr_220px_auto]">
          <input
            className="input"
            placeholder="Document ID"
            value={sourceForm.source_document_id}
            onChange={(e) => setSourceForm({ ...sourceForm, source_document_id: e.target.value })}
          />
          <input
            className="input"
            placeholder="Тип требований"
            value={sourceForm.requirement_type}
            onChange={(e) => setSourceForm({ ...sourceForm, requirement_type: e.target.value })}
          />
          <button className="btn-primary" onClick={createFromSource} disabled={!sourceForm.source_document_id}>
            Создать и индексировать
          </button>
        </div>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-5">
        <h2 className="text-lg font-semibold">Нормативные документы</h2>
        <div className="mt-4 divide-y divide-slate-700">
          {documents.map((doc) => (
            <div key={doc.id} className="flex items-center justify-between gap-3 py-3">
              <div className="min-w-0">
                <p className="font-mono text-sm text-slate-100">{doc.code}</p>
                <p className="mt-1 truncate text-sm text-slate-400">{doc.title}</p>
              </div>
              <button className="btn-secondary shrink-0" onClick={() => indexDocument(doc.id)}>
                Индексировать
              </button>
            </div>
          ))}
          {!documents.length && (
            <p className="py-6 text-center text-sm text-slate-500">НТД не добавлены</p>
          )}
        </div>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-5">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Поиск требований</h2>
          <div className="flex gap-2">
            <input className="input w-80" placeholder="Поиск по коду или тексту" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && searchRequirements()} />
            <button className="btn-secondary" onClick={() => searchRequirements()}>Найти</button>
          </div>
        </div>
        <div className="mt-4 divide-y divide-slate-700">
          {requirements.map((item) => (
            <div key={item.id} className="py-3">
              <div className="flex items-center justify-between gap-3">
                <p className="font-mono text-sm text-slate-100">{item.requirement_code}</p>
                <span className="rounded bg-slate-900 px-2 py-0.5 text-xs text-slate-300">{item.severity}</span>
              </div>
              <p className="mt-1 text-sm text-slate-300">{item.text}</p>
              {item.required_keywords?.length ? (
                <p className="mt-1 text-xs text-slate-500">Ключевые слова: {item.required_keywords.join(", ")}</p>
              ) : null}
            </div>
          ))}
          {!requirements.length && (
            <p className="py-6 text-center text-sm text-slate-500">Требований не найдено</p>
          )}
        </div>
      </section>

      <style jsx>{`
        .input {
          border-radius: 0.375rem;
          border: 1px solid rgb(71 85 105);
          background: rgb(15 23 42);
          padding: 0.5rem 0.75rem;
          font-size: 0.875rem;
          color: rgb(226 232 240);
          outline: none;
        }
        .input:focus {
          border-color: rgb(59 130 246);
        }
        .btn-primary,
        .btn-secondary {
          border-radius: 0.375rem;
          padding: 0.5rem 0.75rem;
          font-size: 0.875rem;
          font-weight: 500;
        }
        .btn-primary {
          background: rgb(37 99 235);
          color: white;
        }
        .btn-secondary {
          background: rgb(51 65 85);
          color: rgb(241 245 249);
        }
        .btn-primary:disabled {
          opacity: 0.5;
        }
      `}</style>
    </div>
  );
}
