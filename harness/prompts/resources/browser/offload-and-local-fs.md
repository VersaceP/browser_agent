---
id: browser.offload-and-local-fs
audience: browser
version: "2026-09-03"
description: Interpret offloaded observations and use local_fs paging without confusing historical evidence with live page state.
sources:
  - harness/offload.py
  - harness/local_fs.py
  - harness/tools/browser_tools/dispatch.py
related_tools:
  - local_fs_read
  - local_fs_search
  - find_in_axtree
related_methods:
  - DOM.getAXTree
topics:
  - offload
  - local filesystem
  - paging
  - large receipt
  - artifact read
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
epoch. Use local_fs_search/local_fs_read after a stale-epoch reply or when you
need historical line-level context the focused query cannot express.

local_fs_read is paged by line_offset, line_limit and max_bytes. Respect
truncated and nextLineOffset; totalLines tells you whether more content
exists. local_fs_search is separately bounded by max_results,
max_bytes_per_hit and max_total_bytes. Repeatedly reading the same unchanged
offload is not new page evidence; return to Page/DOM/Input perception or state
the evidence-bound blocker.
