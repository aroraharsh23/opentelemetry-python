# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase
from unittest.mock import Mock, PropertyMock, patch

from google.protobuf.duration_pb2 import Duration
from google.rpc.error_details_pb2 import RetryInfo
from grpc import ChannelCredentials, StatusCode, server

from opentelemetry.exporter.otlp.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceServicer,
    add_TraceServiceServicer_to_server,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    InstrumentationLibrary,
    KeyValue,
)
from opentelemetry.proto.resource.v1.resource_pb2 import (
    Resource as OTLPResource,
)
from opentelemetry.proto.trace.v1.trace_pb2 import (
    InstrumentationLibrarySpans,
    ResourceSpans,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OTLPSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status
from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_SPAN_CERTIFICATE,
    OTEL_EXPORTER_OTLP_SPAN_ENDPOINT,
    OTEL_EXPORTER_OTLP_SPAN_HEADERS,
    OTEL_EXPORTER_OTLP_SPAN_TIMEOUT,
)
from opentelemetry.sdk.resources import Resource as SDKResource
from opentelemetry.sdk.trace import Status as SDKStatus
from opentelemetry.sdk.trace import StatusCode as SDKStatusCode
from opentelemetry.sdk.trace import TracerProvider, _Span
from opentelemetry.sdk.trace.export import (
    SimpleExportSpanProcessor,
    SpanExportResult,
)
from opentelemetry.sdk.util.instrumentation import InstrumentationInfo

THIS_DIR = os.path.dirname(__file__)


class TraceServiceServicerUNAVAILABLEDelay(TraceServiceServicer):
    # pylint: disable=invalid-name,unused-argument,no-self-use
    def Export(self, request, context):
        context.set_code(StatusCode.UNAVAILABLE)

        context.send_initial_metadata(
            (("google.rpc.retryinfo-bin", RetryInfo().SerializeToString()),)
        )
        context.set_trailing_metadata(
            (
                (
                    "google.rpc.retryinfo-bin",
                    RetryInfo(
                        retry_delay=Duration(seconds=4)
                    ).SerializeToString(),
                ),
            )
        )

        return ExportTraceServiceResponse()


class TraceServiceServicerUNAVAILABLE(TraceServiceServicer):
    # pylint: disable=invalid-name,unused-argument,no-self-use
    def Export(self, request, context):
        context.set_code(StatusCode.UNAVAILABLE)

        return ExportTraceServiceResponse()


class TraceServiceServicerSUCCESS(TraceServiceServicer):
    # pylint: disable=invalid-name,unused-argument,no-self-use
    def Export(self, request, context):
        context.set_code(StatusCode.OK)

        return ExportTraceServiceResponse()


class TraceServiceServicerALREADY_EXISTS(TraceServiceServicer):
    # pylint: disable=invalid-name,unused-argument,no-self-use
    def Export(self, request, context):
        context.set_code(StatusCode.ALREADY_EXISTS)

        return ExportTraceServiceResponse()


class TestOTLPSpanExporter(TestCase):
    def setUp(self):
        tracer_provider = TracerProvider()
        self.exporter = OTLPSpanExporter(insecure=True)
        tracer_provider.add_span_processor(
            SimpleExportSpanProcessor(self.exporter)
        )
        self.tracer = tracer_provider.get_tracer(__name__)

        self.server = server(ThreadPoolExecutor(max_workers=10))

        self.server.add_insecure_port("[::]:4317")

        self.server.start()

        event_mock = Mock(
            **{
                "timestamp": 1591240820506462784,
                "attributes": OrderedDict([("a", 1), ("b", False)]),
            }
        )

        type(event_mock).name = PropertyMock(return_value="a")

        self.span = _Span(
            "a",
            context=Mock(
                **{
                    "trace_state": OrderedDict([("a", "b"), ("c", "d")]),
                    "span_id": 10217189687419569865,
                    "trace_id": 67545097771067222548457157018666467027,
                }
            ),
            resource=SDKResource(OrderedDict([("a", 1), ("b", False)])),
            parent=Mock(**{"span_id": 12345}),
            attributes=OrderedDict([("a", 1), ("b", True)]),
            events=[event_mock],
            links=[
                Mock(
                    **{
                        "context.trace_id": 1,
                        "context.span_id": 2,
                        "attributes": OrderedDict([("a", 1), ("b", False)]),
                        "kind": OTLPSpan.SpanKind.SPAN_KIND_INTERNAL,  # pylint: disable=no-member
                    }
                )
            ],
            instrumentation_info=InstrumentationInfo(
                name="name", version="version"
            ),
        )

        self.span.start()
        self.span.end()

    def tearDown(self):
        self.server.stop(None)

    @patch.dict(
        "os.environ",
        {
            OTEL_EXPORTER_OTLP_SPAN_ENDPOINT: "collector:4317",
            OTEL_EXPORTER_OTLP_SPAN_CERTIFICATE: THIS_DIR
            + "/fixtures/test.cert",
            OTEL_EXPORTER_OTLP_SPAN_HEADERS: "key1=value1,key2=value2",
            OTEL_EXPORTER_OTLP_SPAN_TIMEOUT: "10",
        },
    )
    @patch("opentelemetry.exporter.otlp.exporter.OTLPExporterMixin.__init__")
    def test_env_variables(self, mock_exporter_mixin):
        OTLPSpanExporter()

        self.assertTrue(len(mock_exporter_mixin.call_args_list) == 1)
        _, kwargs = mock_exporter_mixin.call_args_list[0]

        self.assertEqual(kwargs["endpoint"], "collector:4317")
        self.assertEqual(kwargs["headers"], "key1=value1,key2=value2")
        self.assertEqual(kwargs["timeout"], 10)
        self.assertIsNotNone(kwargs["credentials"])
        self.assertIsInstance(kwargs["credentials"], ChannelCredentials)

    @patch("opentelemetry.exporter.otlp.exporter.ssl_channel_credentials")
    @patch("opentelemetry.exporter.otlp.exporter.secure_channel")
    @patch("opentelemetry.exporter.otlp.trace_exporter.OTLPSpanExporter._stub")
    # pylint: disable=unused-argument
    def test_no_credentials_error(
        self, mock_ssl_channel, mock_secure, mock_stub
    ):
        OTLPSpanExporter(insecure=False)
        self.assertTrue(mock_ssl_channel.called)

    @patch.dict(
        "os.environ",
        {OTEL_EXPORTER_OTLP_SPAN_HEADERS: "key1=value1,key2=value2"},
    )
    @patch("opentelemetry.exporter.otlp.exporter.ssl_channel_credentials")
    @patch("opentelemetry.exporter.otlp.exporter.secure_channel")
    # pylint: disable=unused-argument
    def test_otlp_headers_from_env(self, mock_ssl_channel, mock_secure):
        exporter = OTLPSpanExporter()
        # pylint: disable=protected-access
        self.assertEqual(
            exporter._headers, (("key1", "value1"), ("key2", "value2"))
        )
        exporter = OTLPSpanExporter(
            headers=(("key3", "value3"), ("key4", "value4"))
        )
        # pylint: disable=protected-access
        self.assertEqual(
            exporter._headers, (("key3", "value3"), ("key4", "value4"))
        )

    @patch("opentelemetry.exporter.otlp.exporter.ssl_channel_credentials")
    @patch("opentelemetry.exporter.otlp.exporter.secure_channel")
    # pylint: disable=unused-argument
    def test_otlp_headers(self, mock_ssl_channel, mock_secure):
        exporter = OTLPSpanExporter()
        # pylint: disable=protected-access
        self.assertIsNone(exporter._headers, None)

    @patch("opentelemetry.exporter.otlp.exporter.expo")
    @patch("opentelemetry.exporter.otlp.exporter.sleep")
    def test_unavailable(self, mock_sleep, mock_expo):

        mock_expo.configure_mock(**{"return_value": [1]})

        add_TraceServiceServicer_to_server(
            TraceServiceServicerUNAVAILABLE(), self.server
        )
        self.assertEqual(
            self.exporter.export([self.span]), SpanExportResult.FAILURE
        )
        mock_sleep.assert_called_with(1)

    @patch("opentelemetry.exporter.otlp.exporter.expo")
    @patch("opentelemetry.exporter.otlp.exporter.sleep")
    def test_unavailable_delay(self, mock_sleep, mock_expo):

        mock_expo.configure_mock(**{"return_value": [1]})

        add_TraceServiceServicer_to_server(
            TraceServiceServicerUNAVAILABLEDelay(), self.server
        )
        self.assertEqual(
            self.exporter.export([self.span]), SpanExportResult.FAILURE
        )
        mock_sleep.assert_called_with(4)

    def test_success(self):
        add_TraceServiceServicer_to_server(
            TraceServiceServicerSUCCESS(), self.server
        )
        self.assertEqual(
            self.exporter.export([self.span]), SpanExportResult.SUCCESS
        )

    def test_failure(self):
        add_TraceServiceServicer_to_server(
            TraceServiceServicerALREADY_EXISTS(), self.server
        )
        self.assertEqual(
            self.exporter.export([self.span]), SpanExportResult.FAILURE
        )

    def test_translate_spans(self):

        expected = ExportTraceServiceRequest(
            resource_spans=[
                ResourceSpans(
                    resource=OTLPResource(
                        attributes=[
                            KeyValue(key="a", value=AnyValue(int_value=1)),
                            KeyValue(
                                key="b", value=AnyValue(bool_value=False)
                            ),
                        ]
                    ),
                    instrumentation_library_spans=[
                        InstrumentationLibrarySpans(
                            instrumentation_library=InstrumentationLibrary(
                                name="name", version="version"
                            ),
                            spans=[
                                OTLPSpan(
                                    # pylint: disable=no-member
                                    name="a",
                                    start_time_unix_nano=self.span.start_time,
                                    end_time_unix_nano=self.span.end_time,
                                    trace_state="a=b,c=d",
                                    span_id=int.to_bytes(
                                        10217189687419569865, 8, "big"
                                    ),
                                    trace_id=int.to_bytes(
                                        67545097771067222548457157018666467027,
                                        16,
                                        "big",
                                    ),
                                    parent_span_id=(
                                        b"\000\000\000\000\000\00009"
                                    ),
                                    kind=(
                                        OTLPSpan.SpanKind.SPAN_KIND_INTERNAL
                                    ),
                                    attributes=[
                                        KeyValue(
                                            key="a",
                                            value=AnyValue(int_value=1),
                                        ),
                                        KeyValue(
                                            key="b",
                                            value=AnyValue(bool_value=True),
                                        ),
                                    ],
                                    events=[
                                        OTLPSpan.Event(
                                            name="a",
                                            time_unix_nano=1591240820506462784,
                                            attributes=[
                                                KeyValue(
                                                    key="a",
                                                    value=AnyValue(
                                                        int_value=1
                                                    ),
                                                ),
                                                KeyValue(
                                                    key="b",
                                                    value=AnyValue(
                                                        bool_value=False
                                                    ),
                                                ),
                                            ],
                                        )
                                    ],
                                    status=Status(code=0, message=""),
                                    links=[
                                        OTLPSpan.Link(
                                            trace_id=int.to_bytes(
                                                1, 16, "big"
                                            ),
                                            span_id=int.to_bytes(2, 8, "big"),
                                            attributes=[
                                                KeyValue(
                                                    key="a",
                                                    value=AnyValue(
                                                        int_value=1
                                                    ),
                                                ),
                                                KeyValue(
                                                    key="b",
                                                    value=AnyValue(
                                                        bool_value=False
                                                    ),
                                                ),
                                            ],
                                        )
                                    ],
                                )
                            ],
                        )
                    ],
                ),
            ]
        )

        # pylint: disable=protected-access
        self.assertEqual(expected, self.exporter._translate_data([self.span]))

    def _check_translated_status(
        self,
        translated: ExportTraceServiceRequest,
        code_expected: Status,
        deprecated_code_expected: Status,
    ):
        status = (
            translated.resource_spans[0]
            .instrumentation_library_spans[0]
            .spans[0]
            .status
        )

        self.assertEqual(
            status.code, code_expected,
        )
        self.assertEqual(
            status.deprecated_code, deprecated_code_expected,
        )

    def test_span_status_translate(self):
        # pylint: disable=protected-access,no-member
        unset = SDKStatus(status_code=SDKStatusCode.UNSET)
        ok = SDKStatus(status_code=SDKStatusCode.OK)
        error = SDKStatus(status_code=SDKStatusCode.ERROR)
        unset_translated = self.exporter._translate_data(
            [_create_span_with_status(unset)]
        )
        ok_translated = self.exporter._translate_data(
            [_create_span_with_status(ok)]
        )
        error_translated = self.exporter._translate_data(
            [_create_span_with_status(error)]
        )
        self._check_translated_status(
            unset_translated,
            Status.STATUS_CODE_UNSET,
            Status.DEPRECATED_STATUS_CODE_OK,
        )
        self._check_translated_status(
            ok_translated,
            Status.STATUS_CODE_OK,
            Status.DEPRECATED_STATUS_CODE_OK,
        )
        self._check_translated_status(
            error_translated,
            Status.STATUS_CODE_ERROR,
            Status.DEPRECATED_STATUS_CODE_UNKNOWN_ERROR,
        )


def _create_span_with_status(status: SDKStatus):
    span = _Span(
        "a",
        context=Mock(
            **{
                "trace_state": OrderedDict([("a", "b"), ("c", "d")]),
                "span_id": 10217189687419569865,
                "trace_id": 67545097771067222548457157018666467027,
            }
        ),
        parent=Mock(**{"span_id": 12345}),
        instrumentation_info=InstrumentationInfo(
            name="name", version="version"
        ),
    )
    span.set_status(status)
    return span
