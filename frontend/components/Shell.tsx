import Link from "next/link";
import type { ReactNode } from "react";

const navItems = [
  "Сегодня",
  "Кейсы",
  "Документы",
  "Чертежи",
  "Закупка",
  "Почта",
  "История",
];

export function Shell({ children, rail }: { children: ReactNode; rail?: ReactNode }) {
  return (
    <div className="app-shell">
      <div className="workspace-grid">
        <aside className="glass-panel sidebar">
          <Link className="brand-mark" href="/">
            <span className="brand-symbol">M</span>
            <span>
              <p className="eyebrow">AI Manufacturing</p>
              <h1 className="brand-title">Workspace</h1>
            </span>
          </Link>
          <nav className="nav-stack" aria-label="Главная навигация">
            {navItems.map((item, index) => (
              <span className="nav-item" data-active={index === 0} key={item}>
                {item}
              </span>
            ))}
          </nav>
        </aside>
        <main className="main-stage fade-in">{children}</main>
        <aside className="ai-rail">{rail ?? <SvetaPanel />}</aside>
      </div>
    </div>
  );
}

export function SvetaPanel({
  focus = "Соберу документы, найду риски, подготовлю следующий безопасный шаг.",
  actions = ["Smart ingest", "Найти дубли", "Сформировать вопросы"],
}: {
  focus?: string;
  actions?: string[];
}) {
  return (
    <section className="ai-panel">
      <h2 className="sveta-title">
        <span className="sveta-orb" />
        Света
      </h2>
      <p className="small-muted">{focus}</p>
      <div className="action-row" style={{ marginTop: 18 }}>
        {actions.map((action) => (
          <button className="ghost-button" key={action} type="button">
            {action}
          </button>
        ))}
      </div>
      <div className="command-hint">
        <span>Командная палитра</span>
        <span>
          <kbd className="kbd">Ctrl</kbd> + <kbd className="kbd">K</kbd>
        </span>
      </div>
    </section>
  );
}

export function EmptyState({ title, text }: { title: string; text: string }) {
  return (
    <div className="empty-state">
      <h3 className="section-title">{title}</h3>
      <p>{text}</p>
    </div>
  );
}
