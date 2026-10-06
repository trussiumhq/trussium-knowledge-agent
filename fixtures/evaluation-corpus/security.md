# Data boundaries

## Local file access

Indexing is explicit and local. The indexer skips symbolic links and does not
read files outside the selected source root. Markdown files larger than 5 MiB
or containing invalid UTF-8 abort before database state changes.

## Runtime data handling

Document chunks are sent to the configured Trussium runtime for embeddings.
Questions and retrieved passages are sent to its chat endpoint when using the
`ask` command. Choose a runtime whose data handling is appropriate for those
documents.
