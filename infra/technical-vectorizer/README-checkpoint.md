# Checkpoint provenance

`model_lines.weights` — the official pretrained "line" model published with
Deep Vectorization of Technical Drawings (Egiazarian et al., ECCV 2020,
MPL-2.0): https://github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings

Not redistributed in this repo (149MB binary). Download and place at the
path referenced by `TECHNICAL_VECTORIZER_CHECKPOINT` (default
`/models/model_lines.weights`, i.e. into the `technical_vectorizer_models`
Docker volume):

```
https://disk.yandex.ru/d/FKJuMvNJuy-K9g
```

The upstream README's Google Drive mirror did not resolve at time of
writing (quota/permissions) — the Yandex Disk link above is the one
actually verified working. Resolve to a direct download URL via the public
API if scripting this:

```bash
curl -s "https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key=https://disk.yandex.ru/d/FKJuMvNJuy-K9g" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['href'])"
```

A companion "curve" checkpoint also exists
(`https://disk.yandex.ru/d/yOZzCSrd-QSACA`) but is deliberately NOT used
here — tested live (2026-07-11) and found to hurt precision more than it
helps recall on this project's real drawings (it duplicates noisy Bezier
approximations of the straight lines the line-model already covers well).
Circles/arcs stay on the CV recognizer path.
