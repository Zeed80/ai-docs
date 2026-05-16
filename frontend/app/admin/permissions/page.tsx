"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

const ROLE_LABELS: Record<string, string> = {
  admin: "Администратор",
  manager: "Менеджер",
  accountant: "Бухгалтер",
  buyer: "Закупщик",
  engineer: "Инженер",
  viewer: "Наблюдатель",
};

function PermissionsContent() {
  const [matrix, setMatrix] = useState<Record<string, string[]> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/admin/permissions`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => setMatrix(d.matrix))
      .catch((e) => setError(e.message));
  }, []);

  if (error) return <p className="text-sm text-destructive">Ошибка: {error}</p>;
  if (!matrix)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;

  const allPerms = [...new Set(Object.values(matrix).flat())].sort();
  const roles = Object.keys(matrix);

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Матрица только для просмотра. Для изменения групп используйте Authentik.
      </p>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead className="bg-muted">
            <tr>
              <th className="px-3 py-2 text-left text-muted-foreground font-medium">
                Разрешение
              </th>
              {roles.map((r) => (
                <th
                  key={r}
                  className="px-3 py-2 text-center text-muted-foreground font-medium whitespace-nowrap"
                >
                  {ROLE_LABELS[r] ?? r}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {allPerms.map((perm) => (
              <tr key={perm} className="hover:bg-muted/30">
                <td className="px-3 py-1.5 font-mono text-muted-foreground">
                  {perm}
                </td>
                {roles.map((r) => {
                  const perms = matrix[r] ?? [];
                  const granted = perms.includes("*") || perms.includes(perm);
                  return (
                    <td key={r} className="px-3 py-1.5 text-center">
                      {granted ? (
                        <span className="text-green-600 font-bold">✓</span>
                      ) : (
                        <span className="text-muted-foreground/30">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AdminPermissionsPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <PermissionsContent />
    </ProtectedRoute>
  );
}
