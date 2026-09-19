# ADR: Local modular monolith for 001A

The spike uses one Python package for FastAPI and a separate worker command. Redis provides asynchronous work notifications while PostgreSQL remains the lifecycle source of truth. Video providers implement normalized contracts, so orchestration does not depend on Runway types. Local filesystem storage is the temporary artifact adapter. The mock provider and local storage are deliberately temporary and make the architecture testable without credentials or paid APIs.
