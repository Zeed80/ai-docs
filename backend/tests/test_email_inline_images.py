"""Ф5.2 — картинка, вставленная в тело письма.

Композер грузит её обычным вложением и оставляет маркер в HTML. Если не
разделить «части тела» и «файлы рядом», получателю уйдёт ссылка на наш сервер:
снаружи она не открывается, в письме дыра, и отправитель об этом не узнает —
у него-то картинка видна.
"""

from app.domain.email_inline import cid_for, rewrite_to_cid, split_inline


class _Att:
    def __init__(self, ident: str, filename: str = "x.png"):
        self.id = ident
        self.filename = filename


def test_only_the_attachments_the_body_references_become_inline():
    body = '<p>Схема:</p><img src="blob:1" data-attachment-id="aaa">'
    inline, files = split_inline(body, [_Att("aaa", "схема.png"), _Att("bbb", "счёт.pdf")])
    assert [a.id for a in inline] == ["aaa"]
    assert [a.id for a in files] == ["bbb"]


def test_a_letter_without_html_keeps_every_attachment_as_a_file():
    inline, files = split_inline(None, [_Att("aaa"), _Att("bbb")])
    assert inline == []
    assert len(files) == 2


def test_src_is_rewritten_to_cid_and_the_marker_survives():
    """Маркер остаётся: по нему часть находится снова, если черновик
    отправляют повторно."""
    body = '<p>Смотрите</p><img src="blob:http://app/1" data-attachment-id="aaa" alt="схема">'
    out = rewrite_to_cid(body, "aaa")
    assert f'src="cid:{cid_for("aaa")}"' in out
    assert 'data-attachment-id="aaa"' in out
    assert "blob:" not in out
    assert 'alt="схема"' in out


def test_other_images_are_left_alone():
    body = (
        '<img src="blob:1" data-attachment-id="aaa">'
        '<img src="https://example.com/logo.png">'
    )
    out = rewrite_to_cid(body, "aaa")
    assert 'src="https://example.com/logo.png"' in out


def test_an_img_without_src_still_gets_one():
    body = '<img data-attachment-id="aaa">'
    out = rewrite_to_cid(body, "aaa")
    assert f'src="cid:{cid_for("aaa")}"' in out
