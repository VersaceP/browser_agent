---
id: browser.offload-and-local-fs
audience: browser
version: "2026-09-15"
description: Interpret offloaded observations and use bounded local file tools without confusing historical evidence with live page state.
sources:
  - harness/offload.py
  - harness/local_fs.py
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

For a current offloaded AX tree, use find_in_axtree first when its liveQuery
path is available. It queries the in-memory snapshot and preserves the current
epoch. A stale-epoch reply requires fresh browser observation before acting on a
target. Use local_fs_search/local_fs_read for historical line-level evidence
or a bounded question the focused query cannot express; those reads cannot
make stale ids current.

local_fs_read is paged by line_offset, line_limit and max_bytes. Respect
truncated and nextLineOffset; totalLines tells you whether more content
exists. local_fs_search is separately bounded by max_results,
max_bytes_per_hit and max_total_bytes. Repeatedly reading the same unchanged
offload is not new page evidence; return to Page/DOM/Input perception or state
the evidence-bound blocker.

Use local_fs_batch for delivery filesystem work that does not require browser
execution: create directories, write UTF-8 text or JSON, inspect size/hash, and
copy task files into their requested delivery layout. Batch independent
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
