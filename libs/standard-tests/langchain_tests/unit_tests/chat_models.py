""":autodoc-options: autoproperty."""

import inspect
import os
from abc import abstractmethod
from typing import Any, Literal, Optional
from unittest import mock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.load import dumpd, load
from langchain_core.runnables import RunnableBinding
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, SecretStr
from pydantic.v1 import (
    BaseModel as BaseModelV1,
)
from pydantic.v1 import (
    Field as FieldV1,
)
from pydantic.v1 import (
    ValidationError as ValidationErrorV1,
)
from pytest_benchmark.fixture import BenchmarkFixture  # type: ignore[import-untyped]
from syrupy import SnapshotAssertion

from langchain_tests.base import BaseStandardTests
from langchain_tests.utils.pydantic import PYDANTIC_MAJOR_VERSION


def generate_schema_pydantic_v1_from_2() -> Any:
    """Use to generate a schema from v1 namespace in pydantic 2.

    :private:
    """
    if PYDANTIC_MAJOR_VERSION != 2:
        msg = "This function is only compatible with Pydantic v2."
        raise AssertionError(msg)

    class PersonB(BaseModelV1):
        """Record attributes of a person."""

        name: str = FieldV1(..., description="The name of the person.")
        age: int = FieldV1(..., description="The age of the person.")

    return PersonB


def generate_schema_pydantic() -> Any:
    """Works with either pydantic 1 or 2.

    :private:
    """

    class PersonA(BaseModel):
        """Record attributes of a person."""

        name: str = Field(..., description="The name of the person.")
        age: int = Field(..., description="The age of the person.")

    return PersonA


TEST_PYDANTIC_MODELS = [generate_schema_pydantic()]

if PYDANTIC_MAJOR_VERSION == 2:
    TEST_PYDANTIC_MODELS.append(generate_schema_pydantic_v1_from_2())


class ChatModelTests(BaseStandardTests):
    """Base class for chat model tests.

    :private:
    """

    @property
    @abstractmethod
    def chat_model_class(self) -> type[BaseChatModel]:
        """The chat model class to test, e.g., ``ChatParrotLink``."""
        ...

    @property
    def chat_model_params(self) -> dict:
        """Initialization parameters for the chat model."""
        return {}

    @property
    def standard_chat_model_params(self) -> dict:
        """:private:"""
        return {
            "temperature": 0,
            "max_tokens": 100,
            "timeout": 60,
            "stop": [],
            "max_retries": 2,
        }

    @pytest.fixture
    def model(self) -> BaseChatModel:
        """:private:"""
        return self.chat_model_class(
            **{
                **self.standard_chat_model_params,
                **self.chat_model_params,
            }
        )

    @pytest.fixture
    def my_adder_tool(self) -> BaseTool:
        """:private:"""

        @tool
        def my_adder_tool(a: int, b: int) -> int:
            """Takes two integers, a and b, and returns their sum."""
            return a + b

        return my_adder_tool

    @property
    def has_tool_calling(self) -> bool:
        """(bool) whether the model supports tool calling."""
        return self.chat_model_class.bind_tools is not BaseChatModel.bind_tools

    @property
    def tool_choice_value(self) -> Optional[str]:
        """(None or str) to use for tool choice when used in tests."""
        return None

    @property
    def has_tool_choice(self) -> bool:
        """(bool) whether the model supports tool calling."""
        bind_tools_params = inspect.signature(
            self.chat_model_class.bind_tools
        ).parameters
        return "tool_choice" in bind_tools_params

    @property
    def has_structured_output(self) -> bool:
        """(bool) whether the chat model supports structured output."""
        return (
            self.chat_model_class.with_structured_output
            is not BaseChatModel.with_structured_output
        ) or self.has_tool_calling

    @property
    def structured_output_kwargs(self) -> dict:
        """If specified, additional kwargs for with_structured_output."""
        return {}

    @property
    def supports_json_mode(self) -> bool:
        """(bool) whether the chat model supports JSON mode."""
        return False

    @property
    def supports_image_inputs(self) -> bool:
        """(bool) whether the chat model supports image inputs, defaults to
        ``False``.
        """
        return False

    @property
    def supports_image_urls(self) -> bool:
        """(bool) whether the chat model supports image inputs from URLs, defaults to
        ``False``.
        """
        return False

    @property
    def supports_pdf_inputs(self) -> bool:
        """(bool) whether the chat model supports PDF inputs, defaults to ``False``."""
        return False

    @property
    def supports_audio_inputs(self) -> bool:
        """(bool) whether the chat model supports audio inputs, defaults to
        ``False``.
        """
        return False

    @property
    def supports_video_inputs(self) -> bool:
        """(bool) whether the chat model supports video inputs, defaults to ``False``.
        No current tests are written for this feature.
        """
        return False

    @property
    def returns_usage_metadata(self) -> bool:
        """(bool) whether the chat model returns usage metadata on invoke and streaming
        responses.
        """
        return True

    @property
    def supports_anthropic_inputs(self) -> bool:
        """(bool) whether the chat model supports Anthropic-style inputs."""
        return False

    @property
    def supports_image_tool_message(self) -> bool:
        """(bool) whether the chat model supports ToolMessages that include image
        content.
        """
        return False

    @property
    def enable_vcr_tests(self) -> bool:
        """(bool) whether to enable VCR tests for the chat model.

        .. important::
            See ``enable_vcr_tests`` dropdown :class:`above <ChatModelTests>` for more
            information.
        """
        return False

    @property
    def supported_usage_metadata_details(
        self,
    ) -> dict[
        Literal["invoke", "stream"],
        list[
            Literal[
                "audio_input",
                "audio_output",
                "reasoning_output",
                "cache_read_input",
                "cache_creation_input",
            ]
        ],
    ]:
        """(dict) what usage metadata details are emitted in invoke and stream. Only
        needs to be overridden if these details are returned by the model.
        """
        return {"invoke": [], "stream": []}


class ChatModelUnitTests(ChatModelTests):
    """Base class for chat model unit tests.

    Test subclasses must implement the ``chat_model_class`` and
    ``chat_model_params`` properties to specify what model to test and its
    initialization parameters.

    Example:

    .. code-block:: python

        from typing import Type

        from langchain_tests.unit_tests import ChatModelUnitTests
        from my_package.chat_models import MyChatModel


        class TestMyChatModelUnit(ChatModelUnitTests):
            @property
            def chat_model_class(self) -> Type[MyChatModel]:
                # Return the chat model class to test here
                return MyChatModel

            @property
            def chat_model_params(self) -> dict:
                # Return initialization parameters for the model.
                return {"model": "model-001", "temperature": 0}

    .. note::
          API references for individual test methods include troubleshooting tips.


    Test subclasses must implement the following two properties:

    chat_model_class
        The chat model class to test, e.g., ``ChatParrotLink``.

        Example:

        .. code-block:: python

            @property
            def chat_model_class(self) -> Type[ChatParrotLink]:
                return ChatParrotLink

    chat_model_params
        Initialization parameters for the chat model.

        Example:

        .. code-block:: python

            @property
            def chat_model_params(self) -> dict:
                return {"model": "bird-brain-001", "temperature": 0}

    In addition, test subclasses can control what features are tested (such as tool
    calling or multi-modality) by selectively overriding the following properties.
    Expand to see details:

    .. dropdown:: has_tool_calling

        Boolean property indicating whether the chat model supports tool calling.

        By default, this is determined by whether the chat model's `bind_tools` method
        is overridden. It typically does not need to be overridden on the test class.

        Example override:

        .. code-block:: python

            @property
            def has_tool_calling(self) -> bool:
                return True

    .. dropdown:: tool_choice_value

        Value to use for tool choice when used in tests.

        .. warning:: Deprecated since version 0.3.15:
           This property will be removed in version 0.3.20. If a model does not
           support forcing tool calling, override the ``has_tool_choice`` property to
           return ``False``. Otherwise, models should accept values of ``'any'`` or
           the name of a tool in ``tool_choice``.

        Example:

        .. code-block:: python

            @property
            def tool_choice_value(self) -> Optional[str]:
                return "any"

    .. dropdown:: has_tool_choice

        Boolean property indicating whether the chat model supports forcing tool
        calling via a ``tool_choice`` parameter.

        By default, this is determined by whether the parameter is included in the
        signature for the corresponding ``bind_tools`` method.

        If ``True``, the minimum requirement for this feature is that
        ``tool_choice="any"`` will force a tool call, and ``tool_choice=<tool name>``
        will force a call to a specific tool.

        Example override:

        .. code-block:: python

            @property
            def has_tool_choice(self) -> bool:
                return False

    .. dropdown:: has_structured_output

        Boolean property indicating whether the chat model supports structured
        output.

        By default, this is determined by whether the chat model overrides the
        ``with_structured_output`` or ``bind_tools`` methods. If the base
        implementations are intended to be used, this method should be overridden.

        See: https://python.langchain.com/docs/concepts/structured_outputs/

        Example:

        .. code-block:: python

            @property
            def has_structured_output(self) -> bool:
                return True

    .. dropdown:: structured_output_kwargs

        Dict property that can be used to specify additional kwargs for
        ``with_structured_output``. Useful for testing different models.

        Example:

        .. code-block:: python

            @property
            def structured_output_kwargs(self) -> dict:
                return {"method": "function_calling"}

    .. dropdown:: supports_json_mode

        Boolean property indicating whether the chat model supports JSON mode in
        ``with_structured_output``.

        See: https://python.langchain.com/docs/concepts/structured_outputs/#json-mode

        Example:

        .. code-block:: python

            @property
            def supports_json_mode(self) -> bool:
                return True

    .. dropdown:: supports_image_inputs

        Boolean property indicating whether the chat model supports image inputs.
        Defaults to ``False``.

        If set to ``True``, the chat model will be tested using content blocks of the
        form

        .. code-block:: python

            {
                "type": "image",
                "source_type": "base64",
                "data": "<base64 image data>",
                "mime_type": "image/jpeg",  # or appropriate mime-type
            }

        In addition to OpenAI-style content blocks:

        .. code-block:: python

            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
            }

        See https://python.langchain.com/docs/concepts/multimodality/

        Example:

        .. code-block:: python

            @property
            def supports_image_inputs(self) -> bool:
                return True

    .. dropdown:: supports_image_urls

        Boolean property indicating whether the chat model supports image inputs from
        URLs. Defaults to ``False``.

        If set to ``True``, the chat model will be tested using content blocks of the
        form

        .. code-block:: python

            {
                "type": "image",
                "source_type": "url",
                "url": "https://...",
            }

        See https://python.langchain.com/docs/concepts/multimodality/

        Example:

        .. code-block:: python

            @property
            def supports_image_urls(self) -> bool:
                return True

    .. dropdown:: supports_pdf_inputs

        Boolean property indicating whether the chat model supports PDF inputs.
        Defaults to ``False``.

        If set to ``True``, the chat model will be tested using content blocks of the
        form

        .. code-block:: python

            {
                "type": "file",
                "source_type": "base64",
                "data": "<base64 file data>",
                "mime_type": "application/pdf",
            }

        See https://python.langchain.com/docs/concepts/multimodality/

        Example:

        .. code-block:: python

            @property
            def supports_pdf_inputs(self) -> bool:
                return True

    .. dropdown:: supports_audio_inputs

        Boolean property indicating whether the chat model supports audio inputs.
        Defaults to ``False``.

        If set to ``True``, the chat model will be tested using content blocks of the
        form

        .. code-block:: python

            {
                "type": "audio",
                "source_type": "base64",
                "data": "<base64 audio data>",
                "mime_type": "audio/wav",  # or appropriate mime-type
            }

        See https://python.langchain.com/docs/concepts/multimodality/

        Example:

        .. code-block:: python

            @property
            def supports_audio_inputs(self) -> bool:
                return True

    .. dropdown:: supports_video_inputs

        Boolean property indicating whether the chat model supports image inputs.
        Defaults to ``False``. No current tests are written for this feature.

    .. dropdown:: returns_usage_metadata

        Boolean property indicating whether the chat model returns usage metadata
        on invoke and streaming responses.

        ``usage_metadata`` is an optional dict attribute on AIMessages that track input
        and output tokens: https://python.langchain.com/api_reference/core/messages/langchain_core.messages.ai.UsageMetadata.html

        Example:

        .. code-block:: python

            @property
            def returns_usage_metadata(self) -> bool:
                return False

        Models supporting ``usage_metadata`` should also return the name of the
        underlying model in the ``response_metadata`` of the AIMessage.

    .. dropdown:: supports_anthropic_inputs

        Boolean property indicating whether the chat model supports Anthropic-style
        inputs.

        These inputs might feature "tool use" and "tool result" content blocks, e.g.,

        .. code-block:: python

            [
                {"type": "text", "text": "Hmm let me think about that"},
                {
                    "type": "tool_use",
                    "input": {"fav_color": "green"},
                    "id": "foo",
                    "name": "color_picker",
                },
            ]

        If set to ``True``, the chat model will be tested using content blocks of this
        form.

        Example:

        .. code-block:: python

            @property
            def supports_anthropic_inputs(self) -> bool:
                return False

    .. dropdown:: supports_image_tool_message

        Boolean property indicating whether the chat model supports ToolMessages
        that include image content, e.g.,

        .. code-block:: python

            ToolMessage(
                content=[
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                    },
                ],
                tool_call_id="1",
                name="random_image",
            )

        (OpenAI Chat Completions format), as well as

        .. code-block:: python

            ToolMessage(
                content=[
                    {
                        "type": "image",
                        "source_type": "base64",
                        "data": image_data,
                        "mime_type": "image/jpeg",
                    },
                ],
                tool_call_id="1",
                name="random_image",
            )

        (standard format).

        If set to ``True``, the chat model will be tested with message sequences that
        include ToolMessages of this form.

        Example:

        .. code-block:: python

            @property
            def supports_image_tool_message(self) -> bool:
                return False

    .. dropdown:: supported_usage_metadata_details

        Property controlling what usage metadata details are emitted in both invoke
        and stream.

        ``usage_metadata`` is an optional dict attribute on AIMessages that track input
        and output tokens: https://python.langchain.com/api_reference/core/messages/langchain_core.messages.ai.UsageMetadata.html

        It includes optional keys ``input_token_details`` and ``output_token_details``
        that can track usage details associated with special types of tokens, such as
        cached, audio, or reasoning.

        Only needs to be overridden if these details are supplied.

    .. dropdown:: enable_vcr_tests

        Property controlling whether to enable select tests that rely on
        `VCR <https://vcrpy.readthedocs.io/en/latest/>`_ caching of HTTP calls, such
        as benchmarking tests.

        To enable these tests, follow these steps:

        1. Override the ``enable_vcr_tests`` property to return ``True``:

            .. code-block:: python

                @property
                def enable_vcr_tests(self) -> bool:
                    return True

        2. Configure VCR to exclude sensitive headers and other information from cassettes.

            .. important::
                VCR will by default record authentication headers and other sensitive
                information in cassettes. Read below for how to configure what
                information is recorded in cassettes.

            To add configuration to VCR, add a ``conftest.py`` file to the ``tests/``
            directory and implement the ``vcr_config`` fixture there.

            ``langchain-tests`` excludes the headers ``'authorization'``,
            ``'x-api-key'``, and ``'api-key'`` from VCR cassettes. To pick up this
            configuration, you will need to add ``conftest.py`` as shown below. You can
            also exclude additional headers, override the default exclusions, or apply
            other customizations to the VCR configuration. See example below:

            .. code-block:: python
                :caption: tests/conftest.py

                import pytest
                from langchain_tests.conftest import _base_vcr_config as _base_vcr_config

                _EXTRA_HEADERS = [
                    # Specify additional headers to redact
                    ("user-agent", "PLACEHOLDER"),
                ]


                def remove_response_headers(response: dict) -> dict:
                    # If desired, remove or modify headers in the response.
                    response["headers"] = {}
                    return response


                @pytest.fixture(scope="session")
                def vcr_config(_base_vcr_config: dict) -> dict:  # noqa: F811
                    \"\"\"Extend the default configuration from langchain_tests.\"\"\"
                    config = _base_vcr_config.copy()
                    config.setdefault("filter_headers", []).extend(_EXTRA_HEADERS)
                    config["before_record_response"] = remove_response_headers

                    return config

            .. dropdown:: Compressing cassettes

                ``langchain-tests`` includes a custom VCR serializer that compresses
                cassettes using gzip. To use it, register the ``yaml.gz`` serializer
                to your VCR fixture and enable this serializer in the config. See
                example below:

                .. code-block:: python
                    :caption: tests/conftest.py

                    import pytest
                    from langchain_tests.conftest import CustomPersister, CustomSerializer
                    from langchain_tests.conftest import _base_vcr_config as _base_vcr_config
                    from vcr import VCR

                    _EXTRA_HEADERS = [
                        # Specify additional headers to redact
                        ("user-agent", "PLACEHOLDER"),
                    ]


                    def remove_response_headers(response: dict) -> dict:
                        # If desired, remove or modify headers in the response.
                        response["headers"] = {}
                        return response


                    @pytest.fixture(scope="session")
                    def vcr_config(_base_vcr_config: dict) -> dict:  # noqa: F811
                        \"\"\"Extend the default configuration from langchain_tests.\"\"\"
                        config = _base_vcr_config.copy()
                        config.setdefault("filter_headers", []).extend(_EXTRA_HEADERS)
                        config["before_record_response"] = remove_response_headers
                        # New: enable serializer and set file extension
                        config["serializer"] = "yaml.gz"
                        config["path_transformer"] = VCR.ensure_suffix(".yaml.gz")

                        return config


                    def pytest_recording_configure(config: dict, vcr: VCR) -> None:
                        vcr.register_persister(CustomPersister())
                        vcr.register_serializer("yaml.gz", CustomSerializer())


                You can inspect the contents of the compressed cassettes (e.g., to
                ensure no sensitive information is recorded) using

                .. code-block:: bash

                    gunzip -k /path/to/tests/cassettes/TestClass_test.yaml.gz

                or by using the serializer:

                .. code-block:: python

                    from langchain_tests.conftest import CustomPersister, CustomSerializer

                    cassette_path = "/path/to/tests/cassettes/TestClass_test.yaml.gz"
                    requests, responses = CustomPersister().load_cassette(path, CustomSerializer())

        3. Run tests to generate VCR cassettes.

            Example:

            .. code-block:: bash

                uv run python -m pytest tests/integration_tests/test_chat_models.py::TestMyModel::test_stream_time

            This will generate a VCR cassette for the test in
            ``tests/integration_tests/cassettes/``.

            .. important::
                You should inspect the generated cassette to ensure that it does not
                contain sensitive information. If it does, you can modify the
                ``vcr_config`` fixture to exclude headers or modify the response
                before it is recorded.

            You can then commit the cassette to your repository. Subsequent test runs
            will use the cassette instead of making HTTP calls.

    Testing initialization from environment variables
        Some unit tests may require testing initialization from environment variables.
        These tests can be enabled by overriding the ``init_from_env_params``
        property (see below):

        .. dropdown:: init_from_env_params

            This property is used in unit tests to test initialization from
            environment variables. It should return a tuple of three dictionaries
            that specify the environment variables, additional initialization args,
            and expected instance attributes to check.

            Defaults to empty dicts. If not overridden, the test is skipped.

            Example:

            .. code-block:: python

                @property
                def init_from_env_params(self) -> Tuple[dict, dict, dict]:
                    return (
                        {
                            "MY_API_KEY": "api_key",
                        },
                        {
                            "model": "bird-brain-001",
                        },
                        {
                            "my_api_key": "api_key",
                        },
                    )

    """  # noqa: E501

    @property
    def standard_chat_model_params(self) -> dict:
        """:private:"""
        params = super().standard_chat_model_params
        params["api_key"] = "test"
        return params

    @property
    def init_from_env_params(self) -> tuple[dict, dict, dict]:
        """(tuple) environment variables, additional initialization args, and expected
        instance attributes for testing initialization from environment variables.
        """
        return {}, {}, {}

    def test_init(self) -> None:
        """Test model initialization. This should pass for all integrations.

        .. dropdown:: Troubleshooting

            If this test fails, ensure that:

            1. ``chat_model_params`` is specified and the model can be initialized from those params;
            2. The model accommodates standard parameters: https://python.langchain.com/docs/concepts/chat_models/#standard-parameters
        """  # noqa: E501
        model = self.chat_model_class(
            **{
                **self.standard_chat_model_params,
                **self.chat_model_params,
            }
        )
        assert model is not None

    def test_init_from_env(self) -> None:
        """Test initialization from environment variables. Relies on the
        ``init_from_env_params`` property. Test is skipped if that property is not
        set.

        .. dropdown:: Troubleshooting

            If this test fails, ensure that ``init_from_env_params`` is specified
            correctly and that model parameters are properly set from environment
            variables during initialization.
        """
        env_params, model_params, expected_attrs = self.init_from_env_params
        if not env_params:
            pytest.skip("init_from_env_params not specified.")
        else:
            with mock.patch.dict(os.environ, env_params):
                model = self.chat_model_class(**model_params)
            assert model is not None
            for k, expected in expected_attrs.items():
                actual = getattr(model, k)
                if isinstance(actual, SecretStr):
                    actual = actual.get_secret_value()
                assert actual == expected

    def test_init_streaming(
        self,
    ) -> None:
        """Test that model can be initialized with ``streaming=True``. This is for
        backward-compatibility purposes.

        .. dropdown:: Troubleshooting

            If this test fails, ensure that the model can be initialized with a
            boolean ``streaming`` parameter.
        """
        model = self.chat_model_class(
            **{
                **self.standard_chat_model_params,
                **self.chat_model_params,
                "streaming": True,
            }
        )
        assert model is not None

    def test_bind_tool_pydantic(
        self,
        model: BaseChatModel,
        my_adder_tool: BaseTool,
    ) -> None:
        """Test that chat model correctly handles Pydantic models that are passed
        into ``bind_tools``. Test is skipped if the ``has_tool_calling`` property
        on the test class is False.

        .. dropdown:: Troubleshooting

            If this test fails, ensure that the model's ``bind_tools`` method
            properly handles Pydantic V2 models. ``langchain_core`` implements
            a utility function that will accommodate most formats: https://python.langchain.com/api_reference/core/utils/langchain_core.utils.function_calling.convert_to_openai_tool.html

            See example implementation of ``bind_tools`` here: https://python.langchain.com/api_reference/_modules/langchain_openai/chat_models/base.html#BaseChatOpenAI.bind_tools
        """
        if not self.has_tool_calling:
            return

        def my_adder(a: int, b: int) -> int:
            """Takes two integers, a and b, and returns their sum."""
            return a + b

        tools = [my_adder_tool, my_adder]

        for pydantic_model in TEST_PYDANTIC_MODELS:
            model_schema = (
                pydantic_model.model_json_schema()
                if hasattr(pydantic_model, "model_json_schema")
                else pydantic_model.schema()
            )
            tools.extend([pydantic_model, model_schema])

        # Doing a mypy ignore here since some of the tools are from pydantic
        # BaseModel 2 which isn't typed properly yet. This will need to be fixed
        # so type checking does not become annoying to users.
        tool_model = model.bind_tools(tools, tool_choice="any")  # type: ignore[arg-type]
        assert isinstance(tool_model, RunnableBinding)

    @pytest.mark.parametrize("schema", TEST_PYDANTIC_MODELS)
    def test_with_structured_output(
        self,
        model: BaseChatModel,
        schema: Any,
    ) -> None:
        """Test ``with_structured_output`` method. Test is skipped if the
        ``has_structured_output`` property on the test class is False.

        .. dropdown:: Troubleshooting

            If this test fails, ensure that the model's ``bind_tools`` method
            properly handles Pydantic V2 models. ``langchain_core`` implements
            a utility function that will accommodate most formats: https://python.langchain.com/api_reference/core/utils/langchain_core.utils.function_calling.convert_to_openai_tool.html

            See example implementation of ``with_structured_output`` here: https://python.langchain.com/api_reference/_modules/langchain_openai/chat_models/base.html#BaseChatOpenAI.with_structured_output
        """
        if not self.has_structured_output:
            return

        assert model.with_structured_output(schema) is not None
        for method in ["json_schema", "function_calling", "json_mode"]:
            strict_values = [None, False, True] if method != "json_mode" else [None]
            for strict in strict_values:
                assert model.with_structured_output(
                    schema, method=method, strict=strict
                )

    def test_standard_params(self, model: BaseChatModel) -> None:
        """Test that model properly generates standard parameters. These are used
        for tracing purposes.

        .. dropdown:: Troubleshooting

            If this test fails, check that the model accommodates `standard parameters <https://python.langchain.com/docs/concepts/chat_models/#standard-parameters>`__.

            Check also that the model class is named according to convention
            (e.g., ``ChatProviderName``).
        """

        class ExpectedParams(BaseModelV1):
            ls_provider: str
            ls_model_name: str
            ls_model_type: Literal["chat"]
            ls_temperature: Optional[float]
            ls_max_tokens: Optional[int]
            ls_stop: Optional[list[str]]

        ls_params = model._get_ls_params()
        try:
            ExpectedParams(**ls_params)  # type: ignore[arg-type]
        except ValidationErrorV1 as e:
            pytest.fail(f"Validation error: {e}")

        # Test optional params
        model = self.chat_model_class(
            max_tokens=10,  # type: ignore[call-arg]
            stop=["test"],  # type: ignore[call-arg]
            **self.chat_model_params,
        )
        ls_params = model._get_ls_params()
        try:
            ExpectedParams(**ls_params)  # type: ignore[arg-type]
        except ValidationErrorV1 as e:
            pytest.fail(f"Validation error: {e}")

    def test_serdes(self, model: BaseChatModel, snapshot: SnapshotAssertion) -> None:
        """Test serialization and deserialization of the model. Test is skipped if the
        ``is_lc_serializable`` property on the chat model class is not overwritten
        to return ``True``.

        .. dropdown:: Troubleshooting

            If this test fails, check that the ``init_from_env_params`` property is
            correctly set on the test class.
        """
        if not self.chat_model_class.is_lc_serializable():
            pytest.skip("Model is not serializable.")
        else:
            env_params, _model_params, _expected_attrs = self.init_from_env_params
            with mock.patch.dict(os.environ, env_params):
                ser = dumpd(model)
                assert ser == snapshot(name="serialized")
                assert (
                    model.dict()
                    == load(
                        dumpd(model), valid_namespaces=model.get_lc_namespace()[:1]
                    ).dict()
                )

    @pytest.mark.benchmark
    def test_init_time(self, benchmark: BenchmarkFixture) -> None:
        """Test initialization time of the chat model. If this test fails, check that
        we are not introducing undue overhead in the model's initialization.
        """

        def _init_in_loop() -> None:
            for _ in range(10):
                self.chat_model_class(**self.chat_model_params)

        benchmark(_init_in_loop)
