"""W2: input that moves and types like a person.

A real visitor's pointer travels a curve to an element and lands somewhere
inside it, not in its exact centre; keys come one by one with an uneven
rhythm; a page is read by turning the wheel. ``locator.click()`` and
``fill()`` do none of that — they teleport and paste, which anti-bot scripts
measure. These helpers keep the last pointer position per session so a
sequence of actions forms one continuous trajectory.
"""

from __future__ import annotations

import asyncio
import random

# Typed key by key up to this length; the rest is inserted at once (a person
# pastes a long text too, and 2000 characters at 100 ms would take minutes).
TYPE_KEYS_MAX = 120

# Russian ЙЦУКЕН on the physical keys of a US board: (code, Windows key code).
# keyboard.type() sends a character missing from the US layout as a bare
# "input" without keydown/keyup — Cyrillic typed with no key presses at all.
_RU_KEYS = dict(
    zip(
        "йцукенгшщзхъфывапролджэячсмитьбю.ё",
        [
            ("KeyQ", 81), ("KeyW", 87), ("KeyE", 69), ("KeyR", 82), ("KeyT", 84),
            ("KeyY", 89), ("KeyU", 85), ("KeyI", 73), ("KeyO", 79), ("KeyP", 80),
            ("BracketLeft", 219), ("BracketRight", 221), ("KeyA", 65), ("KeyS", 83),
            ("KeyD", 68), ("KeyF", 70), ("KeyG", 71), ("KeyH", 72), ("KeyJ", 74),
            ("KeyK", 75), ("KeyL", 76), ("Semicolon", 186), ("Quote", 222),
            ("KeyZ", 90), ("KeyX", 88), ("KeyC", 67), ("KeyV", 86), ("KeyB", 66),
            ("KeyN", 78), ("KeyM", 77), ("Comma", 188), ("Period", 190),
            ("Slash", 191), ("Backquote", 192),
        ],
        strict=True,
    )
)
_SHIFT = 8  # CDP modifier bit


async def _cdp(page, state: dict):
    sessions = state.setdefault("_cdp", {})
    session = sessions.get(id(page))
    if session is None:
        session = sessions[id(page)] = await page.context.new_cdp_session(page)
    return session


async def _key_ru(page, state: dict, char: str) -> bool:
    """Press the key that types ``char`` on a Russian layout; False if none."""
    lower = char.lower()
    if lower not in _RU_KEYS:
        return False
    code, vk = _RU_KEYS[lower]
    shift = char != lower
    cdp = await _cdp(page, state)
    base = {"code": code, "windowsVirtualKeyCode": vk, "modifiers": _SHIFT if shift else 0}
    if shift:
        await cdp.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Shift", "code": "ShiftLeft", "windowsVirtualKeyCode": 16, "modifiers": _SHIFT})
    await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": char, "text": char, "unmodifiedText": lower, **base})
    await asyncio.sleep(random.uniform(0.02, 0.07))
    await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char, **base})
    if shift:
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Shift", "code": "ShiftLeft", "windowsVirtualKeyCode": 16, "modifiers": 0})
    return True


async def type_char(page, state: dict, char: str) -> None:
    if char.isascii():
        await page.keyboard.type(char)
    elif not await _key_ru(page, state, char):
        # A character no key of either layout makes (№, «, —): it is inserted,
        # as a person would pick it from a symbol table or paste it.
        await page.keyboard.insert_text(char)


def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
    return x, y


def path(
    start: tuple[float, float], end: tuple[float, float], steps: int
) -> list[tuple[float, float]]:
    """Points of a curved path from start to end, eased in and out."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    spread = max(30.0, (dx * dx + dy * dy) ** 0.5 * 0.25)
    c1 = (
        x0 + dx * 0.3 + random.uniform(-spread, spread),
        y0 + dy * 0.3 + random.uniform(-spread, spread),
    )
    c2 = (
        x0 + dx * 0.7 + random.uniform(-spread, spread),
        y0 + dy * 0.7 + random.uniform(-spread, spread),
    )
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)
        points.append(_bezier((x0, y0), c1, c2, (x1, y1), eased))
    points[-1] = (x1, y1)
    return points


async def _viewport(page) -> tuple[int, int]:
    size = page.viewport_size
    if size:
        return size["width"], size["height"]
    width, height = await page.evaluate("() => [innerWidth, innerHeight]")
    return int(width), int(height)


async def move_to(page, state: dict, x: float, y: float) -> None:
    start = state.get("mouse")
    if start is None:
        width, height = await _viewport(page)
        start = (random.uniform(0.2, 0.8) * width, random.uniform(0.2, 0.8) * height)
    distance = ((x - start[0]) ** 2 + (y - start[1]) ** 2) ** 0.5
    steps = max(8, min(40, int(distance / 25) + random.randint(4, 10)))
    for px, py in path(start, (x, y), steps):
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.004, 0.014))
    state["mouse"] = (x, y)


def _point_in(box: dict) -> tuple[float, float]:
    """A point inside the box, nearer the middle, never on its edge."""
    fx = min(0.85, max(0.15, random.gauss(0.5, 0.14)))
    fy = min(0.8, max(0.2, random.gauss(0.5, 0.12)))
    return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy


async def click(page, locator, state: dict, *, timeout: float = 10000) -> None:
    await locator.scroll_into_view_if_needed(timeout=timeout)
    box = await locator.bounding_box(timeout=timeout)
    if not box or box["width"] < 1 or box["height"] < 1:
        # Nothing to aim at (zero-size, inside a closed shadow root): the
        # driver's own click still respects visibility and actionability.
        await locator.click(timeout=timeout)
        return
    x, y = _point_in(box)
    await move_to(page, state, x, y)
    await asyncio.sleep(random.uniform(0.05, 0.18))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.04, 0.12))
    await page.mouse.up()


async def hover(page, locator, state: dict, *, timeout: float = 10000) -> None:
    await locator.scroll_into_view_if_needed(timeout=timeout)
    box = await locator.bounding_box(timeout=timeout)
    if not box:
        await locator.hover(timeout=timeout)
        return
    await move_to(page, state, *_point_in(box))


async def type_text(page, locator, text: str, state: dict, *, timeout: float = 10000) -> None:
    """Focus the field like a person, replace what is there, type the text."""
    await click(page, locator, state, timeout=timeout)
    await page.keyboard.press("ControlOrMeta+A")
    await page.keyboard.press("Backspace")
    keyed, rest = text[:TYPE_KEYS_MAX], text[TYPE_KEYS_MAX:]
    for i, char in enumerate(keyed):
        await type_char(page, state, char)
        pause = random.uniform(0.045, 0.17)
        if char == " " or (i and i % random.randint(7, 15) == 0):
            pause += random.uniform(0.05, 0.25)
        await asyncio.sleep(pause)
    if rest:
        await page.keyboard.insert_text(rest)


async def scroll(page, state: dict, dy: int) -> None:
    """Turn the wheel in notches over the page, pointer somewhere inside it."""
    if state.get("mouse") is None:
        width, height = await _viewport(page)
        await move_to(
            page, state, width * random.uniform(0.3, 0.7), height * random.uniform(0.3, 0.7)
        )
    left = dy
    while abs(left) > 0:
        notch = max(-abs(left), min(abs(left), random.choice((100, 100, 120, 200))))
        step = notch if left > 0 else -notch
        await page.mouse.wheel(0, step)
        left -= step
        await asyncio.sleep(random.uniform(0.03, 0.12))
    await asyncio.sleep(random.uniform(0.2, 0.5))


async def glance(page, state: dict) -> None:
    """What a visitor does on a fresh page: the pointer drifts, the wheel turns."""
    try:
        width, height = await _viewport(page)
        await move_to(
            page, state, width * random.uniform(0.25, 0.75), height * random.uniform(0.3, 0.7)
        )
        await scroll(page, state, random.randint(2, 5) * 120)
    except Exception:  # noqa: BLE001 — a page mid-navigation has no viewport yet
        pass
