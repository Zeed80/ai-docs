"""Known mail-provider presets for the mailbox settings UI.

Autofilling host/port/TLS and showing the right auth hint is the difference
between "add a mailbox" actually working and a person guessing IMAP hostnames
and getting a cryptic AUTH failure from Gmail. Each preset also tells the UI
which auth methods this provider actually accepts today — plain passwords
have quietly stopped working for Gmail and (for most tenants) Microsoft 365,
so offering that option there just reproduces the same failed login.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MailboxPreset:
    id: str
    label: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_use_tls: bool  # True = STARTTLS, False = implicit TLS/SSL
    auth_methods: tuple[str, ...]  # subset of "password", "app_password", "oauth2"
    oauth_provider: str | None
    hint: str


PRESETS: list[MailboxPreset] = [
    MailboxPreset(
        id="gmail",
        label="Gmail / Google Workspace",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_use_tls=True,
        auth_methods=("oauth2", "app_password"),
        oauth_provider="google",
        hint=(
            "Обычный пароль аккаунта Google для IMAP/SMTP не принимается. "
            "Надёжнее всего — подключить через OAuth2 (кнопка «Войти через "
            "Google» ниже): пароль вообще не потребуется, доступ можно "
            "отозвать в любой момент на myaccount.google.com/permissions. "
            "Второй вариант — пароль приложения: включите двухфакторную "
            "аутентификацию на аккаунте, затем создайте 16-символьный пароль "
            "на myaccount.google.com/apppasswords и вставьте его в поле "
            "«Пароль» вместо обычного. Если администратор Google Workspace "
            "запретил пароли приложений политикой — доступен только OAuth2."
        ),
    ),
    MailboxPreset(
        id="outlook",
        label="Outlook / Microsoft 365",
        imap_host="outlook.office365.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.office365.com",
        smtp_port=587,
        smtp_use_tls=True,
        auth_methods=("oauth2", "app_password"),
        oauth_provider="microsoft",
        hint=(
            "Microsoft отключает вход по обычному паролю (Basic Auth) для "
            "IMAP/SMTP почти везде — используйте OAuth2 (кнопка «Войти через "
            "Microsoft»). Пароль (в т.ч. пароль приложения) сработает только "
            "если для личного @outlook.com/@hotmail.com включена "
            "двухфакторная аутентификация и создан пароль приложения "
            "(account.live.com → Безопасность), либо администратор "
            "Microsoft 365 явно включил Basic Auth для протокола IMAP/SMTP "
            "в этом тенанте — в большинстве организаций это отключено."
        ),
    ),
    MailboxPreset(
        id="yandex",
        label="Яндекс.Почта",
        imap_host="imap.yandex.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.yandex.ru",
        smtp_port=587,
        smtp_use_tls=True,
        auth_methods=("app_password",),
        oauth_provider=None,
        hint=(
            "Сначала включите доступ по IMAP: Настройки почты → Все "
            "настройки → Почтовые программы → «Разрешить доступ к почтовому "
            "ящику через IMAP». Если на аккаунте включена двухфакторная "
            "аутентификация — обычный пароль не подойдёт, создайте пароль "
            "приложения на id.yandex.ru → Пароли и авторизация → Пароли "
            "приложений (тип «Почта»)."
        ),
    ),
    MailboxPreset(
        id="mail_ru",
        label="Mail.ru",
        imap_host="imap.mail.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.ru",
        smtp_port=465,
        smtp_use_tls=False,  # Mail.ru's 465 is implicit TLS, not STARTTLS
        auth_methods=("app_password",),
        oauth_provider=None,
        hint=(
            "Сначала включите IMAP-доступ: Настройки → Все настройки → "
            "Почтовые клиенты → «Разрешить доступ». Обычный пароль от "
            "почты Mail.ru для сторонних приложений уже не принимается — "
            "создайте «пароль для внешних приложений» в настройках "
            "безопасности аккаунта (id.mail.ru → Пароль и безопасность) и "
            "используйте его вместо обычного пароля."
        ),
    ),
    MailboxPreset(
        id="icloud",
        label="iCloud Mail",
        imap_host="imap.mail.me.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        smtp_use_tls=True,
        auth_methods=("app_password",),
        oauth_provider=None,
        hint=(
            "Apple ID должен быть с двухфакторной аутентификацией. Создайте "
            "пароль приложения на appleid.apple.com → Вход и безопасность → "
            "Пароли приложений — обычный пароль Apple ID для почты не "
            "подходит."
        ),
    ),
    MailboxPreset(
        id="custom",
        label="Другой / корпоративный сервер",
        imap_host="",
        imap_port=993,
        imap_ssl=True,
        smtp_host="",
        smtp_port=587,
        smtp_use_tls=True,
        auth_methods=("password",),
        oauth_provider=None,
        hint=(
            "Введите параметры сервера вручную. Для собственного почтового "
            "сервера на этой платформе (Mailcow) — см. настройки в "
            "/admin/integrations, обычный логин/пароль ящика подходит без "
            "ограничений."
        ),
    ),
]

_BY_ID = {p.id: p for p in PRESETS}


def get_preset(preset_id: str) -> MailboxPreset | None:
    return _BY_ID.get(preset_id)
