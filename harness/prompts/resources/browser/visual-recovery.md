---
id: browser.visual-recovery
audience: browser
version: "2026-09-04"
description: Use visual recovery and visual_locate only after structured recovery is exhausted.
sources:
  - harness/tools/browser_tools/visual.py
  - harness/tools/browser_tools/capability.py
  - harness/vl/locate.py
emitter_sources:
  - harness/tools/browser_tools/visual.py
related_tools:
  - visual_verify
  - browser_call
related_methods:
  - DOM.getAXTree
  - DOM.getSemanticTree
  - Input.click
error_codes:
  - mixed_bindings
  - conflicting_targets
topics:
  - visual recovery
  - visual locate
  - screenshot arbitration
  - coordinates
aliases:
  - 视觉定位
  - 截图判断
  - 看不到元素
  - 坐标点击
---
# Visual recovery

A `visualRecoveryHint` says visual location is available after deterministic
recovery failed. It is not an instruction to use vision or proof that the
target is actionable. Consider it only when the target is plausibly on screen
but AX/semantic DOM cannot name it, such as canvas UI, image-baked text or a
purely visual control.

Use visual_verify with mode `visual_locate` and one bounded expected target.
A returned `resolvedId` says where the node is, not what may be done to it.
It is the AX node that covered the located pixel, so choose the native method
permitted by that node's own role and the live schema. A
button can receive Input.click; a field can receive Input.type; a scrollable
ancestor can receive Input.scroll. A located option is not automatically a
valid Input.select control.

A located target is acted on by id wherever one exists. `cssPoint` is the
fallback when it does not: it is valid for exactly one `Input.click{pageId,x,y}`
on the current page state. It
comes only from a proven visual locate result, must not be persisted into a
skill, and must not survive page change. If `coordinateRefused` is present,
re-observe and use an id; never estimate coordinates from screenshots or AX
rectangles. Re-observe after acting. Safety policy still applies to every
located control.
