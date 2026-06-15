---
name: authoring-html-email
description: Build client-facing HTML email that actually renders — correct multipart MIME (HTML + plain-text fallback ALWAYS), inline styles, and verify by rendering the received message.
---

# authoring-html-email

Email is not the web. Clients (Gmail, Outlook, Apple Mail) strip `<head>`/`<style>`, ignore most CSS, and some show only plain text. A "looks fine in my browser" HTML email arrives broken or as raw tags. Build for the medium.

## The non-negotiable: multipart/alternative with BOTH parts
EVERY client-facing email MUST be `multipart/alternative` carrying **both** a `text/html` part AND a `text/plain` fallback — always, no exceptions. Clients that can't/won't render HTML show the plain part; a missing plain part = some recipients see nothing useful (or your HTML as raw source). Order matters: plain-text first, HTML second (clients pick the LAST part they support).

```
Content-Type: multipart/alternative; boundary="b"
--b
Content-Type: text/plain; charset="utf-8"
<readable plain version>
--b
Content-Type: text/html; charset="utf-8"
<the HTML>
--b--
```

## HTML that survives email clients
- **INLINE every style** (`style="..."` on each element). `<style>` blocks and external CSS are stripped by major clients. Use a tool/step that inlines CSS before send.
- **Tables for layout**, not flexbox/grid (Outlook uses Word's rendering engine). Set explicit widths; avoid negative margins, position, float.
- **Images:** absolute https URLs (no `cid:`/attachments unless intentional); ALWAYS set `alt` (many clients block images by default) and width/height; don't rely on background-images.
- **Links:** full absolute URLs; don't hide critical info behind images-only.
- **Keep it narrow** (~600px), web-safe fonts with fallbacks, sufficient contrast.

## Verify BY RENDERING the received message (not the send log)
"Sent" ≠ "renders". Verification = inspect the ACTUALLY-RECEIVED message: confirm it's `multipart/alternative` with both parts (read the raw MIME), and that it renders as HTML in a real client (or a rendering preview), images/links work, and the plain-text fallback is readable. A send-API 200 proves nothing about rendering.

## DONE means
The received message is multipart/alternative with a text/plain fallback AND a text/html part; styles are inlined; it renders correctly in a real client; verified by inspecting the received MIME + rendering — not by the send log.
