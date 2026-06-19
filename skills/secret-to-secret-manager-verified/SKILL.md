---
name: secret-to-secret-manager-verified
description: Use when migrating a secret from any source into a secret manager. Requires non-empty extraction, exact-byte preservation, readback hash verification, and source destruction only after verified match.
---

# Secret To Secret Manager, Verified

Use this skill when moving a credential, token, private key, certificate, or
other secret into a managed secret store.

## Protocol

1. Read the source secret without printing it.
2. Assert the extracted byte length is greater than zero.
3. Store the exact bytes in the secret manager.
4. Read the stored value back from the secret manager.
5. Assert the readback byte length is greater than zero.
6. Compare a cryptographic hash of the source bytes to a hash of the readback
   bytes.
7. Destroy, rotate, or remove the source only after the non-empty hashes match.

If any step fails, leave the source intact and report the exact failed check
without exposing the secret value.

## Byte Handling

- Treat PEM, SSH keys, certificates, and multiline tokens as byte-sensitive.
- Do not trim whitespace, normalize newlines, rewrap lines, JSON-escape by
  hand, or paste values through chat.
- Prefer byte-preserving pipes or files with restrictive permissions for the
  short handoff window.
- Remove temporary files after successful readback verification.

## Anti-Patterns

- Comparing empty source and empty readback hashes.
- Accepting a secret-manager write response without reading the value back.
- Destroying the old source before proving the new source contains the same
  non-empty bytes.
- Logging the secret to prove it was copied.
- Manually editing PEM contents to make a command accept them.

## Report Shape

Use this compact report:

`Secret migration verified: source_len=<n>; destination_len=<n>; hash_match=<true>; source_destroyed=<only after verified match or no>.`
