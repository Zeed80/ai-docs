"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

/**
 * Руководство по ручной части настройки почтового сервера.
 *
 * Кнопка «Развернуть Mailcow» делает всё, что можно сделать за оператора
 * (контейнеры, Traefik-роут, сертификат на почтовых портах). Здесь собрано
 * ровно то, что автоматизировать нельзя — DNS чужой зоны, фаервол хостера,
 * DKIM (ключ генерируется только после создания домена) и API-ключ Mailcow.
 * Подстановка реального домена делает инструкцию копипастной.
 */
function Section({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <h2 className="text-base font-semibold">
        <span className="text-muted-foreground mr-2">{n}.</span>
        {title}
      </h2>
      <div className="space-y-2 text-sm">{children}</div>
    </section>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <pre className="text-xs bg-muted rounded p-3 overflow-x-auto whitespace-pre-wrap">
      {children}
    </pre>
  );
}

function GuideContent() {
  const [mailDomain, setMailDomain] = useState("mail.example.com");
  const [baseDomain, setBaseDomain] = useState("example.com");

  useEffect(() => {
    // Домен берём из фактической конфигурации, чтобы команды можно было
    // копировать без правок; пока не настроено — остаётся example.com.
    fetch(`${API}/api/admin/mail-server/deploy/status`, {
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const domain: string | null =
          d?.job?.mail_domain ?? d?.suggested_domain ?? null;
        if (domain) {
          setMailDomain(domain);
          const parts = domain.split(".");
          setBaseDomain(parts.length > 2 ? parts.slice(1).join(".") : domain);
        }
      })
      .catch(() => undefined);

    fetch(`${API}/api/admin/integrations/mail-server`, {
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.mail_domain) setBaseDomain(d.mail_domain);
      })
      .catch(() => undefined);
  }, []);

  return (
    <div className="max-w-3xl space-y-6 pb-10">
      <header className="space-y-2">
        <h1 className="text-lg font-semibold">
          Настройка Mailcow: ручные шаги
        </h1>
        <p className="text-sm text-muted-foreground">
          Кнопка «Развернуть Mailcow» поднимает контейнеры, подключает вебмейл к
          нашему Traefik и кладёт TLS-сертификат на почтовые порты. Ниже — то,
          что за вас сделать нельзя: записи в чужой DNS-зоне, правила фаервола у
          хостера, DKIM (ключ появляется только после создания домена) и
          API-ключ Mailcow.
        </p>
        <p className="text-xs text-muted-foreground">
          Консольный вариант того же чеклиста —{" "}
          <code className="font-mono">infra/installer/mailcow.README</code>.
        </p>
      </header>

      <Section n={1} title="DNS — до развёртывания">
        <p>
          Записи должны существовать <strong>до</strong> нажатия «Развернуть»:
          без A-записи Let&apos;s Encrypt не выдаст сертификат, и шаг с
          сертификатом завершится предупреждением.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-xs border border-border">
            <thead className="bg-muted">
              <tr>
                <th className="text-left p-2 border-b border-border">Тип</th>
                <th className="text-left p-2 border-b border-border">Имя</th>
                <th className="text-left p-2 border-b border-border">
                  Значение
                </th>
              </tr>
            </thead>
            <tbody className="font-mono">
              <tr>
                <td className="p-2 border-b border-border">A</td>
                <td className="p-2 border-b border-border">{mailDomain}</td>
                <td className="p-2 border-b border-border">
                  публичный IP сервера
                </td>
              </tr>
              <tr>
                <td className="p-2 border-b border-border">MX</td>
                <td className="p-2 border-b border-border">{baseDomain}</td>
                <td className="p-2 border-b border-border">
                  {mailDomain}, приоритет 10
                </td>
              </tr>
              <tr>
                <td className="p-2 border-b border-border">PTR</td>
                <td className="p-2 border-b border-border">
                  обратная зона (у хостера)
                </td>
                <td className="p-2 border-b border-border">
                  {mailDomain} для IP сервера
                </td>
              </tr>
              <tr>
                <td className="p-2 border-b border-border">TXT (SPF)</td>
                <td className="p-2 border-b border-border">{baseDomain}</td>
                <td className="p-2 border-b border-border">
                  v=spf1 mx a:{mailDomain} -all
                </td>
              </tr>
              <tr>
                <td className="p-2 border-b border-border">TXT (DMARC)</td>
                <td className="p-2 border-b border-border">
                  _dmarc.{baseDomain}
                </td>
                <td className="p-2 border-b border-border">
                  v=DMARC1; p=quarantine; rua=mailto:postmaster@{baseDomain}
                </td>
              </tr>
              <tr>
                <td className="p-2 border-b border-border">A/CNAME</td>
                <td className="p-2 border-b border-border">
                  autoconfig.{baseDomain}, autodiscover.{baseDomain}
                </td>
                <td className="p-2 border-b border-border">
                  IP сервера — автонастройка Outlook/Thunderbird
                </td>
              </tr>
              <tr>
                <td className="p-2">TXT (DKIM)</td>
                <td className="p-2">dkim._domainkey.{baseDomain}</td>
                <td className="p-2">ключ из Mailcow — появится на шаге 4</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="text-xs text-amber-600">
          Уточните у хостинг-провайдера, что исходящий порт 25 не заблокирован —
          у облачных VPS это частое ограничение, и без него письма не уйдут
          наружу.
        </p>
      </Section>

      <Section n={2} title="Порты в фаерволе хоста">
        <p>
          Traefik проксирует только HTTPS для вебмейла. Почтовые протоколы
          Mailcow публикует на хосте сам, поэтому их нужно открыть:
        </p>
        <Code>{`sudo ufw allow 25/tcp    # SMTP (приём почты извне)
sudo ufw allow 465/tcp   # SMTPS (отправка из клиентов)
sudo ufw allow 587/tcp   # Submission
sudo ufw allow 993/tcp   # IMAPS
sudo ufw allow 143/tcp   # IMAP STARTTLS
sudo ufw allow 110/tcp   # POP3   (можно не открывать, если не нужен)
sudo ufw allow 995/tcp   # POP3S  (можно не открывать, если не нужен)`}</Code>
      </Section>

      <Section n={3} title="Домен и первый ящик в Mailcow">
        <p>
          Откройте{" "}
          <a
            href={`https://${mailDomain}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:underline font-mono"
          >
            https://{mailDomain}
          </a>{" "}
          и войдите админом Mailcow (логин и пароль сгенерированы установщиком в{" "}
          <code className="font-mono">infra/mailcow/mailcow.conf</code>, при
          первом входе смените их). Затем{" "}
          <em>Configuration → Mail setup → Domains → Add domain</em> — заведите{" "}
          <span className="font-mono">{baseDomain}</span>.
        </p>
        <p className="text-xs text-muted-foreground">
          Личные ящики сотрудникам после этого выдаются из нашей админки
          (Пользователи → карточка → Корпоративная почта), заводить их в Mailcow
          вручную не нужно.
        </p>
      </Section>

      <Section n={4} title="DKIM — опубликовать выданный ключ">
        <p>
          Ключ генерируется только после создания домена, поэтому этот шаг —
          после шага 3. В Mailcow: <em>Configuration → ARC/DKIM keys</em> →
          скопируйте TXT-запись и опубликуйте её как{" "}
          <span className="font-mono">dkim._domainkey.{baseDomain}</span>.
        </p>
        <p>
          Проверить доставляемость целиком (SPF + DKIM + DMARC + PTR) удобно
          через mail-tester.com: отправьте письмо с нового ящика на выданный им
          адрес и убедитесь, что все проверки зелёные.
        </p>
      </Section>

      <Section n={5} title="API-ключ для нашей админки">
        <p>
          <em>Configuration → Access → Edit administrator details → API</em>:
          создайте ключ с правами <strong>Read-Write</strong>.
        </p>
        <p className="text-amber-600">
          Обязательно внесите в белый список IP этого ключа адрес/подсеть
          контейнера backend — иначе Mailcow отвечает 401/403 на полностью
          валидный ключ. Это самая частая причина ошибки на кнопке «Сохранить и
          проверить». Узнать адрес:
        </p>
        <Code>{`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' infra-backend-1
# или разрешите всю docker-сеть, например 172.16.0.0/12`}</Code>
        <p>
          Затем на странице{" "}
          <a
            href="/admin/integrations"
            className="text-primary hover:underline"
          >
            Интеграции
          </a>{" "}
          заполните блок «Почтовый сервер (Mailcow)» и нажмите «Сохранить и
          проверить».
        </p>
      </Section>

      <Section n={6} title="Автообновление сертификата и проверка обновлений">
        <p>
          Сертификат Let&apos;s Encrypt продлевает Traefik, но Postfix/Dovecot
          читают его из своей папки — копирование выполняет отдельный таймер.
          Включите его один раз:
        </p>
        <Code>{`sudo cp infra/installer/mailcow-certdump.service /etc/systemd/system/
sudo cp infra/installer/mailcow-certdump.timer   /etc/systemd/system/
sudo cp infra/installer/mailcow-update-check.service /etc/systemd/system/
sudo cp infra/installer/mailcow-update-check.timer   /etc/systemd/system/
# в обоих .service поправьте WorkingDirectory= на путь к репозиторию
sudo systemctl daemon-reload
sudo systemctl enable --now mailcow-certdump.timer mailcow-update-check.timer`}</Code>
        <p className="text-xs text-muted-foreground">
          Без первого таймера почтовые клиенты перестанут подключаться примерно
          через 90 дней — когда Traefik обновит сертификат, а Mailcow останется
          со старым.
        </p>
      </Section>

      <Section n={7} title="Проверка">
        <Code>{`# сертификат на почтовых портах должен быть от Let's Encrypt, не самоподписанный
openssl s_client -starttls smtp -connect ${mailDomain}:587 -servername ${mailDomain} </dev/null 2>/dev/null | openssl x509 -noout -issuer
openssl s_client -connect ${mailDomain}:993 -servername ${mailDomain} </dev/null 2>/dev/null | openssl x509 -noout -issuer`}</Code>
        <ul className="list-disc pl-5 space-y-1 text-sm">
          <li>Вебмейл открывается по https без предупреждений.</li>
          <li>Письмо с внешнего адреса доходит до нового ящика и обратно.</li>
          <li>
            Почтовый клиент настраивается вводом только адреса и пароля
            (autodiscover).
          </li>
          <li>mail-tester.com: SPF, DKIM, DMARC, PTR — зелёные.</li>
          <li>
            «Сохранить и проверить» в Интеграциях отвечает «подключение
            работает».
          </li>
        </ul>
      </Section>

      <Section n={8} title="Обновление и резервные копии">
        <p>
          Обновление Mailcow запускается из консоли, где под рукой откат: бэкап
          → переключение тега → health-check → автоматический откат при неудаче.
        </p>
        <Code>{`infra/installer/update-mailcow.sh --check   # что установлено и что вышло
infra/installer/update-mailcow.sh --yes     # обновить`}</Code>
        <p className="text-xs text-muted-foreground">
          Данные Mailcow попадают в общий бэкап автоматически. Учтите объём:
          почтовые ящики всех сотрудников архивируются целиком в каждый бэкап,
          поэтому для ежедневного расписания разумен{" "}
          <code className="font-mono">backup.sh --skip-mailcow</code>, а почту
          бэкапить отдельно и реже.
        </p>
      </Section>
    </div>
  );
}

export default function MailcowGuidePage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <GuideContent />
    </ProtectedRoute>
  );
}
