# Backend migrations

Alembic migrations are the production schema path.

Development may still use `AUTO_CREATE_SCHEMA=true` for fast local SQLite startup. Production must set:

```bash
APP_ENV=production
AUTO_CREATE_SCHEMA=false
alembic upgrade head
```

`create_all` is blocked when `APP_ENV=production` and `AUTO_CREATE_SCHEMA=true`.
