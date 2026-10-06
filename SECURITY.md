# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities through GitHub's private vulnerability
reporting feature for this repository. Do not file public issues containing
exploit details, credentials, private documents, or personal data.

Include the affected version or commit, impact, and a minimal reproduction when
possible. The maintainers will acknowledge reports and coordinate a fix and
public disclosure.

## Security boundaries

This project is an early reference application. Do not expose it to untrusted
networks with real documents or credentials until deployment hardening is
complete. Index only a repository you are authorized to access. The indexer
must stay beneath the explicitly selected root and ignore symbolic links.
Indexing sends chunk text, search sends query text, and `ask` sends the question
and retrieved passages to the configured Trussium runtime. Choose endpoints
whose privacy and retention behavior is acceptable for that content. Use TLS
across untrusted networks and supply runtime credentials through a secret
mechanism.

Retrieved passages are untrusted data and may contain prompt-injection attempts.
The answer flow directs the model to ignore embedded instructions, validates a
constrained JSON response, and displays citations only when their IDs resolve
to retrieved records. These checks reduce citation/link fabrication but do not
guarantee factual accuracy or eliminate prompt injection. Tools must be
explicitly registered and bounded, and external writes require human approval.
Never commit `.env` files, model credentials, or private document samples.
