"""Lossless, task-owned conversation objects and one atomic current checkpoint.

Only storage changes: providers still receive complete Tau messages, including
original image bytes and opaque thinking/signatures. Historical checkpoints
refer to immutable message/image resources, never to a newer current head.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from contextlib import contextmanager
from typing import Any

from harness.storage.base import StorageCorruptError

HEAD_PATH = "workflow/context.json"
HISTORY_PATH = "workflow/messages.json"
CHECKPOINT_KIND = "workflow_conversation_checkpoint"
OBJECT_KIND = "workflow_conversation_object"
CHECKPOINT_FORMAT = "workflow-conversation-v1"
MESSAGE_FORMAT = "workflow-conversation-message-v1"
IMAGE_FORMAT = "workflow-conversation-image-v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


def is_checkpoint(value: Any) -> bool:
    return isinstance(value, dict) and value.get("format") == CHECKPOINT_FORMAT


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _corrupt(reason: str) -> StorageCorruptError:
    return StorageCorruptError("Conversation checkpoint: " + reason)


class ConversationStore:
    """Save a complete history/context pair using small immutable references.

    ``save`` takes Tau messages or their JSON dictionaries. ``load`` returns
    legacy-compatible JSON lists. ``save_context`` is the compactor's durable
    callback and commits the same already-saved history with the new context.
    Object caches avoid repeated writes, not content checks: every supplied
    message is dumped and hashed anew, so changing an existing object is kept.
    """

    def __init__(self, store: Any):
        self.store, self.task_id = store, store.logger.task_id
        self._images: dict[tuple[str, str], dict[str, str]] = {}
        self._messages: dict[str, dict[str, str]] = {}
        self._decoded_images: dict[str, dict[str, Any]] = {}
        self._decoded_messages: dict[str, dict[str, Any]] = {}
        self._history_refs: list[dict[str, str]] | None = None
        self._legacy_history: list[Any] = []
        self._legacy_context: list[Any] | None = None
        self.last_metrics: dict[str, Any] | None = None
        self._metrics: dict[str, Any] | None = None

    @contextmanager
    def _timed(self, key: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            if self._metrics is not None:
                self._metrics[key] += (time.perf_counter() - started) * 1000

    def _digest(self, value: Any) -> str:
        with self._timed("encode_hash_ms"):
            raw = _canonical(value)
            result = hashlib.sha256(raw).hexdigest()
        if self._metrics is not None:
            self._metrics["canonical_bytes_hashed"] += len(raw)
        return result

    def _owner(self, value: Any, expected: str, fields: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != fields or value.get("format") != expected:
            raise _corrupt("unknown or invalid " + expected + " format")
        if value.get("taskId") != self.task_id:
            raise _corrupt("object belongs to a different task")
        return value

    def _descriptor(self, value: Any) -> dict[str, str]:
        if (not isinstance(value, dict) or set(value) != {"ref", "sha256"}
                or not isinstance(value.get("ref"), str) or not value["ref"]
                or not isinstance(value.get("sha256"), str)
                or not _DIGEST.fullmatch(value["sha256"])):
            raise _corrupt("invalid object reference")
        return value

    def _raw_ref(self, reference: str) -> Any:
        with self._timed("lookup_ms"):
            return self.store._read_ref_raw(reference, strict=True)

    def _raw_path(self, path: str) -> Any:
        with self._timed("lookup_ms"):
            return self.store._read_raw(path, _MISSING, strict=True)

    def _write_object(self, path: str, value: Any, digest: str, kind: str) -> dict[str, str]:
        with self._timed("object_save_ms"):
            reference = self.store.write(path, value, OBJECT_KIND)
        if self._metrics is not None:
            self._metrics[kind + "_objects_written"] += 1
        return {"ref": reference, "sha256": digest}

    def _image_key(self, image: dict[str, Any]) -> tuple[str, str]:
        # CPython caches the hash of an immutable str. This avoids repeatedly
        # JSON-encoding a base64 payload; all non-data image fields remain part
        # of the key and the first content digest.
        return (json.dumps({k: v for k, v in image.items() if k != "data"},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":")), image["data"])

    def _encode_image(self, image: dict[str, Any]) -> dict[str, str]:
        if not isinstance(image.get("data"), str):
            raise _corrupt("image data is not a string")
        with self._timed("lookup_ms"):
            key = self._image_key(image)
            known = self._images.get(key)
        if known is not None:
            return dict(known)
        digest = self._digest(image)
        path = f"workflow/conversation/images/{digest}.json"
        previous = self._raw_path(path)
        if previous is not _MISSING:
            self._validate_image(previous, digest)
            # Obtain this backend's owned immutable URI, not a file-system
            # alias that a database backend cannot resolve.
            # An unreferenced file can be an orphan of an interrupted write.
            # Re-persist it before publishing a new head so File durability is
            # re-established; ordinary resumes populate caches from the head.
            descriptor = self._write_object(path, previous, digest, "image")
        else:
            value = {"format": IMAGE_FORMAT, "taskId": self.task_id, "image": image}
            descriptor = self._write_object(path, value, digest, "image")
        self._images[key] = descriptor
        return dict(descriptor)

    def _encode_message(self, message: Any) -> dict[str, str]:
        with self._timed("message_dump_ms"):
            if hasattr(message, "model_dump"):
                value = message.model_dump(by_alias=True, exclude_none=True)
            else:
                value = copy.deepcopy(message)
        if not isinstance(value, dict):
            raise _corrupt("message is not an object")
        images = []
        content = value.get("content")
        if isinstance(content, list):
            for index, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "image":
                    descriptor = self._encode_image(block)
                    images.append({"index": index, **descriptor})
                    content[index] = {"imageDigest": descriptor["sha256"]}
        identity = {"message": value, "images": [
            {"index": item["index"], "sha256": item["sha256"]} for item in images]}
        digest = self._digest(identity)
        with self._timed("lookup_ms"):
            descriptor = self._messages.get(digest)
        if descriptor is None:
            path = f"workflow/conversation/messages/{digest}.json"
            previous = self._raw_path(path)
            if previous is not _MISSING:
                self._validate_message(previous, digest)
                descriptor = self._write_object(path, previous, digest, "message")
            else:
                descriptor = self._write_object(path, {"format": MESSAGE_FORMAT,
                    "taskId": self.task_id, "message": value, "images": images}, digest, "message")
            self._messages[digest] = descriptor
        return dict(descriptor)

    def _validate_image(self, value: Any, digest: str) -> dict[str, Any]:
        row = self._owner(value, IMAGE_FORMAT, {"format", "taskId", "image"})
        image = row["image"]
        if (not isinstance(image, dict) or image.get("type") != "image"
                or not isinstance(image.get("data"), str) or self._digest(image) != digest):
            raise _corrupt("image content digest mismatch")
        return image

    def _decode_image(self, descriptor: Any) -> dict[str, Any]:
        descriptor = self._descriptor(descriptor)
        reference, digest = descriptor["ref"], descriptor["sha256"]
        if reference not in self._decoded_images:
            image = self._validate_image(self._raw_ref(reference), digest)
            self._decoded_images[reference] = image
            self._images[self._image_key(image)] = dict(descriptor)
        else:
            image = self._decoded_images[reference]
            if self._digest(image) != digest:
                raise _corrupt("conflicting image reference digest")
        return image

    def _validate_message(self, value: Any, digest: str) -> dict[str, Any]:
        row = self._owner(value, MESSAGE_FORMAT, {"format", "taskId", "message", "images"})
        message, images = row["message"], row["images"]
        if not isinstance(message, dict) or not isinstance(images, list):
            raise _corrupt("invalid message payload")
        indices = []
        for image in images:
            if not isinstance(image, dict) or set(image) != {"index", "ref", "sha256"}:
                raise _corrupt("invalid image binding")
            self._descriptor({key: image[key] for key in ("ref", "sha256")})
            index = image["index"]
            content = message.get("content")
            if (isinstance(index, bool) or not isinstance(index, int) or index < 0
                    or not isinstance(content, list) or index >= len(content)
                    or content[index] != {"imageDigest": image["sha256"]}):
                raise _corrupt("image binding does not match message")
            indices.append(index)
        if indices != sorted(set(indices)):
            raise _corrupt("duplicate or unordered image bindings")
        identity = {"message": message, "images": [
            {"index": item["index"], "sha256": item["sha256"]} for item in images]}
        if self._digest(identity) != digest:
            raise _corrupt("message content digest mismatch")
        return row

    def _decode_message(self, descriptor: Any) -> dict[str, Any]:
        descriptor = self._descriptor(descriptor)
        reference, digest = descriptor["ref"], descriptor["sha256"]
        if reference in self._decoded_messages:
            value = self._decoded_messages[reference]
            if value["digest"] != digest:
                raise _corrupt("conflicting message reference digest")
            return copy.deepcopy(value["message"])
        row = self._validate_message(self._raw_ref(reference), digest)
        message = copy.deepcopy(row["message"])
        for item in row["images"]:
            message["content"][item["index"]] = copy.deepcopy(self._decode_image(
                {key: item[key] for key in ("ref", "sha256")}))
        self._messages[digest] = dict(descriptor)
        self._decoded_messages[reference] = {"digest": digest, "message": message}
        return copy.deepcopy(message)

    def restore(self, head: Any) -> tuple[list[Any], list[Any]]:
        row = self._owner(head, CHECKPOINT_FORMAT,
                          {"format", "taskId", "history", "context", "sha256"})
        digest = row["sha256"]
        if (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or self._digest({k: v for k, v in row.items() if k != "sha256"}) != digest):
            raise _corrupt("checkpoint content digest mismatch")
        if not isinstance(row["history"], list) or not isinstance(row["context"], list):
            raise _corrupt("invalid message sequences")
        # Restore both sequences before publishing either to the caller. An
        # inaccessible object is corruption, never an empty or partial history.
        history = [self._decode_message(item) for item in row["history"]]
        context = [self._decode_message(item) for item in row["context"]]
        self._history_refs = copy.deepcopy(row["history"])
        self._legacy_history = []
        self._legacy_context = None
        return history, context

    def load(self) -> tuple[list[Any], list[Any]]:
        head = self._raw_path(HEAD_PATH)
        if head is _MISSING:
            head = []
        if isinstance(head, dict):
            return self.restore(head)
        if not isinstance(head, list):
            raise _corrupt("legacy context is not an array")
        history = self._raw_path(HISTORY_PATH)
        if history is _MISSING:
            history = []
        if not isinstance(history, list):
            raise _corrupt("legacy history is not an array")
        self._legacy_history = history
        self._legacy_context = head
        return history, head

    def _archive(self, head: dict[str, Any]) -> str:
        path = f"workflow/conversation/checkpoints/{head['sha256']}.json"
        previous = self._raw_path(path)
        if previous is not _MISSING:
            if previous != head:
                raise _corrupt("immutable checkpoint archive changed")
        with self._timed("checkpoint_save_ms"):
            reference = self.store.write(path, head, OBJECT_KIND)
        if self._metrics is not None:
            self._metrics["checkpoint_archives_written"] += 1
        return reference

    def _head(self, history: list[Any], context: list[Any]) -> dict[str, Any]:
        head = {"format": CHECKPOINT_FORMAT, "taskId": self.task_id,
                "history": history, "context": context}
        head["sha256"] = self._digest(head)
        return head

    def _persist(self, context: list[Any], history: list[Any] | None) -> str:
        started = time.perf_counter()
        metrics = {key: 0.0 for key in ("message_dump_ms", "encode_hash_ms", "lookup_ms",
                   "object_save_ms", "checkpoint_save_ms")}
        metrics.update({key: 0 for key in ("canonical_bytes_hashed", "image_objects_written",
                                          "message_objects_written", "checkpoint_archives_written")})
        self._metrics = metrics
        try:
            if history is not None:
                history_refs = [self._encode_message(item) for item in history]
            elif self._history_refs is not None:
                history_refs = copy.deepcopy(self._history_refs)
            else:
                self.load()
                history_refs = (copy.deepcopy(self._history_refs) if self._history_refs is not None
                    else [self._encode_message(item) for item in self._legacy_history])
            context_refs = [self._encode_message(item) for item in context]
            head = self._head(history_refs, context_refs)
            if self._legacy_context is not None:
                # Preserve a pre-upgrade File context too, even when a first
                # compaction changes it before the new head is published.
                legacy = self._head([self._encode_message(item) for item in self._legacy_history],
                                    [self._encode_message(item) for item in self._legacy_context])
                self._archive(legacy)
            reference = self._archive(head)
            # Objects exist and are independently verifiable before this
            # single atomic publish. A failure above leaves the old head live.
            with self._timed("checkpoint_save_ms"):
                self.store.write(HEAD_PATH, head, CHECKPOINT_KIND)
            self._history_refs = history_refs
            self._legacy_history = []
            self._legacy_context = None
            metrics.update(history_messages=len(history_refs), context_messages=len(context_refs),
                           total_ms=(time.perf_counter() - started) * 1000)
            self.last_metrics = dict(metrics)
            self.store.logger.write("workflow.conversation.persist", metrics)
            return reference
        finally:
            self._metrics = None

    def save(self, history: list[Any], context: list[Any]) -> str:
        return self._persist(context, history)

    def save_context(self, context: list[Any]) -> str:
        return self._persist(context, None)
