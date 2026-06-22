# AI-DOCS — Android-оболочка (Capacitor)

Тонкая нативная оболочка, которая грузит **живой сайт** AI-DOCS в WebView и добавляет возможности
смартфона. **Приложение не привязано к конкретному серверу**: адрес задаётся при первом запуске
(ввод вручную или скан QR-кода) и хранится на устройстве. **Изменения фронтенда применяются без
переустановки** — APK пересобирается только при изменении набора плагинов/прав.

## Что внутри

- `capacitor.config.ts` — без `server.url`; `allowNavigation: ["*"]` только для bootstrap выбранного
  self-hosted сервера, runtime origin-policy применяет `ServerConfig`.
- `public/index.html` — лаунчер первичной настройки (ввод URL / скан QR) и офлайн-фолбэк.
- `android-overrides/` — файлы, накладываемые на сгенерированный `android/` (манифест, FileProvider,
  кастомные плагины Kotlin в пакете `ru.aidocs.app`).
- `scripts/make-release-manifest.sh` — генерация `version.json` + `latest.apk` для раздачи `/download`.

Вся бизнес-логика интеграции натив↔веб живёт во **фронтенде** (`frontend/lib/native-bridge.ts`) и
обновляется вместе с сайтом. В APK — только плагины и OS-обработчики.

### Кастомные плагины (наши)

- **ServerConfig** — хранит выбранный адрес сервера (`get`/`set`/`clear`). Лаунчер читает его и
  переходит на живой сайт; AppUpdate/push резолвят относительные пути относительно этого адреса.
  После настройки сервера WebView остаётся на этом origin; чужие HTTP(S) origin-ы открываются
  во внешнем браузере без доступа к Capacitor bridge. Смена сервера сбрасывает cookie, push-topic
  и pending deep-link.
- **AppUpdate** — самообновление: читает `<сервер>/download/version.json`, скачивает подписанный APK,
  проверяет подпись manifest, sha256, package name и сертификат APK, запускает системный установщик через FileProvider.
- **AidocsPush** — владеет персональным ntfy-топиком (секрет) и foreground-службой подписки
  (`AidocsPushService`) — push без Google services. В payload только title/body/type/action_url.

Готовые community-плагины: Camera, MLKit DocumentScanner, MLKit BarcodeScanning (скан QR сервера),
SendIntent (приём «Поделиться»), NativeBiometric (биометро-lock), SpeechRecognition (голос), Share,
Filesystem, App, Browser.

## Требования к тулчейну

- **Node ≥ 22** (CLI Capacitor 8), **JDK 21** (AGP 8.13), Android SDK: **platform 36 + build-tools 36**
  (нужно для `androidx.core` 1.17), плюс platform-tools. minSdk проекта — 28 (Android 9+).

## Первичная настройка

```bash
cd mobile
npm install
npx cap add android            # генерирует android/ (нужен Android SDK)
scripts/prepare-android.sh     # накладывает overrides, включает Kotlin, minSdk 28, compileSdk 36
npx cap sync android
```

`scripts/prepare-android.sh` идемпотентно: удаляет сгенерированный `MainActivity.java`,
копирует `android-overrides/*`, включает Kotlin (classpath + плагин), ставит `minSdkVersion 28`
и форсирует `compileSdkVersion 36` для всех подпроектов (некоторые плагины, напр. `send-intent`,
жёстко задают 35, что отклоняет `androidx.core` 1.17).

`android-overrides` содержит `AndroidManifest.xml` (intent-filters, разрешения, FileProvider,
foreground-service), `res/xml/file_paths.xml` и Kotlin-исходники плагинов в пакете
`ru.aidocs.app`. `namespace`/`applicationId` (`ru.aidocs.app`) Capacitor берёт из
`capacitor.config.ts` автоматически.

## Подпись (единый release keystore)

Обновление «поверх» работает только если все сборки подписаны одним ключом.

```bash
keytool -genkey -v -keystore aidocs-release.jks -keyalg RSA -keysize 4096 \
  -validity 10000 -alias aidocs
```

Keystore и пароли **не коммитим** — храним как секреты CI (`ANDROID_KEYSTORE_BASE64`,
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`).

В `android/app/build.gradle` добавить `signingConfigs.release` из этих переменных окружения и
привязать к `buildTypes.release`.

`version.json` для `/download` подписывается тем же release-keystore. Генератор
`scripts/make-release-manifest.sh` читает переменные:

```bash
MOBILE_MANIFEST_KEYSTORE=/path/to/aidocs-release.jks
MOBILE_MANIFEST_KEYSTORE_PASSWORD=...
MOBILE_MANIFEST_KEY_ALIAS=aidocs
MOBILE_MANIFEST_KEY_PASSWORD=...   # если отличается от store password
```

Приложение проверяет `signedPayload`/`signature` публичным ключом сертификата
установленного APK. Неподписанный manifest или manifest, подписанный другим
ключом, не считается валидным обновлением.

## Сборка APK

```bash
cd mobile/android
./gradlew assembleRelease
# → app/build/outputs/apk/release/app-release.apk
```

## Публикация релиза (раздача с сервера)

```bash
# Положить latest.apk + version.json в каталог, который backend отдаёт на /download
# (Docker volume releases_data, смонтирован в backend как /releases):
mobile/scripts/make-release-manifest.sh \
  app/build/outputs/apk/release/app-release.apk \
  1.0.0 100 /path/to/releases "Первый релиз"
```

После этого на вашем сервере:
- Страница установки с QR-кодами: `https://<ваш-домен>/get-app`
- Прямой APK: `https://<ваш-домен>/download/latest.apk`
- Манифест обновления: `https://<ваш-домен>/download/version.json`

Страница `/get-app` показывает **два QR-кода**: для скачивания APK и для настройки адреса сервера
(его сканирует экран первичной настройки приложения). Установленное приложение само предложит
обновиться, когда `versionCode` в `version.json` вырастет.

## Push (ntfy)

Включается на бэкенде: `NTFY_ENABLED=true`, `NTFY_EXTERNAL_URL=https://push.<ваш-домен>` (см.
`infra/docker-compose.yml`, сервис `ntfy`, и Traefik-роут `ntfy`). Нужен DNS A-record
`push.<ваш-домен>`. Регистрация устройства происходит автоматически при первом запуске оболочки
(`registerForPush` → `POST /api/devices/register`).
