# agent-script-supervisor (E27)

Один запуск кода агента — один свежий контейнер: сеть `none`, корень только
для чтения, `tmpfs /tmp` 512 МБ, 1 ГБ памяти, 1 CPU, 64 PID, uid 65534 без
capabilities. Контракт и модель угроз —
`docs/agent-employee-delivery/script-isolation-contract.md`.

Сервис выключен по умолчанию (профиль `scripts`), агент его пока не вызывает.

```bash
docker build -t aiw-script-runtime:py311 infra/agent-script-supervisor/runtime/
# SCRIPT_RUNTIMES в infra/.env: {"python3.11": "<image id из docker images --no-trunc>"}
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --profile scripts up -d agent-script-supervisor
cd infra/agent-script-supervisor && python3 -m pytest tests -q
```
