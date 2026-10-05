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
Indexing sends chunk text and search sends query text to the Trussium runtime
configured by the operator; choose an endpoint whose privacy and retention
behavior is acceptable for that content. Use TLS across untrusted networks and
supply runtime credentials through a secret mechanism.
Treat indexed content as untrusted input. Tools must be explicitly registered
and bounded, and external writes require human approval. Never commit `.env`
files, model credentials, or private document samples.
