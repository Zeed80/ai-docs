const DB_NAME = "ai-docs-offline";
const STORE = "upload-queue";
const DB_VERSION = 1;

interface QueuedUpload {
  id: string;
  filename: string;
  mime: string;
  data: ArrayBuffer;
  queuedAt: number;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(STORE, { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function enqueueUpload(file: File): Promise<string> {
  const db = await openDb();
  const data = await file.arrayBuffer();
  const entry: QueuedUpload = {
    id: crypto.randomUUID(),
    filename: file.name,
    mime: file.type,
    data,
    queuedAt: Date.now(),
  };
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(entry);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
  return entry.id;
}

export async function getQueuedUploads(): Promise<
  Omit<QueuedUpload, "data">[]
> {
  const db = await openDb();
  const items = await new Promise<QueuedUpload[]>((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result as QueuedUpload[]);
    req.onerror = () => reject(req.error);
  });
  db.close();
  return items.map(({ data: _data, ...rest }) => rest);
}

export async function flushQueue(
  uploadFn: (entry: QueuedUpload) => Promise<void>,
): Promise<{ flushed: number; failed: number }> {
  const db = await openDb();
  const items = await new Promise<QueuedUpload[]>((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result as QueuedUpload[]);
    req.onerror = () => reject(req.error);
  });

  let flushed = 0;
  let failed = 0;

  for (const item of items) {
    try {
      await uploadFn(item);
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        tx.objectStore(STORE).delete(item.id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
      flushed++;
    } catch {
      failed++;
    }
  }

  db.close();
  return { flushed, failed };
}

export async function removeFromQueue(id: string): Promise<void> {
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}
