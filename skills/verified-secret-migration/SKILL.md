---
name: verified-secret-migration
description: Use when moving a credential or secret into a secret manager without leaking it or destroying the source before byte-accurate readback verification.
---

# Verified Secret Migration

Use this for any migration from a local file, environment variable, password
store, CI variable, or one secret manager to another.

## Rules

- Never print the secret value.
- Preserve exact bytes. PEMs, JSON keys, certificates, and tokens can break if
  whitespace or trailing newlines are altered.
- Do not destroy or overwrite the source until readback proves the destination
  is non-empty and byte-equivalent.
- Empty source and empty destination is not a match; it is a failed extraction.

## Procedure

1. Read the source into a file or pipe that does not echo.
2. Assert the extracted byte length is greater than zero.
3. Compute a local hash of the exact bytes.
4. Store the exact bytes in the destination.
5. Read the destination back into a separate file or pipe.
6. Assert the readback byte length is greater than zero.
7. Compare source hash and readback hash.
8. Only after the hash matches, remove or rotate the old source if the task
   requires cleanup.

## Failure Handling

- If any read, write, length check, or hash check fails, leave the source intact.
- Report which check failed without including secret material.
- If the destination supports versions, create a new version rather than editing
  the existing value in place.

## Evidence

Record only safe metadata:

- source kind and destination kind
- byte length
- hash algorithm name, not necessarily the hash value if that is sensitive in
  the environment
- readback success/failure
- whether the source was intentionally retained or removed
