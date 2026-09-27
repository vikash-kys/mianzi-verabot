# Vera bot — magicpin AI Challenge

HTTP bot for the magicpin AI Challenge judge harness.

Endpoints: `GET /v1/healthz`, `GET /v1/metadata`, `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`.

Composes grounded merchant/customer WhatsApp messages from category, merchant, trigger and customer context. Deterministic; no API keys required.

Run: `docker build -t vera . && docker run -p 7860:7860 vera`
