"""Retired generated-skill service: health and explicit compatibility errors only.

Task-scoped scripts require a separate process supervisor with kernel-enforced
isolation. This service deliberately does not import, compile or execute code.
"""

from fastapi import FastAPI, HTTPException

app = FastAPI(title="skill-runner (retired)", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "execution_enabled": False}


@app.post("/run/{skill_name}")
async def run_skill(skill_name: str) -> dict:
    raise HTTPException(410, "Generated skill execution is retired")


@app.post("/smoke")
async def smoke() -> dict:
    raise HTTPException(410, "Generated skill import checks are retired")
