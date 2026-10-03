"""Shared browser capability bootstrap; no model planning."""
from __future__ import annotations

import shutil
import os
import time
import uuid
from dataclasses import replace
from abcp_client import ABCPClient, ABCP_TRANSPORT_CONNECT_FAILED
from harness.capabilities.schema_loader import _capability_actions_from_response, _capability_revisions_from_response, _agent_guide_from_capabilities_response, load_capability_bundle
from harness.capabilities.schema_cache import SCHEMA_CONTRACT_GENERATION, capability_hash, global_schema_cache_dir, global_schemas_dir, read_cached_capability_hash, read_cached_capability_metadata, read_schema_methods_from_dirs, schema_bootstrap_lock, write_cached_capability_hash, write_cached_agent_guide
from harness.workflow.workflow_schema_source import bind_schemas_dir, contract_source
from harness.tools.tool_policy import ALWAYS_FORBIDDEN_ABCP_METHODS as _BLOCKED_CAPABILITIES
from harness.utils import JsonDict, make_browser_event_logger

async def bootstrap_schema_cache(self) -> None:
    # Assume healthy; any degraded exit below flips this so _schema_cache_status
    # degrades plan validation instead of trusting a possibly-stale cache.
    self._schema_bootstrap_degraded = False
    self._browser_connection_failure = None
    browser = None
    bootstrap_started = time.monotonic()
    timings: JsonDict = {}
    outcome = "failed"
    cache_mode = "unknown"
    cache_dir = global_schema_cache_dir(self.runtime.harness.worktree_dir)
    schemas_dir = global_schemas_dir(self.runtime.harness.worktree_dir)
    # Point the workflow contract at the directory THIS run uses, before
    # the bootstrap writes it. The model-facing workflow schema, the
    # workflow policy and the tool-schema cache stamp all derive from that
    # contract; left to resolve on their own they read a default location
    # that only coincides with this one under the default worktree_dir.
    bind_schemas_dir(schemas_dir)
    tmp_schemas_dir = cache_dir / f"schemas.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        browser_config = replace(
            self.runtime.browser,
            connect_timeout_seconds=min(
                float(self.runtime.browser.connect_timeout_seconds),
                5.0,
            ),
            call_timeout_seconds=min(
                float(self.runtime.browser.call_timeout_seconds),
                20.0,
            ),
        )
        event_logger = make_browser_event_logger(
            self.logger,
            self.runtime.harness.log_browser_payloads,
            prefix="schema-bootstrap.transport",
        )
        connect_started = time.monotonic()
        async with ABCPClient(browser_config, on_event=event_logger) as browser:
            timings["connectMs"] = int(
                (time.monotonic() - connect_started) * 1000
            )
            register_started = time.monotonic()
            await browser.call(
                "System.register",
                {},
            )
            timings["registerMs"] = int(
                (time.monotonic() - register_started) * 1000
            )
            capabilities_started = time.monotonic()
            caps_response = await browser.call(
                "System.getCapabilities", {"guide": "content"}
            )
            timings["getCapabilitiesMs"] = int(
                (time.monotonic() - capabilities_started) * 1000
            )
            capabilities = _capability_actions_from_response(caps_response)
            revisions = _capability_revisions_from_response(caps_response)
            agent_guide = _agent_guide_from_capabilities_response(caps_response)
            guide_path = write_cached_agent_guide(cache_dir, agent_guide)
            if not capabilities:
                self.logger.write(
                    "schema.bootstrap.failed",
                    {
                        "reason": "empty_capabilities",
                        "dataShape": (
                            type(caps_response.get("data")).__name__
                            if isinstance(caps_response, dict)
                            else type(caps_response).__name__
                        ),
                        "fallback": "validate_task_plan will skip unknown-method check",
                    },
                )
                self._schema_bootstrap_degraded = True
                outcome = "empty_capabilities"
                return
            cache_check_started = time.monotonic()
            digest = capability_hash(
                capabilities,
                policy_fingerprint=_BLOCKED_CAPABILITIES,
                generation=SCHEMA_CONTRACT_GENERATION,
                catalog_revision=revisions["catalogRevision"],
            )
            cached_digest = read_cached_capability_hash(cache_dir)
            cached_metadata = read_cached_capability_metadata(cache_dir)
            cached_methods = read_schema_methods_from_dirs([schemas_dir])
            timings["cacheCheckMs"] = int(
                (time.monotonic() - cache_check_started) * 1000
            )
            if cached_digest == digest and cached_methods:
                # Upgrade legacy hash-only manifests in place so the next
                # catalog change invalidates the complete schema set.
                if (
                    cached_metadata.get("generation")
                    != SCHEMA_CONTRACT_GENERATION
                    or cached_metadata.get("catalog_revision")
                    != revisions["catalogRevision"]
                    or cached_metadata.get("guide_revision")
                    != revisions["guideRevision"]
                ):
                    write_cached_capability_hash(
                        cache_dir,
                        digest=digest,
                        capability_count=len(capabilities),
                        generation=SCHEMA_CONTRACT_GENERATION,
                        catalog_revision=revisions["catalogRevision"],
                        guide_revision=revisions["guideRevision"],
                    )
                self.logger.write(
                    "schema.bootstrap.cached",
                    {
                        "cacheDir": str(cache_dir.resolve()),
                        "schemaCount": len(cached_methods),
                        "capabilityHash": digest,
                        "catalogRevision": revisions["catalogRevision"] or None,
                        "guideRevision": revisions["guideRevision"] or None,
                        "agentGuidePath": guide_path,
                    },
                )
                cache_mode = "hit"
                outcome = "cached"
                return

            with schema_bootstrap_lock(cache_dir, timeout_seconds=10.0) as acquired:
                if not acquired:
                    cached_digest = read_cached_capability_hash(cache_dir)
                    cached_methods = read_schema_methods_from_dirs([schemas_dir])
                    if cached_digest == digest and cached_methods:
                        self.logger.write(
                            "schema.bootstrap.cached",
                            {
                                "cacheDir": str(cache_dir.resolve()),
                                "schemaCount": len(cached_methods),
                                "capabilityHash": digest,
                                "afterLockTimeout": True,
                            },
                        )
                        cache_mode = "hit_after_lock"
                        outcome = "cached"
                        return
                    self.logger.write(
                        "schema.bootstrap.lock_timeout",
                        {
                            "cacheDir": str(cache_dir.resolve()),
                            "fallback": "validate_task_plan will skip unknown-method check",
                        },
                    )
                    self._schema_bootstrap_degraded = True
                    outcome = "lock_timeout"
                    return

                cached_digest = read_cached_capability_hash(cache_dir)
                cached_methods = read_schema_methods_from_dirs([schemas_dir])
                if cached_digest == digest and cached_methods:
                    self.logger.write(
                        "schema.bootstrap.cached",
                        {
                            "cacheDir": str(cache_dir.resolve()),
                            "schemaCount": len(cached_methods),
                            "capabilityHash": digest,
                            "afterLockWait": True,
                        },
                    )
                    cache_mode = "hit_after_wait"
                    outcome = "cached"
                    return

                if tmp_schemas_dir.exists():
                    shutil.rmtree(tmp_schemas_dir, ignore_errors=True)
                cached_metadata = read_cached_capability_metadata(cache_dir)
                same_generation = (
                    cached_metadata.get("generation")
                    == SCHEMA_CONTRACT_GENERATION
                )
                rebuild_started = time.monotonic()
                bundle = await load_capability_bundle(
                    browser,
                    logger=self.logger,
                    blocked_methods=_BLOCKED_CAPABILITIES,
                    schemas_dir=tmp_schemas_dir,
                    schema_cache_dir=(schemas_dir if same_generation else None),
                    caps_response=caps_response,
                    prune_schema_cache=False,
                )
                timings["schemaLoadMs"] = int(
                    (time.monotonic() - rebuild_started) * 1000
                )
                cache_mode = "incremental" if same_generation else "full"
                if not bundle.method_schemas:
                    shutil.rmtree(tmp_schemas_dir, ignore_errors=True)
                    self.logger.write(
                        "schema.bootstrap.failed",
                        {
                            "reason": "empty_schema_bundle",
                            "fallback": "validate_task_plan will skip unknown-method check",
                        },
                    )
                    self._schema_bootstrap_degraded = True
                    outcome = "empty_schema_bundle"
                    return
                if schemas_dir.exists():
                    shutil.rmtree(schemas_dir, ignore_errors=True)
                tmp_schemas_dir.rename(schemas_dir)
                hash_path = write_cached_capability_hash(
                    cache_dir,
                    digest=digest,
                    capability_count=len(capabilities),
                    generation=SCHEMA_CONTRACT_GENERATION,
                    catalog_revision=revisions["catalogRevision"],
                    guide_revision=revisions["guideRevision"],
                )
                self.logger.write(
                    "schema.bootstrap.done",
                    {
                        "cacheDir": str(cache_dir.resolve()),
                        "schemasDir": str(schemas_dir.resolve()),
                        "hashPath": hash_path,
                        "schemaCount": len(bundle.method_schemas),
                        "capabilityHash": digest,
                        "cacheMode": cache_mode,
                        "catalogRevision": revisions["catalogRevision"] or None,
                        "guideRevision": revisions["guideRevision"] or None,
                        "agentGuidePath": guide_path,
                    },
                )
                outcome = "rebuilt"
    except Exception as exc:
        shutil.rmtree(tmp_schemas_dir, ignore_errors=True)
        self._schema_bootstrap_degraded = True
        if (getattr(exc, "transport_code", "") == ABCP_TRANSPORT_CONNECT_FAILED
                or getattr(exc, "connection_fatal", False)):
            self._browser_connection_failure = {
                "reasonCode": getattr(exc, "transport_code", ABCP_TRANSPORT_CONNECT_FAILED),
                "connection": getattr(exc, "connection_details", None)
                    or getattr(browser, "connection_details", {}),
                "message": str(exc),
                "businessActionsReplayed": 0,
            }
        self.logger.write(
            "schema.bootstrap.failed",
            {
                "error": str(exc),
                "errorKind": (
                    "transport_connect_failed"
                    if getattr(exc, "transport_code", "")
                    == ABCP_TRANSPORT_CONNECT_FAILED
                    else "bootstrap_error"
                ),
                "connection": getattr(exc, "connection_details", {}),
                "transportCode": getattr(exc, "transport_code", None),
                "rpcCode": getattr(exc, "rpc_code", None),
                "requestId": getattr(exc, "request_id", "") or None,
                "requestSent": getattr(exc, "request_sent", None),
                "fallback": (
                    "stop before Lead model calls; preserve task for resume"
                    if self._browser_connection_failure else
                    "validate_task_plan will skip unknown-method check"
                ),
            },
        )
        outcome = "exception"
    finally:
        self.logger.write(
            "schema.bootstrap.timing",
            {
                **timings,
                "elapsedMs": int(
                    (time.monotonic() - bootstrap_started) * 1000
                ),
                "outcome": outcome,
                "cacheMode": cache_mode,
            },
        )
        # The workflow contract was bound to this run's cache directory
        # before the write. If the write did not happen, contract reads
        # fall back to the checked-in copy rather than failing every schema
        # build — say so, because a silently older contract is the kind of
        # thing that is only ever noticed from the outside.
        source = contract_source()
        if source.get("fellBack"):
            self.logger.write("schema.contract.fallback", source)
