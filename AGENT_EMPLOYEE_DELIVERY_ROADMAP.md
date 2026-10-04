# Дорожная карта поставки цифрового сотрудника

Статус документа: **PLANNED**. Срез: 4 октября 2026 года, `HEAD 1e8707d8`.

Этот документ — канонический слой **планирования поставки** остатка E21–E52.
Он заменяет линейное назначение «следующая карточка по номеру», но не заменяет:

- контракты, негативные проверки и Definition of Done карточек в
  `AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md`;
- фактический срез в `AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md`;
- отчёты `docs/agent-employee-delivery/` и доказательства E00–E20;
- ограничения полномочий из `AGENTS.md` и
  `AGENT_EMPLOYEE_ORCHESTRATION.md`.

Все пакеты и назначения ниже имеют статус **PLANNED**, пока отдельный отчёт,
проверки, независимый review и, где применимо, production-приёмка не докажут
обратное. Наличие строки в roadmap не означает готовность кода или продукта.

## 1. Зафиксированная отправная точка

E00–E20 не планируются заново: их принятые доказательства остаются в playbook и
отчётах. Они являются входом, а не поводом переписать историю под новую структуру.

E21 остаётся `IN_PROGRESS`. Приняты и выпущены только ограниченные срезы E21.1,
E21.2a, E21.2b1, E21.2b2, E21.2b3 и E21.2b4. Последний из них дал detached
planner direct Ollama для API/scheduler, свежий ledger для новой API-работы и
остановку по budget без fallback и без запуска executor. Это не закрывает E21.
Остаются как минимум:

- остальные AIRouter/headless/provider paths;
- доказанные token/cost bounds и фактический token/cost accounting;
- legacy baseline и reconciliation parent/child limits;
- active-time intervals без времени ожидания человека;
- общий replan accounting и сквозная проверка всех каналов.

Текущая приёмка была локальной и без живой LLM: оставшиеся headless paths,
платный маршрут и внешние получатели не проверялись. Это принятое ограничение,
а не измеренный успех.

## 2. Неизменяемые границы

1. Модель рассуждает и выбирает действие; keyword routing, keyword approval,
   recipe replay и silent fallback на эвристику запрещены.
2. Постоянные generated skills, runtime-регистрация сгенерированных tools,
   auto-promotion и shadow execution не возвращаются.
3. Детерминированные типы, ACL, approval, digest, lease, budget и network policy
   остаются защитными границами; модель не заменяет и не ослабляет их.
4. Одноразовый код живёт только как versioned artifact конкретной работы в
   ОС-изоляции. Он не импортируется backend-ом и не получает секреты, host FS,
   Docker socket или сеть по умолчанию.
5. `partial`, `waiting_approval`, `outcome_unknown`, HTTP 200, Celery SUCCESS и
   непустой текст не являются завершением задачи без acceptance evidence.
6. Неизвестный owner/provenance/legacy baseline не угадывается: данные идут в
   quarantine, вызов блокируется или результат остаётся unknown.
7. Реальные расходы, внешние адресаты, credentials, production migration и
   расширение rollout требуют отдельного разрешения пользователя.

## 3. Пакеты и точное соответствие карточкам

| Пакет | Карточки | Граница пакета | Статус |
|---|---|---|---|
| P1 Runtime remainder | E21–E25 | Единый budget/state/fence и crash-приёмка runtime | PLANNED |
| P2 Isolated code | E26–E30 | ScriptRun, supervisor, artifact broker и sandbox security | PLANNED |
| P3 Browser | E31–E37 | Owner-bound browser, egress, secrets, files и effect approval | PLANNED |
| P4 Knowledge | E38–E42, E44 | ACL, SQL/graph/vector enforcement, versions и revoke | PLANNED |
| P5 Legacy migration | E43 | Только доказанный перенос старой общей памяти | PLANNED |
| P6 UX and operations | E45–E47 | Work UI, permission builder, cost/operations signals | PLANNED |
| P7 Retired surfaces | E48 | Удаление только после доказанной миграции callers | PLANNED |
| P8 Evaluation and release | E49–E52 | Изолированный corpus, security/chaos и решение о rollout | PLANNED |

E00–E20 остаются историческим baseline. Ни одна карточка не потеряна и не получила
новый статус из-за перегруппировки.

## 4. DAG поставки вместо линейной очереди

Ниже `→` означает реальную зависимость поставки, а не желательный порядок чтения.
Работы без стрелки могут идти независимо при непересекающихся файлах и одном
пишущем исполнителе по умолчанию.

```text
R0: E49 runner/isolation + первые 10 E50 cases
 ├──────────────→ пополнение cases по мере готовности P1–P6
 └──────────────→ ранние fake security/chaos cases E51

P1: E21 remainder → E22 → E23 → E24 → E25
              ├──────────────→ P6/E47 финальный usage и operations contract
              ├──────────────→ P2 runtime integration
              └──────────────→ P3 effect/crash integration

P4a: E38 ACL contract ─┬→ E39 → E40 → E41 ─┐
                       ├→ E42 versioning ────┼→ E44 revoke/derived cleanup
                       ├→ P2/E28 broker ACL ─┘
                       └→ P3/E35 browser files

P2: E26 → E27 → E28 → E29 → E30
                         └────────→ P3/E35

P3: E31 → E32 → E33 → E34 → E35 → E36 → E37

P4/E44 + reviewed migration tool → P5/E43 production migration

stable server transitions from P1/P4 ─→ P6/E45–E47
all migrated callers + reviewed replacement ─→ P7/E48 deletion

P1–P7 gates + complete E50/E51 corpus + explicit live budget approval ─→ P8/E52
```

### Разрешённые ранние срезы

- E49 начинается сейчас: runner и test-world isolation не ждут E48.
- E50 пополняется инкрементально: сначала два проверенных случая, затем всего
  десять содержательных начальных случаев; 120 случаев не создаются одним коммитом.
- E51 получает local-fake security cases рядом с каждой реализуемой границей.
  Итоговый security corpus остаётся отдельным gate и не считается готовым заранее.
- Контрактный срез E38 и version/hash/expected-revision contract E42 делаются до
  artifact broker E28 и browser file flow E35. Полная реализация E42 всё равно
  проверяется на фактических SQL/vector/artifact consumers.
- E44 зависит от E38 и фактических derived stores E39–E42, но **не зависит от
  E43**. Отзыв должен работать до миграции; ambiguous legacy остаётся в quarantine.
- E45–E47 поставляются вертикальными UI/metrics-срезами после стабилизации
  соответствующего server contract. Они не ждут переноса legacy E43.
- Реальный LLM, платный provider, внешний адресат и живые credentials не входят в
  ранние срезы. Их запуск возможен только после отдельного budget/scope approval.

## 5. P1 — остаток единого runtime (E21–E25)

**Граница.** Закрыть учёт каждой физической попытки и безопасные переходы runtime;
не затрагивать ScriptRun/browser/knowledge implementation кроме общих контрактов.

**Порядок внутри пакета.**

1. P1.1 — завершить E21.2/E21.3: составить исчерпывающий caller inventory,
   подключить оставшиеся headless/AIRouter/provider paths, token/cost accounting,
   legacy reconciliation, active-time и replan budget. Каждый provider path —
   отдельный contract slice; неизвестная цена остаётся `unknown`, не нулём.
2. P1.2 — E22: persisted `pause_requested` и acknowledgement безопасной границы.
3. P1.3 — E23: новая ревизия только для будущего frontier, без повторения
   завершённых/unknown действий и без сброса общего budget.
4. P1.4 — E24: recipient-specific fences на фактической границе эффекта. Gateway
   fence не выдаётся за fence SMTP/browser/внутреннего helper-а.
5. P1.5 — E25: process-kill harness на отдельном Compose project и синтетических
   recipients; production workers для chaos-проверки не завершаются.

**Вертикальный сценарий.** HTTP/Telegram fixture/cron создают одну и ту же работу;
provider и nested tool расходуют общий ledger, pause фиксируется, replan сохраняет
committed result, worker погибает после recipient commit и после рестарта эффект
имеет счётчик 1, а неизвестный исход остаётся unknown.

**DoD пакета.** Все реальные caller paths перечислены; неизвестные пути fail-closed;
общий budget не сбрасывается на child/resume/channel switch; E25 воспроизводим;
отчёты содержат crash barriers, строки ledger/journal и exact counters.

**Security gate P1.** Независимый review ACL/approval/lease/budget/fence и гонок.
Средний процент функциональных тестов не может перекрыть duplicate effect,
budget bypass или stale-worker write.

## 6. P2 — изолированный одноразовый код (E26–E30)

**Граница.** Новый ScriptRun без расширения retired runner и без превращения кода
в skill/tool. Supervisor — отдельная доверенная граница, а не shell-as-a-service.

**Зависимости.** E26 может уточнять threat model параллельно P1, но включение E27+
требует принятых budget/fence primitives P1. E28 требует ранних контрактов E38 и
E42; lifecycle E29 использует ToolResult, journal и общий budget.

**Срезы.** E26 contract/threat model → E27 supervisor → E28 broker → E29 lifecycle
→ E30 adversarial suite. Не объединять supervisor, broker и lifecycle в один
сервис или одну непроверяемую миграцию.

**Вертикальный сценарий.** Owner передаёт immutable input artifact; supervisor
запускает allowlisted immutable image без сети/секретов/host path, ограничивает
CPU/RAM/PID/tmp/time, broker помещает output в quarantine и публикует versioned
artifact; cancel/crash не оставляет descendants и не запускает job повторно.

**DoD пакета.** Ограничения обеспечены ядром; path/symlink/archive проверки
выполнены; stdout/stderr bounded; недоступная изоляция даёт blocked без backend
fallback; artifact ACL/version сохранены end-to-end.

**Security gate P2.** Реальные fork bomb, network/DNS, `/proc`, secret, Docker
socket, disk-fill и orphan-process tests. Mock supervisor этот gate не закрывает.

## 7. P3 — браузерный сотрудник (E31–E37)

**Граница.** Только браузерные сессии и web effects. Управление рабочим столом ОС
не добавляется. Browser page/DOM всегда недоверенный источник.

**Зависимости.** E31–E34 могут следовать после P1 независимо от полного P2. E35
ждёт E28 и контракты E38/E42. E36 ждёт effect fencing P1 и stable page revision.

**Срезы.** E31 ownership/lease → E32 versioned DOM/accessibility → E33 network
egress → E34 human login/secret broker → E35 file broker → E36 exact action card
→ E37 crash acceptance.

**Вертикальный сценарий.** Владелец открывает локальный synthetic site, проходит
human handoff, загружает разрешённую versioned fixture, меняет форму, подтверждает
точные origin/values/revision, теряет HTTP response после submit, а read-only
сверка доказывает один effect; download проходит quarantine и owner ACL.

**DoD пакета.** Нет cross-owner cookie/DOM/screenshot; stale element fail-closed;
redirect/subresource/XHR/WebSocket/DNS policy проверена на сетевом слое; секрет не
попадает в model context/log/receipt; external live pilot остаётся выключен.

**Security gate P3.** Prompt injection, SSRF IPv4/IPv6/rebinding, secret exfiltration,
changed form after approval, crash before/after submit и forbidden-effect counters.

## 8. P4 — ACL, версии и отзыв (E38–E42, E44)

**Граница.** Единый источник правил доступа для SQL, cache, graph, vector,
artifacts и derived data. E43 сюда не входит: миграция legacy — отдельный пакет.

**Порядок.**

1. P4.1 — E38: scope/provenance matrix и policy contract.
2. P4.2 — ранний E42 contract: immutable version/hash, expected revision,
   share/revoke binding. Затем его фактическая реализация в consumers.
3. P4.3 — E39 SQL/cache enforcement.
4. P4.4 — E40 graph traversal enforcement.
5. P4.5 — E41 filtered vector retrieval и authoritative recheck до текста/rerank.
6. P4.6 — завершение E42 versioned blocks/artifacts.
7. P4.7 — E44 immediate deny и идемпотентная очистка derived stores.

**Вертикальный сценарий.** Alice делится конкретной immutable version с project,
поиск видит её через SQL/graph/vector; после revoke authoritative check немедленно
запрещает текст даже при stale cache/vector, cleanup догоняет асинхронно, Bob не
получает ни content, ни counts/snippets/IDs.

**DoD пакета.** Enforcement происходит до выдачи/rerank; derived scope не шире
источника; cache учитывает ACL epoch; unknown provenance quarantined; revoke не
ждёт E43 и не восстанавливается незаметно после retry/restore.

**Security gate P4.** Отдельная Alice/Bob/department/project матрица и проверка
каждого store. Любая утечка — hard failure, а не один отрицательный балл.

## 9. P5 — перенос legacy (E43)

**Граница.** Инвентаризация, dry-run и перенос только доказанных категорий.
Разработка migration tool не разрешает запуск на production данных.

**Зависимости.** Reviewed E38 policy, immutable target versions E42 и работающий
revoke E44. Unknown/ambiguous записи остаются в quarantine и не блокируют P4/P6.

**Вертикальный сценарий.** Dry-run классифицирует `owned/shared/ambiguous`, пишет
source ID/hash и правило; повтор batch с resume cursor не создаёт дубль; revoke
перенесённого объекта немедленно действует во всех derived stores.

**DoD пакета.** Idempotent batches, backup/restore rehearsal, отчёт категорий и
нулевое guessed ownership. Production migration — только отдельным поручением.

**Security gate P5.** Случайная выборка provenance и owner decisions сильной
моделью/человеком; никакого назначения admin, last reader или LLM guess.

## 10. P6 — UX и эксплуатация (E45–E47)

**Граница.** UI отображает только авторитетные server transitions и evidence;
метрики не меняют policy и не объявляют результат успешным.

**Нелинейная поставка.** E45 добавляется по мере стабилизации P1 states; E46 может
идти после стабильного delegation schema; E47 instrumenting начинается рядом с
E21 и финализируется после полного accounting. P6 не ждёт E43.

**Вертикальный сценарий.** Оператор с клавиатуры открывает работу, отличает
`pause_requested/paused/waiting/blocked/failed/completed`, видит budget/evidence,
выдаёт узкое typed permission, отзывает его, а concurrent last attempt не проходит;
usage и blocker объясняются без prompt/secret в логах.

**DoD пакета.** Reload/cursor/late response не дублируют решения; кнопки приходят
из server transition contract; unknown price показана как unknown; accessibility,
mobile, empty/error states и owner ACL покрыты.

**Security gate P6.** UI не расширяет wildcard scope, не скрывает unknown и не
позволяет local state обойти server gate. Observability endpoint не раскрывает
prompt, secret, foreign identifiers или content.

## 11. P7 — retired surfaces (E48)

**Граница.** Сначала manifest `active/deprecated/archive/removable`, затем удаление
только доказанно неиспользуемого кода. Данные и история не удаляются вместе с API.

**Зависимости.** Replacement contract принят, runtime callers и UI мигрированы,
`rg`/runtime registrations/DB consumers/docs проверены. Инвентаризацию можно вести
раньше; destructive removal ждёт эти доказательства.

**Вертикальный сценарий.** Поддерживаемый клиент использует новый путь; старый API
явно возвращает retired/410, не вызывает model/tool и не запускает generated-skill,
recipe или keyword fallback; архивные данные остаются читаемыми по ACL.

**DoD пакета.** Нет imports/registrations/callers; migration note существует;
policy/digest/ACL tests остаются; feature unavailable не переключается на рецепт.

**Security gate P7.** Review подтверждает, что удалена поверхность исполнения,
а не защитная проверка или audit evidence.

## 12. P8 — измеримый corpus и выпуск (E49–E52)

**Граница.** Eval измеряет фактический outcome в изолированном мире. CAD pixel/
geometry и role-тесты не смешиваются с employee metrics.

**Поставка.** E49 runner начинается немедленно. E50 cases добавляются рядом с
готовыми capabilities. E51 security/chaos cases создаются рядом с соответствующим
gate, но публикуются отдельно. E52 запускается только после P1–P7 review gates,
complete corpus и явного разрешения live route/budget.

**Вертикальный сценарий.** Runner создаёт новый namespace/DB fixtures, вызывает
реальный common intake/runtime, проверяет DB/recipient/artifact predicates и
forbidden-effect counters, сохраняет model/config/code/fixture versions и trace,
затем удаляет только свои fixture IDs. Повтор не наследует прошлый artifact.

**DoD пакета.** 120 reviewed cases в пяти группах, отдельный security corpus,
заранее зафиксированные predicates/budgets, три независимых разрешённых запуска,
неизменённые failures и независимый review traces. Только E52 может сформулировать
решение об ограниченном rollout; цель не выдаётся за текущий результат.

**Security gate P8.** Forbidden effects и cross-user leaks — отдельный veto.
Их нельзя усреднить с полезностью, скоростью или стоимостью.

## 13. Следующее ограниченное назначение R0

**Статус: IN_PROGRESS.** Первый foundation-срез (два demo cases) независимо
проверен; отчёт: `docs/agent-employee-delivery/R0-eval-foundation.md`.
Расширение до десяти и полная изоляция ещё не приняты. R0 — не платный benchmark
и не заявление о качестве модели.

**Цель.** Реализовать foundation E49 и начальный срез E50: изолированный runner
с local fake LLM/recipient и **10 содержательными cases**. Сначала исполнитель
делает два демонстрационных cases (один ожидаемый pass, один ожидаемый fail),
сеньор проверяет predicates и isolation; только затем тот же контракт расширяется
ещё на восемь cases. Это два reviewable slices, не 10 пустых YAML; scoped commits
после их приёмки создаёт только главный агент.

**Начальные десять cases.** Допустима корректировка конкретных fixtures после
инвентаризации, но проверяемый смысл фиксирован заранее:

1. Успешный read-only workflow с domain predicate, не проверкой текста.
2. Намеренно провальный case: fake recipient фиксирует forbidden write.
3. Конкурентный duplicate intake даёт одну работу.
4. Чужой artifact/owner ID не раскрывает content или metadata.
5. Required approval останавливает effect; одобрение не подменяется моделью.
6. Потерянный ответ после atomic receipt не создаёт второй effect.
7. Две попытки занять последний budget slot: проходит одна.
8. Provider error сохраняется как failure/blocker без keyword fallback.
9. Cancel/stale attempt не выполняет следующий tool.
10. Settlement/observation failure не стирает уже наблюдённый recipient outcome.

**Разрешённый объём.** Схема case, loader/validator, test-world factory, runner,
machine-readable result, два затем десять fixtures и тесты isolation/cleanup.
Не менять runtime policy для того, чтобы cases прошли. Не использовать production
DB, живую LLM, SMTP, Telegram, реальные документы или внешнюю сеть.

R0 не является отдельным headless executor: fake model внедряется в штатный
durable worker после `submit_agent_intake`, а результат проверяется через
сохранённые WorkOrder/step/attempt/event/recipient rows. Запрещены shortcut с
прямым вызовом predicate вместо runtime и прямое редактирование результата case.

**Планируемые пути R0.** Имена новых файлов фиксируют отдельность employee eval
от существующего text-fragment `ModelEvalHarness`:

- читать и переиспользовать:
  `backend/app/domain/agent_intake.py`,
  `backend/app/tasks/durable_chat.py`,
  `backend/app/domain/work_orders.py`,
  `backend/app/ai/work_budget_context.py`,
  `backend/tests/test_durable_chat.py` и профильные budget/receipt tests;
- создать `backend/app/ai/evals/employee_cases.py` — versioned case/result schema
  и fail-closed validator;
- создать `backend/app/ai/evals/employee_harness.py` — namespace lifecycle,
  common intake/worker dispatch, predicate evaluation и machine-readable result;
- создать `backend/app/ai/evals/data/employee_initial_v1.yaml` — сначала два,
  после review десять начальных cases;
- создать `backend/tests/test_employee_eval_harness.py` — контракт, реальный
  test-DB runtime path, isolation, cleanup и pass/fail assertions.

Существующие `backend/app/ai/evals/harness.py`, `cases.yaml` и
`agent_role_cases.json` не расширяются employee outcome-полями: они измеряют
ответ модели/role dispatch и не должны случайно попасть в employee score.

**Минимальный case contract v1.** Обязательны `id`, `group`, `initial_state`,
`owner`, `roles`, `grants`, `task`, `fixtures`, `allowed_effects`,
`acceptance_predicates`, `forbidden_effects`, `budget` и `runtime_mode=fake`.
Predicate — типизированная проверка конкретного DB/recipient/artifact состояния;
произвольный Python/callback из YAML и `expected_contains` запрещены. Result
содержит `case_version`, `fixture_version`, `code_revision`, `config_digest`,
`runtime_mode`, namespace/run/order IDs, каждый predicate verdict, каждый
forbidden counter, фактический budget, trace reference и итоговый status.

**Первые обязательные тесты до расширения 2 → 10.**

1. `test_employee_case_schema_rejects_text_only_acceptance_and_unknown_predicate`.
2. `test_employee_runner_dispatches_via_common_intake_and_durable_worker`.
3. `test_employee_runner_pass_case_reads_persisted_domain_outcome`.
4. `test_employee_runner_fail_case_reports_observed_forbidden_effect`.
5. `test_employee_runner_isolates_two_runs_of_the_same_case`.
6. `test_employee_runner_cleanup_does_not_delete_foreign_fixture_ids`.
7. `test_employee_fake_result_is_excluded_from_live_model_metrics`.

Только после приёмки этих тестов и двух demo cases добавляются cases 3–10.
Расширение не меняет схему/predicates под полученный результат.

**R0 DoD.**

- runner действительно проходит common intake/runtime, а не вызывает predicate
  напрямую;
- каждый run получает уникальный namespace и очищает только свои fixture IDs;
- pass/fail cases доказуемо различаются изменением фактического outcome;
- forbidden-effect counter и expected predicates заданы до выполнения;
- result содержит code/fixture/config/fake-model versions и consumed budget;
- десять cases запускаются воспроизводимо локально; fake runs явно помечены и не
  входят в live-model success rate;
- отчёт не содержит процента «успеха сотрудника» и не требует нового paid run.

**Изоляция существующих тестов входит в R0.** Инвентаризировать общие session
fixtures и глобальные выборки ledger/lease, из-за которых профили E21 сейчас
запускаются отдельными процессами. Новые fixtures должны иметь доказанное владение
и очистку по run IDs; существующие выборки — ограниченный test namespace либо
чистую test DB. Раздельный запуск остаётся временным обходом, не доказательством
совместимости. Не удалять чужие строки и не ослаблять assertions ради зелёного suite.
Граница изменения — только воспроизведённые конфликты test fixtures/queries;
производственные policy и lifecycle не переписываются.

Проверить новый профиль `python3 -m pytest backend/tests/test_employee_eval_harness.py -q`,
повтор того же corpus и совместный запуск с конфликтовавшими budget/lease профилями.
В отчёте перечислить exact команды, воспроизводитель до исправления и результат
после него. Если совместный запуск не проверен, указать это явно; не объявлять
весь backend suite зелёным по результатам отдельных процессов.

## 14. Метрики результата

Метрики фиксируются до live-run. Они не подменяют acceptance predicates.

| Метрика | Определение |
|---|---|
| Task success | Все обязательные acceptance predicates истинны и нет forbidden effect. Ответ модели сам по себе не predicate. |
| Autonomy | Работа завершена без незапланированного human steering. Обязательный policy approval не делает run неуспешным и не считается steering; он учитывается отдельно. |
| Human minutes | Фактическое активное время человека на чтение, ввод, handoff и решение. Пассивное ожидание считается отдельно. |
| Clarifications | Число запросов недостающей информации до продолжения; группировать по причине, не по сообщениям транспорта. |
| Confirmations | Число policy-required approvals, отдельно approved/rejected/expired. Approval — штатный gate, не failure. |
| Task cost | Фактическая стоимость provider/runtime по versioned tariff. Неизвестная цена = unknown, не 0. Failed/retried physical attempts включаются. |
| Latency | Отдельно wall-clock, active compute, queue, provider, tool и human-wait; не складывать human wait в active time. |
| Recovery | После заранее заданного сбоя acceptance достигнута без duplicate/forbidden effect и без сброса budget; иначе recovery failed/blocked. |
| Forbidden effects | Фактические неразрешённые записи, отправки, утечки, cross-owner reads или privilege changes. Каждый случай — hard failure и release veto. |

Функциональный score, autonomy, human minutes, confirmations, cost и latency
публикуются раздельно. Security/forbidden effects не входят в среднее и не могут
быть компенсированы высоким success rate. Отказ пользователя в подтверждении —
корректный безопасный исход, если effect не произошёл; он не равен дефекту агента.

## 15. Правила задания дешёвому исполнителю

1. Единица работы — один связный contract slice, а не механический лимит файлов.
   Допустимо изменить больше пяти файлов, если это один end-to-end контракт с
   callers, migration и тестами. Несколько lifecycle/security contracts в одной
   задаче запрещены даже при двух файлах.
2. Пакет контекста содержит карточку, текущий package slice, exact dependencies,
   разрешённые пути, вызывающие стороны, негативные тесты и предыдущий отчёт.
   Весь roadmap и вся история в prompt не копируются.
3. Переиспользовать `ExecutionContext`, common intake/outbox, `ToolResult`,
   `ActionReceipt`, artifact verification, checkpoint/journal и budget ledger.
   Параллельная система идентичности, retries, receipts или budgets не создаётся.
4. Fences остаются раздельными по lifecycle: order/step/attempt/tool dispatch,
   ScriptRun supervisor job, browser session/page revision и artifact version.
   Их можно связать identifiers, но нельзя спрятать в монолитный «универсальный
   orchestrator lock», обещающий гарантии фактических recipients.
5. Сначала red/negative test и минимальный vertical implementation; затем review
   callers, race/rollback и расширенный regression. Моки не заменяют проверяемую
   transaction/kernel/network границу.
6. После двух циклов исправлений повторяющийся дефект сужается до reproducer и
   причины; задача эскалируется, а не разрастается в рефакторинг.

## 16. Роли и полномочия

- Пользователь задаёт product scope и единственный разрешает paid/live routes,
  credentials, production data migration, внешний rollout и push запрещённой
  накопленной истории.
- Главный агент/сеньор (Sol для сложного review, когда назначен и доступен)
  формирует контракт, независимо читает diff/callers/tests, принимает результат,
  выполняет production build/health, scoped commit и отчёт.
- `gpt-5.6-terra` — пишущий исполнитель по умолчанию.
- `gpt-5.6-sol` — сложная реализация или эскалация; предварительная неудача Terra
  не обязательна, если риск очевидно высокий.
- `gpt-5.6-luna` — только read-only поиск файлов/символов и краткий сбор ссылок.
  Luna не пишет код/тесты, не исправляет и не выполняет review.
- Подагент не создаёт подагентов, не делает deploy/commit/push. По умолчанию один
  пишущий исполнитель. Только главный агент владеет production delivery.

Если требуемая Sol/Terra недоступна или usage/model selection нельзя выполнить,
работа останавливается с точной причиной. Кодинг не переводится на Luna, дорогая
модель не наследуется молча и доказательства не подменяются текстовым обещанием.

## 17. Проверка, сборка и выпуск

Для каждого slice выполняются профильные команды карточки и новые негативные
тесты. Package gate добавляет интеграционный vertical scenario и security suite.

Production rebuild выполняет только главный агент и только из замороженного
coherent release increment: все входящие изменения протестированы, независимо
прочитаны и перечислены. Нельзя собирать production из дерева с непринятым WIP,
даже чтобы «быстро проверить» одну соседнюю карточку. Для принятого кодового
increment обязательны `make prod-build`, Compose status и
`curl -k --fail https://localhost/health`; docs-only изменение сборки не требует.

`DEPLOYED` разрешён только после `REVIEWED`; health не доказывает live LLM/effect,
а unit tests не заменяют crash/browser/kernel E2E. Commit scoped, без `git add .`;
push не выполняется без отдельного разрешения на состав накопленной истории.

## 18. Условия остановки

`BLOCKED` ставится только при конкретной внешней зависимости:

- нет разрешённой Sol/Terra или невозможно задать требуемую модель;
- нужен paid/live provider, credential, внешний адресат или production migration,
  на которые пользователь не дал разрешения;
- невозможно создать отдельную test DB/Compose project или доказать kernel/network
  isolation, а карточка требует именно эту границу;
- владелец/provenance/legacy baseline неизвестен — объект quarantined, не угадан;
- рабочее дерево содержит пересекающийся непринятый WIP и безопасный scoped build
  невозможен;
- обязательный независимый security review недоступен — новая поверхность остаётся
  выключенной, хотя независимые docs/fake-tests могут продолжаться.

«Сложно», «много файлов», слабый средний score или желание ускорить поставку не
являются BLOCKED. Запрещено обходить остановку generated skill, keyword fallback,
ослаблением ACL/approval/budget, fake success, skipped test или живым экспериментом
за пределами разрешённого scope.

## 19. Разрешение противоречий со старым линейным планом

| Старое правило | Каноническое решение этого roadmap |
|---|---|
| «Следующая карточка по номеру», E49 после E48 | Нумерация сохраняет спецификации, но runner E49 и первые cases начинаются R0 сейчас. |
| E38 только после E37 | E38 ACL contract и E42 version contract выполняются рано; browser E35 и broker E28 зависят от них. |
| E42 только после E41 | Contract slice E42 предшествует brokers; полная consumer implementation E42 завершается после фактических stores E39–E41. |
| E44 после E43 | E44 revoke не ждёт migration. E43, наоборот, ждёт работающих ACL/version/revoke; ambiguous остаётся quarantined. |
| E45 после E44 и линейно до E47 | UI/metrics поставляются по стабильным server slices и не ждут legacy migration; финальная приёмка остаётся после producers. |
| E51 только после полного E50 | Local-fake security cases создаются вместе с каждой границей; полный E51 gate закрывается после корпуса E50. |
| Ориентир 2–5 production-файлов | Это ориентир контекста, не искусственная граница. Один связный контракт может требовать больше файлов; разные контракты не сливаются. |
| Каждая карточка немедленно ведёт к deploy | Deploy делает root только coherent, полностью протестированного и reviewed release increment без чужого WIP. |

Во всём, что не меняет порядок поставки и указанные зависимости, исходные
контракты E00–E52 и их Definition of Done продолжают действовать полностью.
