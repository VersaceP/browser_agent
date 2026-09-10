---
id: browser.file-upload
audience: browser
version: "2026-09-09"
description: Complete a native file chooser after an upload control was activated, without repeating the input action or waiting on chooser events.
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

Use this guide after a real page interaction has activated a file-upload
control.

1. Keep the current upload target reference from the latest AXTree or DOM
   observation.
2. Call `File.handleChooser` directly with that target and the files required
   by the task.
3. Do not wait for `File.chooserOpened` or `File.chooserClosed`, and do not
   repeat the click, key press, or coordinate click that opened the chooser.
4. If the chooser call reports a stale target or the page epoch changed, refresh
   `Page.getState` and `DOM.getAXTree`, derive a fresh upload target, then make
   one new `File.handleChooser` call.
5. Re-observe the page's upload state before treating the upload as complete.

The native chooser is an attachment mechanism, not page-state proof. A handled
chooser receipt proves the selection attempt; verify any user-visible upload
status separately. Directory uploads require HITL.
