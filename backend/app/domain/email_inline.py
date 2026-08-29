"""Ф5.2 — картинки, вставленные В ТЕЛО письма, а не приложенные рядом.

Композер грузит вставленную картинку обычным вложением и оставляет в HTML
маркер ``data-attachment-id``. Отсюда видно, какие вложения на самом деле
части тела: они уходят в ``multipart/related`` под ``cid:``, остальные — как
обычные вложения. Без этого получателю приходит ссылка на наш сервер, которая
снаружи не открывается: в письме дыра, а отправитель об этом не узнаёт.
"""

from __future__ import annotations

import re


def split_inline(body_html: str | None, attachments: list) -> tuple[list, list]:
    """(inline, files) — вложения, на которые ссылается тело, и все прочие."""
    inline, files = [], []
    for a in attachments:
        marker = f'data-attachment-id="{a.id}"'
        if body_html and marker in body_html:
            inline.append(a)
        else:
            files.append(a)
    return inline, files


def cid_for(attachment_id) -> str:
    return f"att-{attachment_id}@aiworkspace"


def rewrite_to_cid(body_html: str, attachment_id) -> str:
    """Заменить src у одной вставленной картинки на ``cid:``.

    Маркер ``data-attachment-id`` остаётся: по нему часть находится снова, если
    черновик отправляют повторно.
    """
    cid = cid_for(attachment_id)
    pattern = (
        r'<img\b[^>]*?data-attachment-id="' + re.escape(str(attachment_id)) + r'"[^>]*?>'
    )

    def _swap(m: re.Match) -> str:
        tag = m.group(0)
        if re.search(r'\ssrc="[^"]*"', tag):
            return re.sub(r'\ssrc="[^"]*"', f' src="cid:{cid}"', tag, count=1)
        return tag[:-1].rstrip("/") + f' src="cid:{cid}">'

    return re.sub(pattern, _swap, body_html)
