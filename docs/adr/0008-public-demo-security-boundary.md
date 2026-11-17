# ADR 0008: Public demo security boundary

## Status

Accepted for an opt-in, single-instance demonstration mode. This decision does
not authorize or deploy a public service. An internet-facing deployment requires
an independently rehearsed trusted ingress and operational controls described
in the operations guide.

## Context

The browser question endpoint invokes configured embedding and chat models.
The ordinary self-hosted interface has no user authentication and is intended
for loopback or trusted private networks. Making it public without a bounded
identity and usage boundary permits automated request abuse, unexpected model
usage, and access to every indexed source.

## Decision

- Keep public demo mode disabled unless
  `KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE=true` is explicitly set. Invalid or
  incomplete configuration prevents application startup.
- Require a trusted HTTPS ingress to authenticate or rate-limit visitors and
  mint a short-lived HS256 identity token in the secure, host-only
  `__Host-tka-demo-identity` cookie. The application verifies signature,
  issuer, audience, origin, issue/expiry times, subject, and client IP. The
  signing secret is shared only through protected secret configuration. Do not
  trust caller-supplied forwarded-IP headers. Network policy must prevent
  bypassing ingress and reaching the application directly.
- Require same-origin requests and a curated allowlist of source IDs. At
  startup, verify every source exists, is active, and is not bound to private
  GitHub authorization. Keep GitHub App/webhook and MCP tool credentials unset.
- Apply a request body cap, per-subject and per-IP sliding-window limits,
  bounded concurrency, existing Trussium client timeouts, and an atomic
  PostgreSQL UTC-day provider-call counter. Reserve one unit before each
  embedding or chat provider call. The counter limits calls, not provider
  dollars or tokens; set a separate provider-side spend limit where available.
- Keep rate/concurrency enforcement single-process unless an external shared
  limiter is configured. The daily provider-call budget is shared across
  instances through PostgreSQL. A multi-worker or multi-instance deployment
  must also enforce shared edge-level rate and concurrency limits.
- Provide a kill switch that returns unavailable for API routes, reject the
  MCP route in public-demo mode, and expose no private source connector in this
  mode. Do not log questions, answers, credentials, or identity tokens.
- Do not change the default local/self-hosted deployment or configure a public
  URL as part of this change.

## Consequences

The application can enforce a narrow, opt-in request envelope, but a secure
public deployment still depends on an external ingress, network isolation,
protected secrets, a deliberately curated corpus, provider-side controls, and
an operational rehearsal. The database call budget is durable and atomic;
the in-process rate and concurrency limits require one worker or a shared edge
limiter. The daily counter retains a small aggregate row per UTC day and
contains no user identifiers.

## Alternatives considered

- **Expose the ordinary browser endpoint directly:** rejected because it has
  no identity boundary or abuse controls.
- **Trust arbitrary `X-Forwarded-For` headers:** rejected because a direct
  caller could forge its IP identity.
- **Build an authentication product in the application:** deferred. A trusted
  ingress can provide a narrowly scoped signed identity without adding an
  account system or tenant model.
- **Use only an in-memory model-call quota:** rejected because it resets on
  restart and is not shared between instances.

## Validation gate

Before sharing a URL, rehearse the actual ingress and direct-origin network
boundary, secret rotation, authenticated and rejected requests, rate and
concurrency behavior, request-size enforcement, daily quota exhaustion and
UTC-day reset, kill switch, curated-source startup checks, privacy/retention,
monitoring, backup, and rollback. Do not call the service public-ready until
that evidence is recorded.
