"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface Supplier {
  id: string;
  name: string;
  inn: string | null;
  role: string;
  contact_email: string | null;
  contact_phone: string | null;
}

export default function SuppliersPage() {
  const router = useRouter();
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        if (search.length >= 2) {
          const resp = await fetch(`${API}/api/suppliers/search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: search }),
          });
          const data = await resp.json();
          setSuppliers(data.results);
        } else {
          const resp = await fetch(`${API}/api/suppliers`);
          setSuppliers(await resp.json());
        }
      } catch {
        setSuppliers([]);
      } finally {
        setLoading(false);
      }
    };
    const timeout = setTimeout(fetchData, 300);
    return () => clearTimeout(timeout);
  }, [search]);

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <h1 className="text-xl font-bold mb-4">Поставщики</h1>

      <input
        type="text"
        placeholder="Поиск по имени, ИНН..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        className="w-full px-4 py-2 mb-4 bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded-lg outline-none focus:border-blue-400"
      />

      {loading ? (
        <div className="text-slate-400 py-8 text-center">Загрузка...</div>
      ) : suppliers.length === 0 ? (
        <div className="text-slate-400 py-8 text-center">Нет поставщиков</div>
      ) : (
        <div className="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-700/50 text-slate-400 text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-2">Название</th>
                <th className="text-left px-4 py-2">ИНН</th>
                <th className="text-left px-4 py-2">Email</th>
                <th className="text-left px-4 py-2">Телефон</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700">
              {suppliers.map((s) => (
                <tr
                  key={s.id}
                  className="cursor-pointer hover:bg-slate-700/50"
                  onClick={() => router.push(`/suppliers/${s.id}`)}
                >
                  <td className="px-4 py-2.5 font-medium">{s.name}</td>
                  <td className="px-4 py-2.5 text-slate-400 font-mono text-xs">
                    {s.inn ?? "—"}
                  </td>
                  <td className="px-4 py-2.5 text-slate-400">
                    {s.contact_email ?? "—"}
                  </td>
                  <td className="px-4 py-2.5 text-slate-400">
                    {s.contact_phone ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
