---
id: browser.offload-and-local-fs
audience: browser
version: "2026-09-15"
description: Interpret offloaded observations and use bounded local file tools without confusing historical evidence with live page state.
sources:
  - harness/context/offload.py
  - harness/tools/local_fs.py
  - harness/tools/browser_tools/dispatch.py
related_tools:
  - local_fs_read
  - local_fs_search
  - local_fs_batch
  - find_in_axtree
related_methods:
  - DOM.getAXTree
topics:
  - offload
  - local filesystem
  - paging
  - large receipt
  - artifact read
  - file delivery
  - file manifest
aliases:
  - 结果被卸载
  - 内容太大
  - 读文件
  - 分页读取
---
# Offloaded observations and local file reads

Large results may be represented by a savedPath, outline, format and
query_with. Those are pointers to persisted evidence, not a replacement for
fresh browser perception. A local_fs result proves only the file slice or
matches returned; a miss in a truncated search does not prove that a target is
absent.

For a current page view, use find_in_axtree first: it queries the in-memory
snapshot of the latest full view, including the one saved behind a change
list (`lines.delivery: "changes"`). A stale-snapshot reply requires a fresh
browser read before acting on a target. Use local_fs_search/local_fs_read for
historical line-level evidence or a bounded question the focused query cannot
express; those reads cannot make stale ids current. The platform's own
observation artifacts live on the browser host and are read by the harness;
never pass a host `artifact.path` to local_fs tools.

local_fs_read is paged by line_offset, line_limit and max_bytes. Respect
truncated and nextLineOffset; totalLines tells you whether more content
exists. local_fs_search is separately bounded by max_results,
max_bytes_per_hit and max_total_bytes. Repeatedly reading the same unchanged
offload is not new page evidence; return to Page/DOM/Input perception or state
the evidence-bound blocker.

Use local_fs_batch for delivery filesystem work that does not require browser
execution: list directories with op=list (recursive defaults to false), create
directories, write UTF-8 text or JSON, inspect size/hash, and copy authorized
files into their requested delivery layout. Directory listing has no file-count
cap. Large receipts may still be offloaded; inspect the persisted receipt.

External material and delivery paths require terminal confirmation before the
call executes. A phase's worker_contract.local_access_intent declares exact
scopes and modes, not permission: the harness preflights separate terminal
READ/WRITE decisions on the first relevant file call. Do not request each output
file individually when the declared delivery directory already covers verification.
Without an intent, plan the scope before submitting child operations. If the task
needs multiple sibling directories or all files under a material/delivery root,
request that common parent explicitly first: list/search the parent for READ,
or mkdir the delivery parent for WRITE. Once approved, batch child operations
under it; the same task reuses that permission throughout its descendants.
For example, when both /inputs/group-a and /inputs/group-b are needed, request
READ on /inputs first. When only /inputs/group-a is needed, request that child
alone. These are example paths, not default input locations.
A child approval does not cover its parent or siblings. Requesting a broader
parent later requires a new terminal approval, not a reinterpretation of the
child grant. Choose the root needed for the task, not an unrelated ancestor
such as the home directory. Use list/search to discover real child names;
search accepts path as its root.
Interactive confirmation pauses new tool dispatches and model turns across the
current task without a deadline. The original worker resumes the original call
after approval. Requests already dispatched may finish; terminal IO and browser
event handling remain live. Invalid input is not a refusal: enter yes or no.
Do not use browser Hitl.* or another tool to bypass a refused path.
Read and write approvals are separate, task-scoped and reused by continuations.
Only the displayed scope and its descendants are approved. Do not silently
expand an existing grant to a parent or request a parent to bypass a denial.
Symlinks resolving outside the approved root need their own authorization;
permissions do not carry over to another task. Do not retry alternate tools
after denial. Application/source and credential paths
are protected even under an approved parent. If no terminal is available,
report external_path_confirmation_required; do not use file:// as a bypass.
Download.start still obeys the browser's independent sandbox: when necessary,
stage in scratchpad and copy to the approved delivery directory. Batch independent
operations when practical and inspect every per-operation result; a partial
receipt means earlier operations may already have changed files. copy keeps
the source. The tool has no delete, move, shell, Python, or JavaScript surface.

Successful output files are registered to the current attempt and the tool
persists a browser-file-manifest-v1 receipt. Cite the manifest and declare the
same file paths in record_extraction rows so file validators check the intended
delivery set. Diagnostic screenshots or unrelated historical downloads do not
substitute for those declared files. Set overwrite only when replacing that
specific destination is part of the authorized task.

## Targeted search receipts

Search snippets are centered near the actual regex match on long lines.
`matchColumn` and `snippetStartColumn` are one-based character positions;
`line` is one-based, while `readHint.line_offset` is zero-based. A truncated
snippet is not the complete node/event. Use the returned readHint only if the
surrounding lines answer a remaining question. Reuse unchanged evidence; do not
repeat broad reads merely because a prior result was offloaded. A file search
is historical evidence, never a refresh of the live page or transport.
