# E10 — контракт продолжения после проверенного commit

Дата: 27 сентября 2026. Статус: **CONTRACT REVIEWED / RED TESTS ADDED**.

## Граница карточки

E10 описывает новый, отдельный переход для остановленного durable chat, когда
получатель уже зафиксировал эффект, ответ worker-у мог быть потерян, а текущая
версия артефакта независимо проверена. E10 не реализует переход, не меняет
`resume_chat_run`, worker, schema или UI и не открывает `can_resume`. Существующий
путь `confirmation_required` остаётся единственным работающим продолжением.

Проверенный commit не означает разрешение повторить tool. Продолжение создаёт
новую попытку, восстанавливает историю и подставляет в неё **детерминированно
адаптированный сохранённый ответ receipt** как successful `ToolResult` уже
завершённого logical action. `execute_skill`
для этого action больше недостижим. Подтверждение владельца означает «продолжить
после доказанного результата»; оно не подтверждает сам эффект задним числом и не
разрешает следующие tool calls. Для каждого следующего действия действуют его
обычные RBAC, confirmation и execution gates.

## Авторитетные данные и binding

Решение допустимо только после сериализации под общим lock `WorkOrder` и должно
связать следующие неизменяемые значения:

| Область | Обязательная связь |
| --- | --- |
| Владелец и turn | `owner_key`, `DurableChatRun`, `session_id`, исходный `user_message_id`, отсутствие более нового turn |
| Источник | source `attempt_id`, `step_id`, `plan_id`, source `plan_revision`, checkpoint `sha256`, phase и `in_flight_call_id` |
| Action | `logical_action_id`, `call_id`, canonical request и `request_digest`; action принадлежит тем же order/source attempt |
| Receipt | supported `operation`, `receipt_version`, owner/order/action/request binding, exact raw `response` и `response_digest`, artifact ID/revision, provenance |
| Наблюдение | новый server-side вызов E09 verifier; signed `matched` verdict для exact receipt artifact version/hash |
| Runtime | digest плана, config fingerprint, последний известный ход, срок решения и общие budget counters |
| Цель | новый `target_step_id`, `target_revision`, одноразовый `decision_id`; source и target различны |

Клиентский E09 verdict не является authority: его `can_resume=false` сохраняется.
Endpoint сам заново читает receipt/descriptor и вызывает verifier для fresh
recipient observation до фиксации решения. Это не одна ACID-транзакция с БД
получателя: после observation и повторного получения order lock сервер сверяет
связанные digest/version ещё раз, а worker повторяет проверку перед consumption.
Изменение recipient между этими точками даёт fail-closed. Старый, присланный,
даже корректно подписанный verdict не
заменяет эту проверку. Поддерживаются только две review-approved пары E09:
`agent_control.task_propose`/`agent_task` и
`warehouse.update_item`/`inventory_item`. Внешний эффект, opaque URL/reference,
legacy receipt без достаточного binding или неизвестная операция fail-closed.

## Таблица переходов

| Source frontier | Доказательство | Решение владельца | Переход |
| --- | --- | --- | --- |
| `tool_started`, action фактически committed, checkpoint ещё не содержит tool result | exact supported receipt + fresh exact `matched` observation | approve | создать один target step; адаптировать raw receipt response в supported `ToolResult`; удалить только этот call из pending; сохранить остальной tail |
| `tool_recorded`, journal/history содержит `outcome_unknown`, но receipt доказывает commit | exact unknown result binding + exact receipt + fresh exact `matched` observation | approve | заменить только unknown result этого call детерминированным successful `ToolResult`; не добавлять дубль; продолжить pending tail |
| тот же допустимый source | то же | reject | записать неизменяемое отрицательное решение; order остаётся blocked, step не создаётся |
| повтор идентичного request/decision key | ранее committed decision | то же тело | вернуть тот же target run/step без нового события или шага |
| повтор с отличающимся телом | ранее committed decision | любое | `409`, без мутации |
| любой source | missing/changed/stale artifact, forged/stale verdict, corrupt/missing receipt, response/request mismatch | approve | отказ `409`, order остаётся blocked |
| canceled order, более новый user turn, changed plan/config, expired decision, exhausted budget | любое | approve | отказ `409`, никакого target step |
| unknown external effect или unsupported recipient | opaque/manual evidence | approve | отказ; E10 не превращает наблюдение человека в commit proof |

Остановленный source attempt и `blocked` order являются обязательной boundary:
после ошибки worker-а attempt может быть `failed`, а после записанного v1
`outcome_unknown` он имеет статус `outcome_unknown`. Для
`tool_started` отсутствие `completed_call` и tool-message ожидаемо: transition
пропускает raw `receipt.response` через тот же operation-specific adapter, который
использует gateway (`normalize_http_one_db_commit_response` для текущих
получателей), получает valid `ToolResult(status="succeeded")` и сериализует его
как `_tool_result_to_history`: JSON string через
`json.dumps(..., ensure_ascii=False)`. Raw `receipt.response_digest` доказывает
ответ получателя; отдельный digest адаптированного `ToolResult` доказывает
journal/history и не обязан совпадать с raw digest. Для `tool_recorded` обязательны
`completed_call` с valid v1 `outcome_unknown`, соответствующая строковая
tool-message и их exact digest binding. Transition удаляет/заменяет только этот
unknown result, не переписывает другие сообщения и не вставляет второй результат.

## Восстановление истории

Алгоритм работает по stable `call_id`/`logical_action_id`, не по позиции массива.

1. Проверить целостность envelope и найти единственный in-flight/recorded call.
2. Скопировать все сообщения до frontier без изменений, включая результаты ранее
   завершённых calls.
3. Для `tool_started` вставить после соответствующего assistant tool-call одно
   `{role: "tool", tool_call_id: call_id, content: json_string}` с сериализованным
   supported `ToolResult`, детерминированно адаптированным из receipt. Для
   `tool_recorded` заменить ровно одну связанную unknown tool-message этим же
   successful `ToolResult`; unrelated history сохранить byte-for-byte.
4. Удалить из `pending_calls` только доказанный call. Порядок и содержимое прочего
   pending tail сохраняются. Новый worker начинает после подставленного результата.
5. Не переносить approval на tail. Если следующий call требует confirmation, он
   снова останавливается на обычной границе.

Если call отсутствует, встречается дважды, result стоит не в том месте, либо
history и pending frontier расходятся, восстановление запрещено.

## Атомарность и бюджеты

Под одним order/session lock атомарно выполняются финальная сверка сохранённых
observation/version/digest, создание immutable decision event, single-use
consumption marker, новая revision и target step. Само чтение recipient для fresh
observation не объявляется частью этой транзакции. Уникальность
decision/source action не допускает двух target steps
при конкурентных approve. Worker атомарно consume-ит решение вместе с claim шага;
после crash повторное чтение уже подставленного результата не даёт исполнить tool.

Новая попытка не обнуляет `max_tool_calls`, wall clock, tokens, cost и иные общие
счётчики. Доказанный action учитывается как уже потраченный tool call; подстановка
не добавляет новый effect и не вычитает его из истории расхода.

## Красные сценарии E11

`backend/tests/test_verified_commit_continuation_contract.py` содержит исполняемый
fixture-validator сценарных входных данных и domain seam
`verified_commit_continuation_state`. В E10 два позитивных сценария были
`xfail(strict=True)`; после реализации E11 они переведены в обычные passing
tests. Сценарии требуют:

- разные восстановления `tool_started` и `tool_recorded` без повторного tool;
- сохранение прочей истории и pending tail;
- exact action/request/receipt/result/artifact binding и fresh observation;
- отказ canceled/new-turn/changed plan/config/expired/budget/unsupported cases;
- сохранение `can_resume=false` до E11.

Отрицательные тесты пока проверяют только согласованность fixture, а не
production validator: E11 обязан направить их через новый domain seam и добавить
API/worker integration и crash-проверки. Отдельно нужно проверить receipt и
verdict по настоящим digest и UUID, а не строковым маркерам fixture.

## Решения review для E11

1. Отдельная таблица decision/consumption с DB unique по source action и
   source attempt; `WorkEvent` сохраняет audit, но JSON query не является
   уникальным ограничением для конкурентного resume.
2. Для `tool_recorded` разобрать сохранённую JSON-строку tool-message и сравнить
   содержимое с journal result; порядок ключей после checkpoint roundtrip может
   отличаться. Затем заменять только эту строку строкой адаптированного успешного
   результата. Raw digest receipt и digest journal различны и сверяются по своим
   источникам.
3. TTL проверять при решении и перед worker consumption. Истечение после создания
   шага оставляет его blocked с явной причиной, без автоматического replay;
   владелец должен принять новое решение после новой проверки артефакта.
4. API intent — отдельный `verified_commit` discriminator и validator; старый
   confirmation-only request остаётся совместимым и не меняет семантику.

Исполнитель: `gpt-5.6-sol`; два цикла реализации после review сеньора.
Экономия не измерена. Deploy, commit и push не выполнялись.
