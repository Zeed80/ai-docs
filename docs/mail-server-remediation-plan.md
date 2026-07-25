# План доработки почтового сервера (Mailcow) и личных ящиков

Статус на 2026-07-24 (план выполнен, кроме 4.5 — см. «Что осталось»).
База — коммит `800a245` «Add self-hosted mail server
integration…». Документ фиксирует, что сломано, что уже исправлено и в каком
порядке доделывать остальное.

Обозначения: **[✔]** — сделано в рамках агентской доработки (см. раздел 0),
**[ ]** — к работе.

---

## 0. Что уже сделано (агентский контур)

| Изменение | Файлы |
|---|---|
| Личность «от чьего имени работает агент» (ContextVar → `X-Acting-User`) | `backend/app/ai/actor_context.py`, `ai/agent_loop.py`, `api/agent.py`, `api/capability_router.py` |
| Резолвер эффективного пользователя для service-вызовов | `backend/app/auth/acting.py` |
| Приватность личных ящиков на уровне API (list/get/search/read/delete/attachments) | `backend/app/domain/email_access.py`, `api/email.py` |
| Тот же скоуп для spec-таблиц (источник `emails` — SQL в обход API) | `backend/app/domain/table_spec.py`, `api/spec_tables.py` |
| Починены 6 из 13 действий capability `email` (битые пути) + добавлены `my_mailbox`, `process_attachment` | `backend/app/api/capability_router.py`, `aiagent/skills/capabilities.yml` |
| Маршрут «моя почта / мои письма» → роль с capability `email` | `aiagent/config/routes.yml` |
| Тесты приватности личных ящиков | `backend/tests/test_email_personal_mailbox_access.py` |

Точки, где личность агента теперь пробрасывается: capability-диспетчер
(`/api/agent/cap/*`), прямые skill-вызовы `agent_loop._execute_skill` и
детерминированные fast-path'ы оркестратора (правки spec-таблиц). Telegram-бот
личность не пробрасывает (там есть только telegram user id, маппинга на `sub`
нет) — значит, через Telegram личные ящики недоступны вовсе; если это нужно,
потребуется связка telegram_id → пользователь.

Правило видимости, которое зафиксировано в коде: **личный ящик читает только
его владелец** — ни коллеги, ни админы, ни агент в headless-режиме. Админ
управляет ящиком (выдать/сбросить пароль/отозвать), но не его содержимым.
Если для предприятия нужна иная политика (например, руководитель видит почту
подчинённых) — это отдельная настройка в UI ящика, а не роль-бэкдор.

---

## 1. Блокеры инфраструктуры (без них интеграция не работает)

### 1.1 [✔] Traefik ходит не в тот порт → 502 на `mail.<домен>`
`install-mailcow.sh` выставляет `HTTP_PORT=8080`, а mailcow прокидывает эту
переменную и внутрь контейнера (nginx слушает `${HTTP_PORT}`). В
`infra/traefik/prod/routes.yml.template:206` указан `http://nginx-mailcow:80`.

* Исправить URL сервиса на `http://nginx-mailcow:8080`.
* Порт вынести в одно место: `MAILCOW_HTTP_PORT` в `infra/.env`, подстановка
  через `render_traefik_routes` (`infra/installer/lib.sh`) — чтобы установщик и
  роут не могли разъехаться снова.
* Проверка: `curl -I https://mail.<домен>` → 200/302 от SOGo, а не 502.

### 1.2 [✔] Нет валидного TLS на SMTP/IMAP
`SKIP_LETS_ENCRYPT=y` отключает ACME целиком: Traefik закрывает только 443,
Postfix/Dovecot/SOGo остаются на self-signed из `data/assets/ssl/`. Обещание
README «клиент настраивается по email+паролю» при этом не выполняется.

* Скрипт `infra/installer/mailcow-certdump.sh`: достаёт cert/key для
  `mail.<домен>` из `acme.json` Traefik, **копирует** (не симлинк) в
  `infra/mailcow/data/assets/ssl/cert.pem|key.pem`, при изменении делает
  `docker compose restart postfix-mailcow dovecot-mailcow nginx-mailcow`.
* systemd-таймер раз в сутки + вызов из `update-mailcow.sh` после апдейта.
* Проверка: `openssl s_client -starttls smtp -connect mail.<домен>:587` и
  `-connect mail.<домен>:993` показывают сертификат Let's Encrypt.

### 1.3 [✔] Отзыв ящика делает пользователя «вечно занятым»
`revoke_user_mailbox` удаляет ящик в Mailcow, но строку `MailboxConfig` только
гасит `is_active=False`, а все три эндпоинта ищут по `owner_sub +
mailbox_type` без учёта активности → повторная выдача навсегда отдаёт 409,
`reset-password` бьёт в удалённый ящик.

* Решение: строку удалять (`db.delete(cfg)`) — ящика в Mailcow уже нет, хранить
  осиротевший конфиг с паролем незачем; либо, если нужна история, добавить
  `mailbox_type="personal_revoked"` и фильтровать `is_active` во всех запросах.
* `provision` должен уметь переиспользовать освободившийся local_part.
* Тест: выдать → отозвать → выдать снова.

### 1.4 [✔] Отзыв = безвозвратное удаление переписки за одним `window.confirm`
* Двухшаговое подтверждение с вводом адреса (как в опасных операциях админки).
* Опция «отключить, но не удалять» (Mailcow `active=0`) — дефолт для увольнения:
  почта остаётся доступной руководителю по отдельному решению.
* Запись в `AuditLog` с числом писем на момент отзыва.

---

## 2. Приватность и триаж личных ящиков

### 2.1 [✔] Скоуп чтения на API и в spec-таблицах
Сделано (раздел 0). Осталось перепроверить смежные точки: экспорт, RAG-индекс
(`documents`/Qdrant), `analytics.*` — см. 2.5.

### 2.2 [✔] Триаж личных ящиков — только с явного согласия
Сейчас `email_triage._active_mailbox_names()` метёт все активные ящики,
включая личные, без выключателя.

* `mailbox_configs.sweep_enabled` (bool, default **false** для `personal`,
  true для `shared`) + миграция.
* В `/settings → Моя корпоративная почта` — переключатель «разрешить Свете
  читать этот ящик» с явным текстом, что именно получает агент.
* `_active_mailbox_names()` фильтрует по `sweep_enabled`.
* Заодно: не создавать движок на каждый прогон (`create_engine` в теле задачи),
  использовать общую sync-сессию Celery; не подменять ошибку БД легаси-списком
  ящиков (сейчас сбой БД выглядит как успешный прогон).

### 2.3 [✔] Не воровать «непрочитанность»
`imap_client.py:224` ставит `\Seen` на забранные письма. Для личного ящика это
значит, что бот «читает» почту раньше человека.

* Для `mailbox_type="personal"` использовать `BODY.PEEK[]` и не ставить флаг;
  прогресс хранить по UID (`last_seen_uid` в `MailboxConfig`).

### 2.4 [✔] Не спамить админов чужой личной почтой
`_mailbox_recipients` при пустом `assigned_role` уведомляет всех админов.

* Для личного ящика получатель уведомления = владелец, без fallback.

### 2.5 [✔] Владелец у документов из личных вложений
`_store_attachment` создаёт `Document` без `owner_sub`/`visibility` → вложение
из личной почты попадает в общий документооборот и в RAG.

* Проставлять `owner_sub = MailboxConfig.owner_sub` и приватную видимость;
  дальше работает существующий `app/domain/access.py`.
* Проверить дедупликацию: одинаковый файл из личного и общего ящика не должен
  «расшаривать» приватный документ (сейчас дедуп по `file_hash` переиспользует
  первую запись).

---

## 3. Актуальность и эксплуатация Mailcow

### 3.1 [✔] Пин устарел
`DEFAULT_TAG=2026-05c` (26 мая) при вышедшем `2026-07` от 13 июля с
security-патчами (Postfix 3.10.12, Rspamd 4.1.0, Nginx 1.30.3).

* Поднять до `2026-07` после прогона `update-mailcow.sh` на тестовом стенде.
* `DEFAULT_TAG` — в одном месте (`infra/.env` → `MAILCOW_TAG`), оба скрипта
  читают его оттуда.

### 3.2 [✔] `--check` не проверяет обновления
Сейчас сравнивает текущий тег с захардкоженной константой того же скрипта, то
есть еженедельный таймер сообщает «обновлений нет» до ручной правки кода.

* Запрашивать `https://api.github.com/repos/mailcow/mailcow-dockerized/releases/latest`,
  сравнивать с текущим тегом, при недоступности сети — честно сообщать об этом,
  а не «всё актуально».
* Вывести статус в админскую панель «Обновления» рядом с Authentik.

### 3.3 [✔] Ресурсы
ClamAV + Solr съедают ~4-6 ГБ RAM на хосте, где уже стоят Ollama/vLLM/Qdrant.

* `SKIP_CLAMD=y`, `SKIP_SOLR=y` по умолчанию в `install-mailcow.sh`, с
  комментарием как включить обратно.

### 3.4 [✔] Роут `mail.*` рендерится даже без Mailcow
Traefik просит LE-сертификат на несуществующий хост → шум и расход попыток ACME.

* Рендерить блок только если `infra/mailcow` установлен (или флаг
  `MAIL_ENABLED=true` в `infra/.env`).

### 3.5 [✔] Бэкап
`backup.sh` кладёт полный `vmail` всех сотрудников в каждый общий архив.

* Отдельная политика/ротация для mailcow-части, опция `--skip-mailcow`,
  оценка размера перед стартом.

---

## 4. Удобство настройки (UX админа и сотрудника)

### 4.1 [✔] Документация про allow-list IP у Mailcow API
Ключ в Mailcow привязан к списку разрешённых IP. Без внесения подсети backend
кнопка «Проверить подключение» падает без внятной причины — это ловит почти
каждого при первой настройке.

* Добавить в `mailcow.README` и в подсказку на странице интеграции;
* в тексте ошибки `test_connection` подсказывать про allow-list при 401/403.

### 4.2 [✔] «Сохранить и проверить» одной кнопкой
Сейчас «Проверить» заблокирована, пока ключ не сохранён; валидации `api_url`
(схема, лишний слэш) нет.

### 4.3 [✔] Квота ящика
`quota_mb=1024` захардкожена в `provision_user_mailbox` — вынести в поле формы
и в настройки интеграции (дефолт по домену).

### 4.4 [✔] Жизненный цикл
Удаление/деактивация пользователя не трогает его ящик — он остаётся и (после
2.2) продолжает числиться активным. Добавить обработку в `admin.delete_user`.

### 4.5 [ ] Единый вход — НЕ СДЕЛАНО (осознанно, см. «Что осталось»)
Сейчас пароль ящика генерирует админ и передаёт сотруднику вручную — второй
набор учёток рядом с Authentik. Mailcow поддерживает внешний IdP; связка
Authentik → Mailcow убирает раздачу паролей и делает UX почты сопоставимым с
остальным продуктом. Требует отдельной проработки (SOGo + Dovecot auth).

---

## 4bis. Как это реализовано (файлы)

| Пункт | Ключевые файлы |
|---|---|
| 1.1 | `infra/traefik/prod/routes.yml.template` (`__MAILCOW_HTTP_PORT__`), `infra/installer/lib.sh` (`render_traefik_routes`) |
| 1.2 | `infra/installer/mailcow-certdump.sh` + `.service`/`.timer`; вызовы из `install-mailcow.sh` и `update-mailcow.sh` |
| 1.3/1.4 | `backend/app/api/admin.py` (`_personal_mailbox`, `POST …/mailbox/revoke`), `backend/app/services/mailcow_api.py` (`set_mailbox_active`), UI `frontend/app/admin/users/[sub]/page.tsx` |
| 2.2 | миграция `20260724_0003`, `mailbox_configs.sweep_enabled`, `backend/app/tasks/email_triage.py`, `PATCH /api/mailbox/me/sweep`, `PATCH /api/admin/users/{sub}/mailbox/sweep`, UI обеих сторон |
| 2.3 | `backend/app/tasks/imap_client.py` (`BODY.PEEK` + `last_seen_uid`) |
| 2.4 | `backend/app/tasks/ingest.py` (`_mailbox_recipients`) |
| 2.5 | `backend/app/tasks/ingest.py` (`_store_attachment(owner_sub=…)`, скоуп дедупликации) |
| 3.1/3.2 | `MAILCOW_TAG` в `infra/.env`, `update-mailcow.sh` (`releases/latest`) |
| 3.3 | `install-mailcow.sh` (`SKIP_CLAMD`/`SKIP_SOLR`) |
| 3.4 | маркеры `MAILCOW-BLOCK-*`/`MAILCOW-SERVICE-*` + логика в `render_traefik_routes` |
| 3.5 | `infra/installer/backup.sh` (`--skip-mailcow`, оценка размера vmail) |
| 4.1 | `mailcow_api.explain_api_failure`, тексты в `mailcow.README`/README/UI |
| 4.2 | `IntegrationMailServerUpdate.verify`, `_normalize_api_url`, кнопка «Сохранить и проверить» |
| 4.3 | `mail_server_config.default_quota_mb`, `mailbox_configs.quota_mb`, поля в UI |
| 4.4 | деактивация пользователя гасит его ящик (`update_user` в `admin.py`) |

## 4ter. Развёртывание из админки (2026-07-25)

Кнопка **Администрирование → Интеграции → «Развернуть Mailcow»**. Бэкенд не
может запускать `docker compose` (нет CLI, репозиторий не смонтирован), поэтому
используется тот же хенд-офф, что и для апгрейда Authentik: заявка кладётся в
`_control/mailcow-install.json` в томе бэкапов, host-агент
(`infra/installer/update-agent.sh`, systemd-таймер) исполняет
`install-mailcow.sh --domain … --yes` и стримит прогресс/лог обратно в ту же
карточку.

* API: `backend/app/api/mail_deploy.py` — `GET /api/admin/mail-server/deploy/status`,
  `POST /deploy` (только человек, не агент), `/deploy/cancel`, `/deploy/dismiss`.
* Агент умеет два типа заявок и пишет `_control/agent.heartbeat`, поэтому GUI
  отличает «агент не установлен» от «агент простаивает» и не копит заявки,
  которые некому исполнить.
* Ручная часть вынесена в GUI-руководство `/admin/integrations/mailcow-guide`
  (DNS с подстановкой реального домена, порты фаервола, домен и DKIM, API-ключ
  и его белый список IP, таймеры certdump/update-check, чеклист проверки).

## 5. Что осталось

**4.5 — единый вход (Authentik → Mailcow SSO).** Сознательно не делалось в этом
заходе: это не исправление, а новая интеграция (OIDC-провайдер в Authentik,
настройка identity-provider в Mailcow/SOGo, миграция уже выданных ящиков с
локальных паролей). Требует отдельного окна работ и проверки на живом стенде.
До этого момента пароль ящика по-прежнему выдаёт админ.

**Проверки, требующие живого Mailcow.** В репозитории Mailcow не установлен
(`infra/mailcow` отсутствует), поэтому сквозной прогон установки, certdump,
`--check` против GitHub и восстановления из бэкапа выполняется на стенде по
чеклисту в разделе 6.

## 6. Порядок работ

1. **Сначала то, без чего фича не работает:** 1.1 → 1.2 → 1.3/1.4.
2. **Затем приватность:** 2.2 → 2.3 → 2.4 → 2.5 (2.1 закрыт).
3. **Эксплуатация:** 3.1 → 3.2 → 3.3 → 3.4 → 3.5.
4. **UX:** 4.1 → 4.2 → 4.3 → 4.4; 4.5 — отдельной задачей.

## 7. Чеклист приёмки

- [ ] `https://mail.<домен>` открывается (не 502), сертификат валиден
- [ ] Thunderbird/Outlook настраиваются по email+паролю, без предупреждений о
      сертификате на 993/587
- [ ] Выдать → отозвать → выдать ящик тому же пользователю
- [ ] Личные письма коллеги не видны ни в `/email`, ни через агента, ни в
      spec-таблице `emails`, ни в поиске (регресс-тест
      `tests/test_email_personal_mailbox_access.py`)
- [ ] Триаж личного ящика выключен по умолчанию; включённый не сбивает
      непрочитанность
- [ ] `update-mailcow.sh --check` показывает реальный последний релиз
- [ ] `backup.sh` + `restore.sh` восстанавливают почту на чистом стенде
