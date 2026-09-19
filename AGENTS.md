# Repository guardrails

- Keep this as a modular monolith. API and worker import the same `backend/app` package.
- Do not add microservices, Kubernetes, production deployment, or unapproved tables.
- Provider-specific request/response types stay inside provider adapters.
- Do not make real provider calls in 001A or commit secrets.
- Long-running generation and render operations use the Redis queue and persist lifecycle state in PostgreSQL.
- Keep frontend scope minimal until HYPE-STUDIO-001C.
