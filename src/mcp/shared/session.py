import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Callable, Generic, TypeVar

import anyio
import anyio.lowlevel
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import BaseModel
from typing_extensions import Self

from mcp.shared.exceptions import McpError
from mcp.types import (
    CancelledNotification,
    ClientNotification,
    ClientRequest,
    ClientResult,
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    MessageFrame,
    RequestParams,
    ServerNotification,
    ServerRequest,
    ServerResult,
)

ReadStream = MemoryObjectReceiveStream[MessageFrame | Exception]
ReadStreamWriter = MemoryObjectSendStream[MessageFrame | Exception]
WriteStream = MemoryObjectSendStream[MessageFrame]
WriteStreamReader = MemoryObjectReceiveStream[MessageFrame]

SendRequestT = TypeVar("SendRequestT", ClientRequest, ServerRequest)
SendResultT = TypeVar("SendResultT", ClientResult, ServerResult)
SendNotificationT = TypeVar("SendNotificationT", ClientNotification, ServerNotification)
ReceiveRequestT = TypeVar("ReceiveRequestT", ClientRequest, ServerRequest)
ReceiveResultT = TypeVar("ReceiveResultT", bound=BaseModel)
ReceiveNotificationT = TypeVar(
    "ReceiveNotificationT", ClientNotification, ServerNotification
)

RequestId = str | int


class RequestResponder(Generic[ReceiveRequestT, SendResultT]):
    """Handles responding to MCP requests and manages request lifecycle.

    This class MUST be used as a context manager to ensure proper cleanup and
    cancellation handling:

    Example:
        with request_responder as resp:
            await resp.respond(result)

    The context manager ensures:
    1. Proper cancellation scope setup and cleanup
    2. Request completion tracking
    3. Cleanup of in-flight requests
    """

    def __init__(
        self,
        request_id: RequestId,
        request_meta: RequestParams.Meta | None,
        request: ReceiveRequestT,
        session: """BaseSession[
            SendRequestT,
            SendNotificationT,
            SendResultT,
            ReceiveRequestT,
            ReceiveNotificationT
        ]""",
        on_complete: Callable[["RequestResponder[ReceiveRequestT, SendResultT]"], Any],
    ) -> None:
        self.request_id = request_id
        self.request_meta = request_meta
        self.request = request
        self._session = session
        self._completed = False
        self._cancel_scope = anyio.CancelScope()
        self._on_complete = on_complete
        self._entered = False  # Track if we're in a context manager

    def __enter__(self) -> "RequestResponder[ReceiveRequestT, SendResultT]":
        """Enter the context manager, enabling request cancellation tracking."""
        self._entered = True
        self._cancel_scope = anyio.CancelScope()
        self._cancel_scope.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the context manager, performing cleanup and notifying completion."""
        try:
            if self._completed:
                self._on_complete(self)
        finally:
            self._entered = False
            if not self._cancel_scope:
                raise RuntimeError("No active cancel scope")
            self._cancel_scope.__exit__(exc_type, exc_val, exc_tb)

    async def respond(self, response: SendResultT | ErrorData) -> None:
        """Send a response for this request.

        Must be called within a context manager block.
        Raises:
            RuntimeError: If not used within a context manager
            AssertionError: If request was already responded to
        """
        if not self._entered:
            raise RuntimeError("RequestResponder must be used as a context manager")
        assert not self._completed, "Request already responded to"

        if not self.cancelled:
            self._completed = True

            await self._session._send_response(
                request_id=self.request_id, response=response
            )

    async def cancel(self) -> None:
        """Cancel this request and mark it as completed."""
        if not self._entered:
            raise RuntimeError("RequestResponder must be used as a context manager")
        if not self._cancel_scope:
            raise RuntimeError("No active cancel scope")

        self._cancel_scope.cancel()
        self._completed = True  # Mark as completed so it's removed from in_flight
        # Send an error response to indicate cancellation
        await self._session._send_response(
            request_id=self.request_id,
            response=ErrorData(code=0, message="Request cancelled", data=None),
        )

    @property
    def in_flight(self) -> bool:
        return not self._completed and not self.cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancel_scope is not None and self._cancel_scope.cancel_called


class BaseSession(
    Generic[
        SendRequestT,
        SendNotificationT,
        SendResultT,
        ReceiveRequestT,
        ReceiveNotificationT,
    ],
):
    """
    Implements an MCP "session" on top of read/write streams, including features
    like request/response linking, notifications, and progress.

    This class is an async context manager that automatically starts processing
    messages when entered.
    """

    _response_streams: dict[
        RequestId, MemoryObjectSendStream[JSONRPCResponse | JSONRPCError]
    ]
    _request_id: int
    _in_flight: dict[RequestId, RequestResponder[ReceiveRequestT, SendResultT]]

    def __init__(
        self,
        read_stream: ReadStream,
        write_stream: WriteStream,
        receive_request_type: type[ReceiveRequestT],
        receive_notification_type: type[ReceiveNotificationT],
        # If none, reading will never time out
        read_timeout_seconds: timedelta | None = None,
    ) -> None:
        self._read_stream = read_stream
        self._write_stream = write_stream
        self._response_streams = {}
        self._request_id = 0
        self._receive_request_type = receive_request_type
        self._receive_notification_type = receive_notification_type
        self._read_timeout_seconds = read_timeout_seconds
        self._in_flight = {}

        self._exit_stack = AsyncExitStack()
        self._incoming_message_stream_writer, self._incoming_message_stream_reader = (
            anyio.create_memory_object_stream[
                RequestResponder[ReceiveRequestT, SendResultT]
                | ReceiveNotificationT
                | Exception
            ]()
        )
        self._exit_stack.push_async_callback(
            lambda: self._incoming_message_stream_reader.aclose()
        )
        self._exit_stack.push_async_callback(
            lambda: self._incoming_message_stream_writer.aclose()
        )

    async def __aenter__(self) -> Self:
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._receive_loop)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._exit_stack.aclose()
        # Using BaseSession as a context manager should not block on exit (this
        # would be very surprising behavior), so make sure to cancel the tasks
        # in the task group.
        self._task_group.cancel_scope.cancel()
        return await self._task_group.__aexit__(exc_type, exc_val, exc_tb)

    async def send_request(
        self,
        request: SendRequestT,
        result_type: type[ReceiveResultT],
    ) -> ReceiveResultT:
        """
        Sends a request and wait for a response. Raises an McpError if the
        response contains an error.

        Do not use this method to emit notifications! Use send_notification()
        instead.
        """

        request_id = self._request_id
        self._request_id = request_id + 1

        response_stream, response_stream_reader = anyio.create_memory_object_stream[
            JSONRPCResponse | JSONRPCError
        ](1)
        self._response_streams[request_id] = response_stream

        self._exit_stack.push_async_callback(lambda: response_stream.aclose())
        self._exit_stack.push_async_callback(lambda: response_stream_reader.aclose())

        jsonrpc_request = JSONRPCRequest(
            jsonrpc="2.0",
            id=request_id,
            **request.model_dump(by_alias=True, mode="json", exclude_none=True),
        )

        # TODO: Support progress callbacks

        await self._write_stream.send(
            MessageFrame(message=JSONRPCMessage(jsonrpc_request), raw=None)
        )

        try:
            with anyio.fail_after(
                None
                if self._read_timeout_seconds is None
                else self._read_timeout_seconds.total_seconds()
            ):
                response_or_error = await response_stream_reader.receive()
        except TimeoutError:
            raise McpError(
                ErrorData(
                    code=httpx.codes.REQUEST_TIMEOUT,
                    message=(
                        f"Timed out while waiting for response to "
                        f"{request.__class__.__name__}. Waited "
                        f"{self._read_timeout_seconds} seconds."
                    ),
                )
            )

        if isinstance(response_or_error, JSONRPCError):
            raise McpError(response_or_error.error)
        else:
            return result_type.model_validate(response_or_error.result)

    async def send_notification(self, notification: SendNotificationT) -> None:
        """
        Emits a notification, which is a one-way message that does not expect
        a response.
        """
        jsonrpc_notification = JSONRPCNotification(
            jsonrpc="2.0",
            **notification.model_dump(by_alias=True, mode="json", exclude_none=True),
        )

        await self._write_stream.send(
            MessageFrame(message=JSONRPCMessage(jsonrpc_notification), raw=None)
        )

    async def _send_response(
        self, request_id: RequestId, response: SendResultT | ErrorData
    ) -> None:
        if isinstance(response, ErrorData):
            jsonrpc_error = JSONRPCError(jsonrpc="2.0", id=request_id, error=response)
            await self._write_stream.send(
                MessageFrame(message=JSONRPCMessage(jsonrpc_error), raw=None)
            )
        else:
            jsonrpc_response = JSONRPCResponse(
                jsonrpc="2.0",
                id=request_id,
                result=response.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                ),
            )
            await self._write_stream.send(
                MessageFrame(message=JSONRPCMessage(jsonrpc_response), raw=None)
            )

    async def _receive_loop(self) -> None:
        async with (
            self._read_stream,
            self._write_stream,
            self._incoming_message_stream_writer,
        ):
            async for raw_message in self._read_stream:
                if isinstance(raw_message, Exception):
                    await self._incoming_message_stream_writer.send(raw_message)
                    continue

                message = raw_message.message
                if isinstance(message.root, JSONRPCRequest):
                    validated_request = self._receive_request_type.model_validate(
                        message.root.model_dump(
                            by_alias=True, mode="json", exclude_none=True
                        )
                    )

                    responder = RequestResponder(
                        request_id=message.root.id,
                        request_meta=validated_request.root.params.meta
                        if validated_request.root.params
                        else None,
                        request=validated_request,
                        session=self,
                        on_complete=lambda r: self._in_flight.pop(r.request_id, None),
                    )

                    self._in_flight[responder.request_id] = responder
                    await self._received_request(responder)
                    if not responder._completed:
                        await self._incoming_message_stream_writer.send(responder)

                elif isinstance(message.root, JSONRPCNotification):
                    try:
                        notification = self._receive_notification_type.model_validate(
                            message.root.model_dump(
                                by_alias=True, mode="json", exclude_none=True
                            )
                        )
                        # Handle cancellation notifications
                        if isinstance(notification.root, CancelledNotification):
                            cancelled_id = notification.root.params.requestId
                            if cancelled_id in self._in_flight:
                                await self._in_flight[cancelled_id].cancel()
                        else:
                            await self._received_notification(notification)
                            await self._incoming_message_stream_writer.send(
                                notification
                            )
                    except Exception as e:
                        # For other validation errors, log and continue
                        logging.warning(
                            f"Failed to validate notification: {e}. "
                            f"Message was: {message.root}"
                        )
                else:  # Response or error
                    stream = self._response_streams.pop(message.root.id, None)
                    if stream:
                        await stream.send(message.root)
                    else:
                        await self._incoming_message_stream_writer.send(
                            RuntimeError(
                                "Received response with an unknown "
                                f"request ID: {message}"
                            )
                        )

    async def _received_request(
        self, responder: RequestResponder[ReceiveRequestT, SendResultT]
    ) -> None:
        """
        Can be overridden by subclasses to handle a request without needing to
        listen on the message stream.

        If the request is responded to within this method, it will not be
        forwarded on to the message stream.
        """

    async def _received_notification(self, notification: ReceiveNotificationT) -> None:
        """
        Can be overridden by subclasses to handle a notification without needing
        to listen on the message stream.
        """

    async def send_progress_notification(
        self, progress_token: str | int, progress: float, total: float | None = None
    ) -> None:
        """
        Sends a progress notification for a request that is currently being
        processed.
        """

    @property
    def incoming_messages(
        self,
    ) -> MemoryObjectReceiveStream[
        RequestResponder[ReceiveRequestT, SendResultT]
        | ReceiveNotificationT
        | Exception
    ]:
        return self._incoming_message_stream_reader
