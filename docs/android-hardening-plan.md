# План доработки Android-приложения

Статус: первичный hardening-проход начат. Документ фиксирует целевое состояние,
приоритеты и критерии приёмки для Android-оболочки AI-DOCS.

## Цели

- Сохранить self-hosted модель: приложение не должно быть жёстко привязано к
  одному публичному серверу.
- Снизить риск supply-chain атак при установке и обновлении APK.
- Сделать мобильный вход удобным, но аудитируемым и ограниченным от перебора.
- Оставить уникальные мобильные сценарии: QR-login, camera ingest, share-intake,
  push без Google Services, быстрые approve/snooze действия.

## Фаза 1 — критический hardening

### 1.1 Обновления APK

Проблема: приложение скачивает `version.json` и APK с выбранного пользователем
сервера. Hash из manifest защищает только от повреждения загрузки, но не от
злонамеренного сервера.

Сделано:
- Updater отклоняет абсолютный URL APK, если он уводит с настроенного origin.
- Updater проверяет, что скачанный APK имеет тот же package name.
- Updater проверяет, что сертификаты подписи скачанного APK совпадают с
  установленным приложением до запуска системного installer.
- `version.json` содержит подписанный `signedPayload`; подпись создаётся тем же
  release-keystore, которым подписан APK.
- Android updater проверяет подпись manifest публичным ключом сертификата
  установленного приложения и игнорирует неподписанные manifest-ы.

Следующий шаг:
- Добавить негативные release-smoke проверки: чужой manifest, чужая подпись,
  cross-origin APK URL.
- Для MDM/private-store канала зафиксировать отдельную процедуру ротации
  release-keystore и тест совместимости обновлений.

Критерии приёмки:
- APK с другим package name не предлагает установку.
- APK с другой подписью не предлагает установку.
- Manifest с cross-origin `url` отклоняется.
- Manifest с неправильной подписью отклоняется до скачивания APK.

### 1.2 QR-login

Проблема: QR-login создаёт долгоживущую мобильную сессию. Сам QR короткоживущий
и одноразовый, но endpoints должны считаться login-поверхностью.

Сделано:
- `/api/auth/qr-login/create` и `/api/auth/qr-login/redeem` включены в login
  rate limit.
- Логи QR-login включают user sub, IP и короткий hash QR-токена без раскрытия
  самого токена или JWT.

Следующий шаг:
- Добавить audit event в `AuditLog`: `mobile.qr_login_created`,
  `mobile.qr_login_redeemed`, `mobile.qr_login_rejected`.
- Показывать пользователю подтверждение нового мобильного входа на desktop.
- Отправлять уведомление о новом мобильном устройстве.
- Ограничить lifetime QR-login session настройкой для production, например
  30-90 дней вместо дефолта на годы.

Критерии приёмки:
- Повторный redeem QR-токена всегда отклоняется.
- Массовые попытки redeem получают `429`.
- Админ может отозвать все mobile QR-сессии пользователя.
- Новый mobile login виден в audit trail.

### 1.3 Server URL и WebView trust boundary

Проблема: `allowNavigation: ["*"]` удобно для self-hosted, но расширяет
доверенную зону WebView.

Следующий шаг:
- Добавить e2e-smoke на переходы во внешний origin и смену сервера.
- Добавить экран с fingerprint домена или коротким кодом инсталляции для
  защиты от ошибочно отсканированного QR.

Сделано:
- После настройки сервера `ServerConfigPlugin` оставляет внутри WebView только
  выбранный origin и bundled launcher origin.
- Чужие HTTP(S) origin-ы открываются через системный браузер без доступа к
  Capacitor bridge.
- Смена сервера сбрасывает cookie, push-topic и pending deep-link.
- UI предупреждает, что смена сервера сбрасывает сессию и push-настройки.

Критерии приёмки:
- Навигация на чужой origin открывается во внешнем браузере или блокируется.
- Нативные плагины не доступны произвольным внешним сайтам.
- Смена сервера очищает push-topic и локальный app-lock state.

## Фаза 2 — установка и релизный контур

### 2.1 Каналы установки

Рекомендация:
- Основной production-канал: MDM, private Google Play, RuStore/private store или
  GitHub Release с проверяемым release key.
- `/get-app` и sideload оставить для on-prem bootstrap и dev.

Критерии приёмки:
- У администратора есть страница с текущей версией, fingerprint подписи и
  источником сборки.
- Пользователь видит понятное предупреждение перед sideload.
- Документация объясняет, почему Android просит "install unknown apps".

### 2.2 Server-side APK builder

Проблема: backend с доступом к Docker socket имеет слишком широкие привилегии.

Следующий шаг:
- Вынести сборку APK из backend в отдельный build-runner или CI job.
- Backend должен только читать статус и публиковать уже подписанный артефакт.
- Если on-prem builder нужен, запускать его в отдельном сервисе с минимальными
  правами, отдельным audit log и явным feature flag.

Критерии приёмки:
- Основной backend не монтирует Docker socket.
- Build request не даёт доступ к arbitrary container run.
- Логи сборки не содержат keystore password.

## Фаза 3 — мобильные функции

### 3.1 Документы и approve

Приоритетные сценарии:
- Скан счёта камерой → upload → статус обработки.
- Share PDF/XLSX из почты или мессенджера → экран подтверждения → ingest.
- Push по аномалии → открыть карточку → approve/snooze/comment.
- QR-login без ввода пароля.

Критерии приёмки:
- Share-intake не загружает файл без явного подтверждения пользователя.
- Push payload не содержит конфиденциальный текст документа.
- Approve из push требует открытие приложения и актуальную сессию.

### 3.2 Offline

Рекомендация:
- Не делать полный offline-документооборот в первой версии.
- Разрешить только offline queue для upload/share с явной пометкой "ожидает
  отправки".

Критерии приёмки:
- Очередь не теряет файлы при рестарте приложения.
- Пользователь может удалить элемент из очереди до отправки.
- Нет silent upload после смены сервера.

## Фаза 4 — проверки

Минимальный набор:
- `python3 -m pytest backend/tests/test_qr_login.py backend/tests/test_devices.py backend/tests/test_mobile_security.py`
- Android debug/release build после `scripts/prepare-android.sh`.
- Ручной smoke: `/get-app`, первичная настройка server QR, QR-login, push
  register, share-intake, update check.

Для release:
- Проверить APK с неправильным package name.
- Проверить APK с неправильной подписью.
- Проверить manifest с cross-origin APK URL.
- Проверить GMS-less устройство: QR scan, camera, ntfy push.
