"""Sanity tests for the model's generation loop — pure shape/logic checks,
no dataset needed. Run with the trainer venv: pytest test_model.py.
"""

from __future__ import annotations

import torch

from dataset import collate
from model import IMG_SIZE, CadVectorizerModel


def _model():
    torch.manual_seed(0)
    return CadVectorizerModel(d_model=32, n_layers=2, n_heads=2, dim_ff=64)


def test_collate_pad_mask_includes_the_eos_target_position():
    """A code-review pass flagged (then, on closer inspection, retracted) a
    suspected off-by-one where the EOS row's loss would be masked out —
    ``cad_ir.sequence.encode()`` already appends a trailing EOS row before
    the sequence is saved to .npy, so ``t = cmd.size(0)`` already counts it
    and ``pad_mask[i, t:] = False`` never touches the EOS position itself
    (index t-1). This test pins that down so the real bug (silently
    training the model to never predict EOS) can't be reintroduced by a
    future "fix"."""
    cmd = torch.tensor([1, 1, 0])  # SEG, SEG, EOS — matches encode()'s own convention
    params = torch.zeros(3, 5)
    lc = torch.zeros(3, dtype=torch.long)
    wc = torch.zeros(3, dtype=torch.long)
    image = torch.zeros(1, 8, 8)
    batch = [(image, cmd, params, lc, wc)]

    _images, _dec_in, (cmd_tgt, *_), pad_mask = collate(batch)
    eos_idx = (cmd_tgt[0] == 0).nonzero()[0].item()
    assert pad_mask[0, eos_idx].item() is True


def test_forward_shapes():
    model = _model()
    b, t = 3, 5
    image = torch.rand(b, 1, IMG_SIZE, IMG_SIZE)
    cmd = torch.randint(0, 7, (b, t))
    params = torch.rand(b, t, 5)
    lc = torch.randint(0, 6, (b, t))
    wc = torch.randint(0, 2, (b, t))
    cmd_logits, param_pred, lc_logits, wc_logits = model(image, cmd, params, lc, wc)
    assert cmd_logits.shape == (b, t, 7)
    assert param_pred.shape == (b, t, 5)
    assert lc_logits.shape == (b, t, 6)
    assert wc_logits.shape == (b, t, 2)


def test_generate_immediate_eos_returns_empty_not_a_bogus_row(monkeypatch):
    """Regression test: predicting EOS at the very first decoding step (a
    legitimate "empty sheet" prediction, since encode() of a zero-entity IR
    is literally [EOS]) must return an EMPTY row list — not append an EOS
    row as if it were a real entity. See model.py generate()'s stop check."""
    model = _model()
    model.eval()

    # Force the cmd head to always predict EOS (index 0) regardless of input.
    with torch.no_grad():
        model.cmd_head.weight.zero_()
        model.cmd_head.bias.zero_()
        model.cmd_head.bias[0] = 100.0  # overwhelming logit for EOS

    image = torch.rand(1, 1, IMG_SIZE, IMG_SIZE)
    rows = model.generate(image, max_len=10)
    assert rows == [], f"expected empty generation on immediate EOS, got {rows}"


def test_generate_stops_before_max_len_when_eos_predicted_midway():
    model = _model()
    model.eval()
    calls = {"n": 0}
    real_forward = model.cmd_head.forward

    def fake_cmd_head(x):
        calls["n"] += 1
        out = real_forward(x).clone()
        if calls["n"] >= 3:
            out[..., 0] += 1000.0  # force EOS from the 3rd call onward
        else:
            out[..., 0] -= 1000.0  # deterministically suppress EOS on calls 1-2
        return out

    model.cmd_head.forward = fake_cmd_head
    image = torch.rand(1, 1, IMG_SIZE, IMG_SIZE)
    rows = model.generate(image, max_len=50)
    assert len(rows) == 2, f"expected exactly 2 real rows before EOS, got {len(rows)}"


def test_generate_respects_max_len_when_eos_never_predicted():
    model = _model()
    model.eval()
    with torch.no_grad():
        model.cmd_head.bias[0] = -100.0  # EOS logit crushed, never wins
    image = torch.rand(1, 1, IMG_SIZE, IMG_SIZE)
    rows = model.generate(image, max_len=7)
    assert len(rows) == 7
