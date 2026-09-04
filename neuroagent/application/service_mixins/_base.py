"""Shared dependencies and idempotency helpers for every use-case mixin."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel

from neuroagent.agent.providers import ModelProvider
from neuroagent.agent.secrets import SecretResolver
from neuroagent.application.contracts import ProjectView
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import content_hash
from neuroagent.application.ports import (
    DatabaseLifecyclePort,
    DatasetInspectorPort,
    DemographicsReaderPort,
    PathPolicyPort,
    RepositoryPort,
    SecretWriterPort,
)
from neuroagent.application.settings import Settings
from neuroagent.skills.compiler import SkillCompiler
from neuroagent.skills.registry import SkillRegistry
from neuroagent.skills.resolver import SkillResolver
from neuroagent.skills.validation import SkillValidator
from neuroagent.tools.registry import ToolRegistry

T = TypeVar("T", bound=BaseModel)
U = TypeVar("U")


class BaseServiceMixin:
    """Declares the shared dependencies every use-case mixin reads through ``self``."""

    settings: Settings
    database: DatabaseLifecyclePort
    repository: RepositoryPort
    secret_resolver: SecretResolver
    secret_writer: SecretWriterPort
    providers: Mapping[str, ModelProvider]
    path_policy: PathPolicyPort
    dataset_inspector: DatasetInspectorPort
    demographics_reader: DemographicsReaderPort
    environment_provider: EnvironmentLockProvider
    skill_registry: SkillRegistry
    skill_validator: SkillValidator
    skill_resolver: SkillResolver
    tool_registry: ToolRegistry
    skill_compiler: SkillCompiler

    def _idempotent(
        self,
        *,
        scope: str,
        key: str,
        request: BaseModel,
        response_type: type[T],
        action: Callable[[], T],
    ) -> T:
        request_hash = content_hash(request.model_dump(mode="json"))
        owner_token = str(uuid4())
        with self.repository.atomic():
            stored = self.repository.begin_idempotent_request(
                scope,
                key,
                request_hash,
                owner_token,
                self.settings.idempotency_lease_seconds,
            )
            if stored is not None:
                return response_type.model_validate(stored)
            response = action()
            self.repository.complete_idempotent_request(
                scope, key, request_hash, owner_token, response
            )
        return response

    def _idempotent_prepared(
        self,
        *,
        scope: str,
        key: str,
        request: BaseModel,
        response_type: type[T],
        prepare: Callable[[], U],
        finalize: Callable[[U], T],
    ) -> T:
        """Run slow/read-only preparation outside SQLite's write transaction."""

        request_hash = content_hash(request.model_dump(mode="json"))
        owner_token = str(uuid4())
        stored = self.repository.begin_idempotent_request(
            scope,
            key,
            request_hash,
            owner_token,
            self.settings.idempotency_lease_seconds,
        )
        if stored is not None:
            return response_type.model_validate(stored)
        try:
            prepared = prepare()
            with self.repository.atomic():
                response = finalize(prepared)
                self.repository.complete_idempotent_request(
                    scope, key, request_hash, owner_token, response
                )
            return response
        except BaseException:
            # The durable reservation is a short transaction. A handled
            # preparation/finalization failure releases it so the exact same
            # request can safely retry; business writes and response storage
            # are committed (or rolled back) together above.
            self.repository.release_idempotent_request(scope, key, request_hash, owner_token)
            raise

    async def _idempotent_async(
        self,
        *,
        scope: str,
        key: str,
        request: BaseModel,
        response_type: type[T],
        prepare: Callable[[], Awaitable[U]],
        finalize: Callable[[U], T],
    ) -> T:
        request_hash = content_hash(request.model_dump(mode="json"))
        owner_token = str(uuid4())
        stored = self.repository.begin_idempotent_request(
            scope,
            key,
            request_hash,
            owner_token,
            self.settings.idempotency_lease_seconds,
        )
        if stored is not None:
            return response_type.model_validate(stored)
        operation_task = asyncio.create_task(
            self._complete_idempotent_async_operation(
                scope=scope,
                key=key,
                request_hash=request_hash,
                owner_token=owner_token,
                prepare=prepare,
                finalize=finalize,
            )
        )
        try:
            return await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            # A disconnected HTTP caller must not cancel an already-dispatched
            # billable Provider request and then make the same key retryable.
            # Keep the supervised operation alive until its response or error
            # has been durably resolved, then preserve caller cancellation.
            while not operation_task.done():
                try:
                    await asyncio.shield(operation_task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if operation_task.done() and not operation_task.cancelled():
                operation_task.exception()
            raise

    async def _complete_idempotent_async_operation(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        prepare: Callable[[], Awaitable[U]],
        finalize: Callable[[U], T],
    ) -> T:
        stop_heartbeat = asyncio.Event()
        prepare_task = asyncio.ensure_future(prepare())
        heartbeat_task = asyncio.create_task(
            self._renew_idempotency_lease(
                scope=scope,
                key=key,
                request_hash=request_hash,
                owner_token=owner_token,
                stop=stop_heartbeat,
            )
        )
        try:
            done, _pending = await asyncio.wait(
                (prepare_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise ConflictError(
                    "idempotency_lease_lost",
                    "幂等请求处理所有权已丢失, 已停止外部调用。",
                    scope=scope,
                )
            prepared = prepare_task.result()
            stop_heartbeat.set()
            await heartbeat_task
            with self.repository.atomic():
                if not self.repository.renew_idempotent_request(
                    scope,
                    key,
                    request_hash,
                    owner_token,
                    self.settings.idempotency_lease_seconds,
                ):
                    raise ConflictError(
                        "idempotency_lease_lost",
                        "幂等请求处理所有权已丢失, 未保存外部调用结果。",
                        scope=scope,
                    )
                response = finalize(prepared)
                self.repository.complete_idempotent_request(
                    scope, key, request_hash, owner_token, response
                )
            return response
        except asyncio.CancelledError:
            stop_heartbeat.set()
            for task in (prepare_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prepare_task, heartbeat_task, return_exceptions=True)
            # Cancellation here means service shutdown or explicit lease-loss
            # handling, not an HTTP disconnect (the outer task is shielded).
            # Preserve the reservation because the remote acceptance state may
            # be indeterminate.
            raise
        except BaseException:
            stop_heartbeat.set()
            for task in (prepare_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prepare_task, heartbeat_task, return_exceptions=True)
            # The reservation is deliberately committed before awaiting a
            # provider so SQLite writers are never blocked by network latency.
            # A handled failure releases it, allowing an explicit retry with
            # the same request and key.
            self.repository.release_idempotent_request(scope, key, request_hash, owner_token)
            raise

    async def _renew_idempotency_lease(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        stop: asyncio.Event,
    ) -> None:
        lease_seconds = self.settings.idempotency_lease_seconds
        interval_seconds = min(max(lease_seconds / 3, 0.1), 30.0)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                renewed = await asyncio.to_thread(
                    self.repository.renew_idempotent_request,
                    scope,
                    key,
                    request_hash,
                    owner_token,
                    lease_seconds,
                )
                if not renewed:
                    raise ConflictError(
                        "idempotency_lease_lost",
                        "幂等请求处理所有权已丢失, 已停止外部调用。",
                        scope=scope,
                    ) from None

    def _require_project_version(self, project_id: str, expected_version: int) -> ProjectView:
        project = self.repository.get_project(project_id)
        if project.version != expected_version:
            raise ConflictError(
                "revision_conflict",
                "项目版本已变化, 请刷新后重试。",
                expected=expected_version,
                actual=project.version,
            )
        return project
