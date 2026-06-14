---
name: authoring-html-email
description: Use when composing, implementing, sending, or reviewing HTML email so the message has correct multipart MIME, inline styles, client-safe markup, link/image handling, and render/MIME verification.
---

# Authoring HTML Email

HTML email is not a web page. Build for conservative mail clients and verify the
actual received message, not just the send call.

## MIME Contract

- Send `multipart/alternative` for every HTML email.
- Attach `text/plain` first and `text/html` second.
- The plain part must be readable on its own; do not use an empty placeholder.
- Use UTF-8 for both parts.
- If attachments are needed, wrap the alternative body inside
  `multipart/mixed`; do not replace the alternative body with one HTML part.

## HTML Constraints

- Use simple semantic structure: headings, paragraphs, lists, links, and tables
  only when table layout is needed.
- Inline critical CSS. Many clients strip `<style>`, external CSS, scripts,
  forms, and complex positioning.
- Keep widths responsive and conservative. Avoid relying on flexbox, grid,
  JavaScript, web fonts, or background images for meaning.
- Escape user-provided content before inserting it into HTML.
- Make links absolute, descriptive, and visible in the plain-text fallback.

## Images And Attachments

- Prefer hosted HTTPS images only when the recipient can tolerate remote-image
  blocking.
- Include meaningful alt text for images.
- For required artifacts, attach the file or link to a durable location; do not
  make comprehension depend on a remote image rendering.

## Verification

Before claiming an HTML email works:

- Parse the outbound raw MIME and assert `multipart/alternative` contains both
  `text/plain` and `text/html`.
- Render or inspect the HTML in at least one realistic email client or browser
  harness for layout regressions.
- Fetch the sent or received message and verify recipient, subject, MIME shape,
  HTML body, plain fallback, links, and attachments.
- Treat send logs or API message IDs as insufficient by themselves.
