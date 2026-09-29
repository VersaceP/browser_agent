---
id: browser.file-upload
audience: browser
version: "2026-09-26"
description: Assign files to a current upload input, then verify page acceptance and diagnose an unconfirmed result.
sources:
  - abcp-platform/resources/skills/webcross-browser/SKILL.md
  - harness/tools/browser_tools/axtree_state.py
  - harness/tools/browser_tools/capability.py
emitter_sources:
  - harness/tools/browser_tools/axtree_state.py
  - harness/tools/browser_tools/validation.py
related_tools:
  - browser_call
related_methods:
  - Input.click
  - Input.press
  - Page.click
  - File.handleChooser
  - Page.getState
  - DOM.getAXTree
  - Network.readApi
topics:
  - file upload
  - file chooser
  - upload control
  - stale target
aliases:
  - 上传文件
  - 文件选择器
  - 上传控件
  - 选择文件
---
# File upload

Use the current page view to identify the actual file input and the result
region. Read the input's `accept`, `multiple` and related visible requirements
when they matter to the files being assigned. Do not infer support from a
filename alone.

1. When the actual file input is known, call `File.handleChooser` with its
   current id or unique selector. A preceding click is not required by this
   Action. If a visible wrapper must be activated to expose the input, click it
   once and follow the returned file-upload target without repeating the click.
2. Do not wait for `File.chooserOpened` or `File.chooserClosed`. If the target
   is stale or the document changed, refresh `Page.getState` and `DOM.getAXTree`
   to obtain a current input before assigning files.
3. Verify the page's result region: accepted count, thumbnails, upload status,
   error text or the next required submit step. `handled` and
   `selectedFileCount` confirm the assignment request, not page acceptance.
4. When that result remains unconfirmed, inspect only the missing evidence.
   The input's attributes, visible requirements and current error can explain
   a client-side refusal. `Network.readApi` can inspect Fetch/XHR/Beacon
   requests initiated since the operation, if this page and task permit it.
   Call it directly, outside `Workflow.execute`, so its result is sanitized
   before logging or delivery.
   Start with the time window and no success/failure `responsePattern`; narrow
   by URL only after observing the request. Check `pendingCount`,
   `collectionStartedAt`, `oldestAvailableAt` and `evictedCount` before treating
   an empty match as evidence that no request was captured.
5. Interpret HTTP status, available business response and the page result
   together. HTTP 200 alone does not establish acceptance. Missing captured
   requests do not establish why an upload failed. Avoid repeating an
   uncertain assignment until fresh evidence supports a safe next attempt.

Network request bodies and credential fields in JSON response bodies are
redacted by the harness; unstructured text bodies may be withheld. Use the
remaining metadata and page state for the decision. If the accepted result
cannot be established, keep the upload objective open and report the evidence
and blocker to Lead. Directory uploads require HITL.
