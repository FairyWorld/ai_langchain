"""Base classes and utilities for LangChain tools."""

from __future__ import annotations

import functools
import inspect
import json
import typing
import warnings
from abc import ABC, abstractmethod
from inspect import signature
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Literal,
    Optional,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PydanticDeprecationWarning,
    SkipValidation,
    ValidationError,
    model_validator,
    validate_arguments,
)
from pydantic.v1 import BaseModel as BaseModelV1
from pydantic.v1 import ValidationError as ValidationErrorV1
from pydantic.v1 import validate_arguments as validate_arguments_v1
from typing_extensions import override

from langchain_core._api import deprecated
from langchain_core.callbacks import (
    AsyncCallbackManager,
    BaseCallbackManager,
    CallbackManager,
    Callbacks,
)
from langchain_core.messages.tool import ToolCall, ToolMessage, ToolOutputMixin
from langchain_core.runnables import (
    RunnableConfig,
    RunnableSerializable,
    ensure_config,
    patch_config,
    run_in_executor,
)
from langchain_core.runnables.config import set_config_context
from langchain_core.runnables.utils import coro_with_context
from langchain_core.utils.function_calling import (
    _parse_google_docstring,
    _py_38_safe_origin,
)
from langchain_core.utils.pydantic import (
    TypeBaseModel,
    _create_subset_model,
    get_fields,
    is_basemodel_subclass,
    is_pydantic_v1_subclass,
    is_pydantic_v2_subclass,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

FILTERED_ARGS = ("run_manager", "callbacks")
TOOL_MESSAGE_BLOCK_TYPES = ("text", "image_url", "image", "json", "search_result")


class SchemaAnnotationError(TypeError):
    """Raised when args_schema is missing or has an incorrect type annotation."""


def _is_annotated_type(typ: type[Any]) -> bool:
    """Check if a type is an Annotated type.

    Args:
        typ: The type to check.

    Returns:
        True if the type is an Annotated type, False otherwise.
    """
    return get_origin(typ) is typing.Annotated


def _get_annotation_description(arg_type: type) -> str | None:
    """Extract description from an Annotated type.

    Args:
        arg_type: The type to extract description from.

    Returns:
        The description string if found, None otherwise.
    """
    if _is_annotated_type(arg_type):
        annotated_args = get_args(arg_type)
        for annotation in annotated_args[1:]:
            if isinstance(annotation, str):
                return annotation
    return None


def _get_filtered_args(
    inferred_model: type[BaseModel],
    func: Callable,
    *,
    filter_args: Sequence[str],
    include_injected: bool = True,
) -> dict:
    """Get filtered arguments from a function's signature.

    Args:
        inferred_model: The Pydantic model inferred from the function.
        func: The function to extract arguments from.
        filter_args: Arguments to exclude from the result.
        include_injected: Whether to include injected arguments.

    Returns:
        Dictionary of filtered arguments with their schema definitions.
    """
    schema = inferred_model.model_json_schema()["properties"]
    valid_keys = signature(func).parameters
    return {
        k: schema[k]
        for i, (k, param) in enumerate(valid_keys.items())
        if k not in filter_args
        and (i > 0 or param.name not in {"self", "cls"})
        and (include_injected or not _is_injected_arg_type(param.annotation))
    }


def _parse_python_function_docstring(
    function: Callable, annotations: dict, *, error_on_invalid_docstring: bool = False
) -> tuple[str, dict]:
    """Parse function and argument descriptions from a docstring.

    Assumes the function docstring follows Google Python style guide.

    Args:
        function: The function to parse the docstring from.
        annotations: Type annotations for the function parameters.
        error_on_invalid_docstring: Whether to raise an error on invalid docstring.

    Returns:
        A tuple containing the function description and argument descriptions.
    """
    docstring = inspect.getdoc(function)
    return _parse_google_docstring(
        docstring,
        list(annotations),
        error_on_invalid_docstring=error_on_invalid_docstring,
    )


def _validate_docstring_args_against_annotations(
    arg_descriptions: dict, annotations: dict
) -> None:
    """Validate that docstring arguments match function annotations.

    Args:
        arg_descriptions: Arguments described in the docstring.
        annotations: Type annotations from the function signature.

    Raises:
        ValueError: If a docstring argument is not found in function signature.
    """
    for docstring_arg in arg_descriptions:
        if docstring_arg not in annotations:
            msg = f"Arg {docstring_arg} in docstring not found in function signature."
            raise ValueError(msg)


def _infer_arg_descriptions(
    fn: Callable,
    *,
    parse_docstring: bool = False,
    error_on_invalid_docstring: bool = False,
) -> tuple[str, dict]:
    """Infer argument descriptions from function docstring and annotations.

    Args:
        fn: The function to infer descriptions from.
        parse_docstring: Whether to parse the docstring for descriptions.
        error_on_invalid_docstring: Whether to raise error on invalid docstring.

    Returns:
        A tuple containing the function description and argument descriptions.
    """
    annotations = typing.get_type_hints(fn, include_extras=True)
    if parse_docstring:
        description, arg_descriptions = _parse_python_function_docstring(
            fn, annotations, error_on_invalid_docstring=error_on_invalid_docstring
        )
    else:
        description = inspect.getdoc(fn) or ""
        arg_descriptions = {}
    if parse_docstring:
        _validate_docstring_args_against_annotations(arg_descriptions, annotations)
    for arg, arg_type in annotations.items():
        if arg in arg_descriptions:
            continue
        if desc := _get_annotation_description(arg_type):
            arg_descriptions[arg] = desc
    return description, arg_descriptions


def _is_pydantic_annotation(annotation: Any, pydantic_version: str = "v2") -> bool:
    """Check if a type annotation is a Pydantic model.

    Args:
        annotation: The type annotation to check.
        pydantic_version: The Pydantic version to check against ("v1" or "v2").

    Returns:
        True if the annotation is a Pydantic model, False otherwise.
    """
    base_model_class = BaseModelV1 if pydantic_version == "v1" else BaseModel
    try:
        return issubclass(annotation, base_model_class)
    except TypeError:
        return False


def _function_annotations_are_pydantic_v1(
    signature: inspect.Signature, func: Callable
) -> bool:
    """Check if all Pydantic annotations in a function are from V1.

    Args:
        signature: The function signature to check.
        func: The function being checked.

    Returns:
        True if all Pydantic annotations are from V1, False otherwise.

    Raises:
        NotImplementedError: If the function contains mixed V1 and V2 annotations.
    """
    any_v1_annotations = any(
        _is_pydantic_annotation(parameter.annotation, pydantic_version="v1")
        for parameter in signature.parameters.values()
    )
    any_v2_annotations = any(
        _is_pydantic_annotation(parameter.annotation, pydantic_version="v2")
        for parameter in signature.parameters.values()
    )
    if any_v1_annotations and any_v2_annotations:
        msg = (
            f"Function {func} contains a mix of Pydantic v1 and v2 annotations. "
            "Only one version of Pydantic annotations per function is supported."
        )
        raise NotImplementedError(msg)
    return any_v1_annotations and not any_v2_annotations


class _SchemaConfig:
    """Configuration for Pydantic models generated from function signatures.

    Attributes:
        extra: Whether to allow extra fields in the model.
        arbitrary_types_allowed: Whether to allow arbitrary types in the model.
    """

    extra: str = "forbid"
    arbitrary_types_allowed: bool = True


def create_schema_from_function(
    model_name: str,
    func: Callable,
    *,
    filter_args: Optional[Sequence[str]] = None,
    parse_docstring: bool = False,
    error_on_invalid_docstring: bool = False,
    include_injected: bool = True,
) -> type[BaseModel]:
    """Create a pydantic schema from a function's signature.

    Args:
        model_name: Name to assign to the generated pydantic schema.
        func: Function to generate the schema from.
        filter_args: Optional list of arguments to exclude from the schema.
            Defaults to FILTERED_ARGS.
        parse_docstring: Whether to parse the function's docstring for descriptions
            for each argument. Defaults to False.
        error_on_invalid_docstring: if ``parse_docstring`` is provided, configure
            whether to raise ValueError on invalid Google Style docstrings.
            Defaults to False.
        include_injected: Whether to include injected arguments in the schema.
            Defaults to True, since we want to include them in the schema
            when *validating* tool inputs.

    Returns:
        A pydantic model with the same arguments as the function.
    """
    sig = inspect.signature(func)

    if _function_annotations_are_pydantic_v1(sig, func):
        validated = validate_arguments_v1(func, config=_SchemaConfig)  # type: ignore[call-overload]
    else:
        # https://docs.pydantic.dev/latest/usage/validation_decorator/
        with warnings.catch_warnings():
            # We are using deprecated functionality here.
            # This code should be re-written to simply construct a pydantic model
            # using inspect.signature and create_model.
            warnings.simplefilter("ignore", category=PydanticDeprecationWarning)
            validated = validate_arguments(func, config=_SchemaConfig)  # type: ignore[operator]

    # Let's ignore `self` and `cls` arguments for class and instance methods
    # If qualified name has a ".", then it likely belongs in a class namespace
    in_class = bool(func.__qualname__ and "." in func.__qualname__)

    has_args = False
    has_kwargs = False

    for param in sig.parameters.values():
        if param.kind == param.VAR_POSITIONAL:
            has_args = True
        elif param.kind == param.VAR_KEYWORD:
            has_kwargs = True

    inferred_model = validated.model

    if filter_args:
        filter_args_ = filter_args
    else:
        # Handle classmethods and instance methods
        existing_params: list[str] = list(sig.parameters.keys())
        if existing_params and existing_params[0] in {"self", "cls"} and in_class:
            filter_args_ = [existing_params[0], *list(FILTERED_ARGS)]
        else:
            filter_args_ = list(FILTERED_ARGS)

        for existing_param in existing_params:
            if not include_injected and _is_injected_arg_type(
                sig.parameters[existing_param].annotation
            ):
                filter_args_.append(existing_param)

    description, arg_descriptions = _infer_arg_descriptions(
        func,
        parse_docstring=parse_docstring,
        error_on_invalid_docstring=error_on_invalid_docstring,
    )
    # Pydantic adds placeholder virtual fields we need to strip
    valid_properties = []
    for field in get_fields(inferred_model):
        if not has_args and field == "args":
            continue
        if not has_kwargs and field == "kwargs":
            continue

        if field == "v__duplicate_kwargs":  # Internal pydantic field
            continue

        if field not in filter_args_:
            valid_properties.append(field)

    return _create_subset_model(
        model_name,
        inferred_model,
        list(valid_properties),
        descriptions=arg_descriptions,
        fn_description=description,
    )


class ToolException(Exception):  # noqa: N818
    """Exception thrown when a tool execution error occurs.

    This exception allows tools to signal errors without stopping the agent.
    The error is handled according to the tool's handle_tool_error setting,
    and the result is returned as an observation to the agent.
    """


ArgsSchema = Union[TypeBaseModel, dict[str, Any]]


class BaseTool(RunnableSerializable[Union[str, dict, ToolCall], Any]):
    """Base class for all LangChain tools.

    This abstract class defines the interface that all LangChain tools must implement.
    Tools are components that can be called by agents to perform specific actions.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Validate the tool class definition during subclass creation.

        Args:
            **kwargs: Additional keyword arguments passed to the parent class.

        Raises:
            SchemaAnnotationError: If args_schema has incorrect type annotation.
        """
        super().__init_subclass__(**kwargs)

        args_schema_type = cls.__annotations__.get("args_schema", None)

        if args_schema_type is not None and args_schema_type == BaseModel:
            # Throw errors for common mis-annotations.
            # TODO: Use get_args / get_origin and fully
            # specify valid annotations.
            typehint_mandate = """
class ChildTool(BaseTool):
    ...
    args_schema: Type[BaseModel] = SchemaClass
    ..."""
            name = cls.__name__
            msg = (
                f"Tool definition for {name} must include valid type annotations"
                f" for argument 'args_schema' to behave as expected.\n"
                f"Expected annotation of 'Type[BaseModel]'"
                f" but got '{args_schema_type}'.\n"
                f"Expected class looks like:\n"
                f"{typehint_mandate}"
            )
            raise SchemaAnnotationError(msg)

    name: str
    """The unique name of the tool that clearly communicates its purpose."""
    description: str
    """Used to tell the model how/when/why to use the tool.

    You can provide few-shot examples as a part of the description.
    """

    args_schema: Annotated[Optional[ArgsSchema], SkipValidation()] = Field(
        default=None, description="The tool schema."
    )
    """Pydantic model class to validate and parse the tool's input arguments.

    Args schema should be either:

    - A subclass of pydantic.BaseModel.
    - A subclass of pydantic.v1.BaseModel if accessing v1 namespace in pydantic 2
    - a JSON schema dict
    """
    return_direct: bool = False
    """Whether to return the tool's output directly.

    Setting this to True means
    that after the tool is called, the AgentExecutor will stop looping.
    """
    verbose: bool = False
    """Whether to log the tool's progress."""

    callbacks: Callbacks = Field(default=None, exclude=True)
    """Callbacks to be called during tool execution."""

    callback_manager: Optional[BaseCallbackManager] = deprecated(
        name="callback_manager", since="0.1.7", removal="1.0", alternative="callbacks"
    )(
        Field(
            default=None,
            exclude=True,
            description="Callback manager to add to the run trace.",
        )
    )
    tags: Optional[list[str]] = None
    """Optional list of tags associated with the tool. Defaults to None.
    These tags will be associated with each call to this tool,
    and passed as arguments to the handlers defined in `callbacks`.
    You can use these to eg identify a specific instance of a tool with its use case.
    """
    metadata: Optional[dict[str, Any]] = None
    """Optional metadata associated with the tool. Defaults to None.
    This metadata will be associated with each call to this tool,
    and passed as arguments to the handlers defined in `callbacks`.
    You can use these to eg identify a specific instance of a tool with its use case.
    """

    handle_tool_error: Optional[Union[bool, str, Callable[[ToolException], str]]] = (
        False
    )
    """Handle the content of the ToolException thrown."""

    handle_validation_error: Optional[
        Union[bool, str, Callable[[Union[ValidationError, ValidationErrorV1]], str]]
    ] = False
    """Handle the content of the ValidationError thrown."""

    response_format: Literal["content", "content_and_artifact"] = "content"
    """The tool response format. Defaults to 'content'.

    If "content" then the output of the tool is interpreted as the contents of a
    ToolMessage. If "content_and_artifact" then the output is expected to be a
    two-tuple corresponding to the (content, artifact) of a ToolMessage.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the tool."""
        if (
            "args_schema" in kwargs
            and kwargs["args_schema"] is not None
            and not is_basemodel_subclass(kwargs["args_schema"])
            and not isinstance(kwargs["args_schema"], dict)
        ):
            msg = (
                "args_schema must be a subclass of pydantic BaseModel or "
                f"a JSON schema dict. Got: {kwargs['args_schema']}."
            )
            raise TypeError(msg)
        super().__init__(**kwargs)

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    @property
    def is_single_input(self) -> bool:
        """Check if the tool accepts only a single input argument.

        Returns:
            True if the tool has only one input argument, False otherwise.
        """
        keys = {k for k in self.args if k != "kwargs"}
        return len(keys) == 1

    @property
    def args(self) -> dict:
        """Get the tool's input arguments schema.

        Returns:
            Dictionary containing the tool's argument properties.
        """
        if isinstance(self.args_schema, dict):
            json_schema = self.args_schema
        else:
            input_schema = self.get_input_schema()
            json_schema = input_schema.model_json_schema()
        return json_schema["properties"]

    @property
    def tool_call_schema(self) -> ArgsSchema:
        """Get the schema for tool calls, excluding injected arguments.

        Returns:
            The schema that should be used for tool calls from language models.
        """
        if isinstance(self.args_schema, dict):
            if self.description:
                return {
                    **self.args_schema,
                    "description": self.description,
                }

            return self.args_schema

        full_schema = self.get_input_schema()
        fields = []
        for name, type_ in get_all_basemodel_annotations(full_schema).items():
            if not _is_injected_arg_type(type_):
                fields.append(name)
        return _create_subset_model(
            self.name, full_schema, fields, fn_description=self.description
        )

    # --- Runnable ---

    @override
    def get_input_schema(
        self, config: Optional[RunnableConfig] = None
    ) -> type[BaseModel]:
        """The tool's input schema.

        Args:
            config: The configuration for the tool.

        Returns:
            The input schema for the tool.
        """
        if self.args_schema is not None:
            if isinstance(self.args_schema, dict):
                return super().get_input_schema(config)
            return self.args_schema
        return create_schema_from_function(self.name, self._run)

    @override
    def invoke(
        self,
        input: Union[str, dict, ToolCall],
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Any:
        tool_input, kwargs = _prep_run_args(input, config, **kwargs)
        return self.run(tool_input, **kwargs)

    @override
    async def ainvoke(
        self,
        input: Union[str, dict, ToolCall],
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Any:
        tool_input, kwargs = _prep_run_args(input, config, **kwargs)
        return await self.arun(tool_input, **kwargs)

    # --- Tool ---

    def _parse_input(
        self, tool_input: Union[str, dict], tool_call_id: Optional[str]
    ) -> Union[str, dict[str, Any]]:
        """Parse and validate tool input using the args schema.

        Args:
            tool_input: The raw input to the tool.
            tool_call_id: The ID of the tool call, if available.

        Returns:
            The parsed and validated input.

        Raises:
            ValueError: If string input is provided with JSON schema or if
                InjectedToolCallId is required but not provided.
            NotImplementedError: If args_schema is not a supported type.
        """
        input_args = self.args_schema
        if isinstance(tool_input, str):
            if input_args is not None:
                if isinstance(input_args, dict):
                    msg = (
                        "String tool inputs are not allowed when "
                        "using tools with JSON schema args_schema."
                    )
                    raise ValueError(msg)
                key_ = next(iter(get_fields(input_args).keys()))
                if issubclass(input_args, BaseModel):
                    input_args.model_validate({key_: tool_input})
                elif issubclass(input_args, BaseModelV1):
                    input_args.parse_obj({key_: tool_input})
                else:
                    msg = f"args_schema must be a Pydantic BaseModel, got {input_args}"
                    raise TypeError(msg)
            return tool_input
        if input_args is not None:
            if isinstance(input_args, dict):
                return tool_input
            if issubclass(input_args, BaseModel):
                for k, v in get_all_basemodel_annotations(input_args).items():
                    if (
                        _is_injected_arg_type(v, injected_type=InjectedToolCallId)
                        and k not in tool_input
                    ):
                        if tool_call_id is None:
                            msg = (
                                "When tool includes an InjectedToolCallId "
                                "argument, tool must always be invoked with a full "
                                "model ToolCall of the form: {'args': {...}, "
                                "'name': '...', 'type': 'tool_call', "
                                "'tool_call_id': '...'}"
                            )
                            raise ValueError(msg)
                        tool_input[k] = tool_call_id
                result = input_args.model_validate(tool_input)
                result_dict = result.model_dump()
            elif issubclass(input_args, BaseModelV1):
                for k, v in get_all_basemodel_annotations(input_args).items():
                    if (
                        _is_injected_arg_type(v, injected_type=InjectedToolCallId)
                        and k not in tool_input
                    ):
                        if tool_call_id is None:
                            msg = (
                                "When tool includes an InjectedToolCallId "
                                "argument, tool must always be invoked with a full "
                                "model ToolCall of the form: {'args': {...}, "
                                "'name': '...', 'type': 'tool_call', "
                                "'tool_call_id': '...'}"
                            )
                            raise ValueError(msg)
                        tool_input[k] = tool_call_id
                result = input_args.parse_obj(tool_input)
                result_dict = result.dict()
            else:
                msg = (
                    f"args_schema must be a Pydantic BaseModel, got {self.args_schema}"
                )
                raise NotImplementedError(msg)
            return {
                k: getattr(result, k) for k, v in result_dict.items() if k in tool_input
            }
        return tool_input

    @model_validator(mode="before")
    @classmethod
    def raise_deprecation(cls, values: dict) -> Any:
        """Raise deprecation warning if callback_manager is used.

        Args:
            values: The values to validate.

        Returns:
            The validated values.
        """
        if values.get("callback_manager") is not None:
            warnings.warn(
                "callback_manager is deprecated. Please use callbacks instead.",
                DeprecationWarning,
                stacklevel=6,
            )
            values["callbacks"] = values.pop("callback_manager", None)
        return values

    @abstractmethod
    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Use the tool.

        Add run_manager: Optional[CallbackManagerForToolRun] = None
        to child implementations to enable tracing.
        """

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Use the tool asynchronously.

        Add run_manager: Optional[AsyncCallbackManagerForToolRun] = None
        to child implementations to enable tracing.
        """
        if kwargs.get("run_manager") and signature(self._run).parameters.get(
            "run_manager"
        ):
            kwargs["run_manager"] = kwargs["run_manager"].get_sync()
        return await run_in_executor(None, self._run, *args, **kwargs)

    def _to_args_and_kwargs(
        self, tool_input: Union[str, dict], tool_call_id: Optional[str]
    ) -> tuple[tuple, dict]:
        """Convert tool input to positional and keyword arguments.

        Args:
            tool_input: The input to the tool.
            tool_call_id: The ID of the tool call, if available.

        Returns:
            A tuple of (positional_args, keyword_args) for the tool.

        Raises:
            TypeError: If the tool input type is invalid.
        """
        if (
            self.args_schema is not None
            and isinstance(self.args_schema, type)
            and is_basemodel_subclass(self.args_schema)
            and not get_fields(self.args_schema)
        ):
            # StructuredTool with no args
            return (), {}
        tool_input = self._parse_input(tool_input, tool_call_id)
        # For backwards compatibility, if run_input is a string,
        # pass as a positional argument.
        if isinstance(tool_input, str):
            return (tool_input,), {}
        if isinstance(tool_input, dict):
            # Make a shallow copy of the input to allow downstream code
            # to modify the root level of the input without affecting the
            # original input.
            # This is used by the tool to inject run time information like
            # the callback manager.
            return (), tool_input.copy()
        # This code path is not expected to be reachable.
        msg = f"Invalid tool input type: {type(tool_input)}"
        raise TypeError(msg)

    def run(
        self,
        tool_input: Union[str, dict[str, Any]],
        verbose: Optional[bool] = None,  # noqa: FBT001
        start_color: Optional[str] = "green",
        color: Optional[str] = "green",
        callbacks: Callbacks = None,
        *,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_name: Optional[str] = None,
        run_id: Optional[uuid.UUID] = None,
        config: Optional[RunnableConfig] = None,
        tool_call_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Run the tool.

        Args:
            tool_input: The input to the tool.
            verbose: Whether to log the tool's progress. Defaults to None.
            start_color: The color to use when starting the tool. Defaults to 'green'.
            color: The color to use when ending the tool. Defaults to 'green'.
            callbacks: Callbacks to be called during tool execution. Defaults to None.
            tags: Optional list of tags associated with the tool. Defaults to None.
            metadata: Optional metadata associated with the tool. Defaults to None.
            run_name: The name of the run. Defaults to None.
            run_id: The id of the run. Defaults to None.
            config: The configuration for the tool. Defaults to None.
            tool_call_id: The id of the tool call. Defaults to None.
            kwargs: Keyword arguments to be passed to tool callbacks (event handler)

        Returns:
            The output of the tool.

        Raises:
            ToolException: If an error occurs during tool execution.
        """
        callback_manager = CallbackManager.configure(
            callbacks,
            self.callbacks,
            self.verbose or bool(verbose),
            tags,
            self.tags,
            metadata,
            self.metadata,
        )

        run_manager = callback_manager.on_tool_start(
            {"name": self.name, "description": self.description},
            tool_input if isinstance(tool_input, str) else str(tool_input),
            color=start_color,
            name=run_name,
            run_id=run_id,
            # Inputs by definition should always be dicts.
            # For now, it's unclear whether this assumption is ever violated,
            # but if it is we will send a `None` value to the callback instead
            # TODO: will need to address issue via a patch.
            inputs=tool_input if isinstance(tool_input, dict) else None,
            **kwargs,
        )

        content = None
        artifact = None
        status = "success"
        error_to_raise: Union[Exception, KeyboardInterrupt, None] = None
        try:
            child_config = patch_config(config, callbacks=run_manager.get_child())
            with set_config_context(child_config) as context:
                tool_args, tool_kwargs = self._to_args_and_kwargs(
                    tool_input, tool_call_id
                )
                if signature(self._run).parameters.get("run_manager"):
                    tool_kwargs |= {"run_manager": run_manager}
                if config_param := _get_runnable_config_param(self._run):
                    tool_kwargs |= {config_param: config}
                response = context.run(self._run, *tool_args, **tool_kwargs)
            if self.response_format == "content_and_artifact":
                if not isinstance(response, tuple) or len(response) != 2:
                    msg = (
                        "Since response_format='content_and_artifact' "
                        "a two-tuple of the message content and raw tool output is "
                        f"expected. Instead generated response of type: "
                        f"{type(response)}."
                    )
                    error_to_raise = ValueError(msg)
                else:
                    content, artifact = response
            else:
                content = response
        except (ValidationError, ValidationErrorV1) as e:
            if not self.handle_validation_error:
                error_to_raise = e
            else:
                content = _handle_validation_error(e, flag=self.handle_validation_error)
                status = "error"
        except ToolException as e:
            if not self.handle_tool_error:
                error_to_raise = e
            else:
                content = _handle_tool_error(e, flag=self.handle_tool_error)
                status = "error"
        except (Exception, KeyboardInterrupt) as e:
            error_to_raise = e

        if error_to_raise:
            run_manager.on_tool_error(error_to_raise)
            raise error_to_raise
        output = _format_output(content, artifact, tool_call_id, self.name, status)
        run_manager.on_tool_end(output, color=color, name=self.name, **kwargs)
        return output

    async def arun(
        self,
        tool_input: Union[str, dict],
        verbose: Optional[bool] = None,  # noqa: FBT001
        start_color: Optional[str] = "green",
        color: Optional[str] = "green",
        callbacks: Callbacks = None,
        *,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        run_name: Optional[str] = None,
        run_id: Optional[uuid.UUID] = None,
        config: Optional[RunnableConfig] = None,
        tool_call_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Run the tool asynchronously.

        Args:
            tool_input: The input to the tool.
            verbose: Whether to log the tool's progress. Defaults to None.
            start_color: The color to use when starting the tool. Defaults to 'green'.
            color: The color to use when ending the tool. Defaults to 'green'.
            callbacks: Callbacks to be called during tool execution. Defaults to None.
            tags: Optional list of tags associated with the tool. Defaults to None.
            metadata: Optional metadata associated with the tool. Defaults to None.
            run_name: The name of the run. Defaults to None.
            run_id: The id of the run. Defaults to None.
            config: The configuration for the tool. Defaults to None.
            tool_call_id: The id of the tool call. Defaults to None.
            kwargs: Keyword arguments to be passed to tool callbacks

        Returns:
            The output of the tool.

        Raises:
            ToolException: If an error occurs during tool execution.
        """
        callback_manager = AsyncCallbackManager.configure(
            callbacks,
            self.callbacks,
            self.verbose or bool(verbose),
            tags,
            self.tags,
            metadata,
            self.metadata,
        )
        run_manager = await callback_manager.on_tool_start(
            {"name": self.name, "description": self.description},
            tool_input if isinstance(tool_input, str) else str(tool_input),
            color=start_color,
            name=run_name,
            run_id=run_id,
            # Inputs by definition should always be dicts.
            # For now, it's unclear whether this assumption is ever violated,
            # but if it is we will send a `None` value to the callback instead
            # TODO: will need to address issue via a patch.
            inputs=tool_input if isinstance(tool_input, dict) else None,
            **kwargs,
        )
        content = None
        artifact = None
        status = "success"
        error_to_raise: Optional[Union[Exception, KeyboardInterrupt]] = None
        try:
            tool_args, tool_kwargs = self._to_args_and_kwargs(tool_input, tool_call_id)
            child_config = patch_config(config, callbacks=run_manager.get_child())
            with set_config_context(child_config) as context:
                func_to_check = (
                    self._run if self.__class__._arun is BaseTool._arun else self._arun  # noqa: SLF001
                )
                if signature(func_to_check).parameters.get("run_manager"):
                    tool_kwargs["run_manager"] = run_manager
                if config_param := _get_runnable_config_param(func_to_check):
                    tool_kwargs[config_param] = config

                coro = self._arun(*tool_args, **tool_kwargs)
                response = await coro_with_context(coro, context)
            if self.response_format == "content_and_artifact":
                if not isinstance(response, tuple) or len(response) != 2:
                    msg = (
                        "Since response_format='content_and_artifact' "
                        "a two-tuple of the message content and raw tool output is "
                        f"expected. Instead generated response of type: "
                        f"{type(response)}."
                    )
                    error_to_raise = ValueError(msg)
                else:
                    content, artifact = response
            else:
                content = response
        except ValidationError as e:
            if not self.handle_validation_error:
                error_to_raise = e
            else:
                content = _handle_validation_error(e, flag=self.handle_validation_error)
                status = "error"
        except ToolException as e:
            if not self.handle_tool_error:
                error_to_raise = e
            else:
                content = _handle_tool_error(e, flag=self.handle_tool_error)
                status = "error"
        except (Exception, KeyboardInterrupt) as e:
            error_to_raise = e

        if error_to_raise:
            await run_manager.on_tool_error(error_to_raise)
            raise error_to_raise

        output = _format_output(content, artifact, tool_call_id, self.name, status)
        await run_manager.on_tool_end(output, color=color, name=self.name, **kwargs)
        return output

    @deprecated("0.1.47", alternative="invoke", removal="1.0")
    def __call__(self, tool_input: str, callbacks: Callbacks = None) -> str:
        """Make tool callable (deprecated).

        Args:
            tool_input: The input to the tool.
            callbacks: Callbacks to use during execution.

        Returns:
            The tool's output.
        """
        return self.run(tool_input, callbacks=callbacks)


def _is_tool_call(x: Any) -> bool:
    """Check if the input is a tool call dictionary.

    Args:
        x: The input to check.

    Returns:
        True if the input is a tool call, False otherwise.
    """
    return isinstance(x, dict) and x.get("type") == "tool_call"


def _handle_validation_error(
    e: Union[ValidationError, ValidationErrorV1],
    *,
    flag: Union[
        Literal[True], str, Callable[[Union[ValidationError, ValidationErrorV1]], str]
    ],
) -> str:
    """Handle validation errors based on the configured flag.

    Args:
        e: The validation error that occurred.
        flag: How to handle the error (bool, string, or callable).

    Returns:
        The error message to return.

    Raises:
        ValueError: If the flag type is unexpected.
    """
    if isinstance(flag, bool):
        content = "Tool input validation error"
    elif isinstance(flag, str):
        content = flag
    elif callable(flag):
        content = flag(e)
    else:
        msg = (
            f"Got unexpected type of `handle_validation_error`. Expected bool, "
            f"str or callable. Received: {flag}"
        )
        raise ValueError(msg)  # noqa: TRY004
    return content


def _handle_tool_error(
    e: ToolException,
    *,
    flag: Optional[Union[Literal[True], str, Callable[[ToolException], str]]],
) -> str:
    """Handle tool execution errors based on the configured flag.

    Args:
        e: The tool exception that occurred.
        flag: How to handle the error (bool, string, or callable).

    Returns:
        The error message to return.

    Raises:
        ValueError: If the flag type is unexpected.
    """
    if isinstance(flag, bool):
        content = e.args[0] if e.args else "Tool execution error"
    elif isinstance(flag, str):
        content = flag
    elif callable(flag):
        content = flag(e)
    else:
        msg = (
            f"Got unexpected type of `handle_tool_error`. Expected bool, str "
            f"or callable. Received: {flag}"
        )
        raise ValueError(msg)  # noqa: TRY004
    return content


def _prep_run_args(
    value: Union[str, dict, ToolCall],
    config: Optional[RunnableConfig],
    **kwargs: Any,
) -> tuple[Union[str, dict], dict]:
    """Prepare arguments for tool execution.

    Args:
        value: The input value (string, dict, or ToolCall).
        config: The runnable configuration.
        **kwargs: Additional keyword arguments.

    Returns:
        A tuple of (tool_input, run_kwargs).
    """
    config = ensure_config(config)
    if _is_tool_call(value):
        tool_call_id: Optional[str] = cast("ToolCall", value)["id"]
        tool_input: Union[str, dict] = cast("ToolCall", value)["args"].copy()
    else:
        tool_call_id = None
        tool_input = cast("Union[str, dict]", value)
    return (
        tool_input,
        dict(
            callbacks=config.get("callbacks"),
            tags=config.get("tags"),
            metadata=config.get("metadata"),
            run_name=config.get("run_name"),
            run_id=config.pop("run_id", None),
            config=config,
            tool_call_id=tool_call_id,
            **kwargs,
        ),
    )


def _format_output(
    content: Any,
    artifact: Any,
    tool_call_id: Optional[str],
    name: str,
    status: str,
) -> Union[ToolOutputMixin, Any]:
    """Format tool output as a ToolMessage if appropriate.

    Args:
        content: The main content of the tool output.
        artifact: Any artifact data from the tool.
        tool_call_id: The ID of the tool call.
        name: The name of the tool.
        status: The execution status.

    Returns:
        The formatted output, either as a ToolMessage or the original content.
    """
    if isinstance(content, ToolOutputMixin) or tool_call_id is None:
        return content
    if not _is_message_content_type(content):
        content = _stringify(content)
    return ToolMessage(
        content,
        artifact=artifact,
        tool_call_id=tool_call_id,
        name=name,
        status=status,
    )


def _is_message_content_type(obj: Any) -> bool:
    """Check if object is valid message content format.

    Validates content for OpenAI or Anthropic format tool messages.

    Args:
        obj: The object to check.

    Returns:
        True if the object is valid message content, False otherwise.
    """
    return isinstance(obj, str) or (
        isinstance(obj, list) and all(_is_message_content_block(e) for e in obj)
    )


def _is_message_content_block(obj: Any) -> bool:
    """Check if object is a valid message content block.

    Validates content blocks for OpenAI or Anthropic format.

    Args:
        obj: The object to check.

    Returns:
        True if the object is a valid content block, False otherwise.
    """
    if isinstance(obj, str):
        return True
    if isinstance(obj, dict):
        return obj.get("type", None) in TOOL_MESSAGE_BLOCK_TYPES
    return False


def _stringify(content: Any) -> str:
    """Convert content to string, preferring JSON format.

    Args:
        content: The content to stringify.

    Returns:
        String representation of the content.
    """
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _get_type_hints(func: Callable) -> Optional[dict[str, type]]:
    """Get type hints from a function, handling partial functions.

    Args:
        func: The function to get type hints from.

    Returns:
        Dictionary of type hints, or None if extraction fails.
    """
    if isinstance(func, functools.partial):
        func = func.func
    try:
        return get_type_hints(func)
    except Exception:
        return None


def _get_runnable_config_param(func: Callable) -> Optional[str]:
    """Find the parameter name for RunnableConfig in a function.

    Args:
        func: The function to check.

    Returns:
        The parameter name for RunnableConfig, or None if not found.
    """
    type_hints = _get_type_hints(func)
    if not type_hints:
        return None
    for name, type_ in type_hints.items():
        if type_ is RunnableConfig:
            return name
    return None


class InjectedToolArg:
    """Annotation for tool arguments that are injected at runtime.

    Tool arguments annotated with this class are not included in the tool
    schema sent to language models and are instead injected during execution.
    """


class InjectedToolCallId(InjectedToolArg):
    """Annotation for injecting the tool call ID.

    This annotation is used to mark a tool parameter that should receive
    the tool call ID at runtime.

    .. code-block:: python

        from typing_extensions import Annotated
        from langchain_core.messages import ToolMessage
        from langchain_core.tools import tool, InjectedToolCallId

        @tool
        def foo(
            x: int, tool_call_id: Annotated[str, InjectedToolCallId]
        ) -> ToolMessage:
            \"\"\"Return x.\"\"\"
            return ToolMessage(
                str(x),
                artifact=x,
                name="foo",
                tool_call_id=tool_call_id
            )

    """


def _is_injected_arg_type(
    type_: type, injected_type: Optional[type[InjectedToolArg]] = None
) -> bool:
    """Check if a type annotation indicates an injected argument.

    Args:
        type_: The type annotation to check.
        injected_type: The specific injected type to check for.

    Returns:
        True if the type is an injected argument, False otherwise.
    """
    injected_type = injected_type or InjectedToolArg
    return any(
        isinstance(arg, injected_type)
        or (isinstance(arg, type) and issubclass(arg, injected_type))
        for arg in get_args(type_)[1:]
    )


def get_all_basemodel_annotations(
    cls: Union[TypeBaseModel, Any], *, default_to_bound: bool = True
) -> dict[str, type]:
    """Get all annotations from a Pydantic BaseModel and its parents.

    Args:
        cls: The Pydantic BaseModel class.
        default_to_bound: Whether to default to the bound of a TypeVar if it exists.
    """
    # cls has no subscript: cls = FooBar
    if isinstance(cls, type):
        # Gather pydantic field objects (v2: model_fields / v1: __fields__)
        fields = getattr(cls, "model_fields", {}) or getattr(cls, "__fields__", {})
        alias_map = {field.alias: name for name, field in fields.items() if field.alias}

        annotations: dict[str, type] = {}
        for name, param in inspect.signature(cls).parameters.items():
            # Exclude hidden init args added by pydantic Config. For example if
            # BaseModel(extra="allow") then "extra_data" will part of init sig.
            if fields and name not in fields and name not in alias_map:
                continue
            field_name = alias_map.get(name, name)
            annotations[field_name] = param.annotation
        orig_bases: tuple = getattr(cls, "__orig_bases__", ())
    # cls has subscript: cls = FooBar[int]
    else:
        annotations = get_all_basemodel_annotations(
            get_origin(cls), default_to_bound=False
        )
        orig_bases = (cls,)

    # Pydantic v2 automatically resolves inherited generics, Pydantic v1 does not.
    if not (isinstance(cls, type) and is_pydantic_v2_subclass(cls)):
        # if cls = FooBar inherits from Baz[str], orig_bases will contain Baz[str]
        # if cls = FooBar inherits from Baz, orig_bases will contain Baz
        # if cls = FooBar[int], orig_bases will contain FooBar[int]
        for parent in orig_bases:
            # if class = FooBar inherits from Baz, parent = Baz
            if isinstance(parent, type) and is_pydantic_v1_subclass(parent):
                annotations.update(
                    get_all_basemodel_annotations(parent, default_to_bound=False)
                )
                continue

            parent_origin = get_origin(parent)

            # if class = FooBar inherits from non-pydantic class
            if not parent_origin:
                continue

            # if class = FooBar inherits from Baz[str]:
            # parent = Baz[str],
            # parent_origin = Baz,
            # generic_type_vars = (type vars in Baz)
            # generic_map = {type var in Baz: str}
            generic_type_vars: tuple = getattr(parent_origin, "__parameters__", ())
            generic_map = dict(zip(generic_type_vars, get_args(parent)))
            for field in getattr(parent_origin, "__annotations__", {}):
                annotations[field] = _replace_type_vars(
                    annotations[field], generic_map, default_to_bound=default_to_bound
                )

    return {
        k: _replace_type_vars(v, default_to_bound=default_to_bound)
        for k, v in annotations.items()
    }


def _replace_type_vars(
    type_: type,
    generic_map: Optional[dict[TypeVar, type]] = None,
    *,
    default_to_bound: bool = True,
) -> type:
    """Replace TypeVars in a type annotation with concrete types.

    Args:
        type_: The type annotation to process.
        generic_map: Mapping of TypeVars to concrete types.
        default_to_bound: Whether to use TypeVar bounds as defaults.

    Returns:
        The type with TypeVars replaced.
    """
    generic_map = generic_map or {}
    if isinstance(type_, TypeVar):
        if type_ in generic_map:
            return generic_map[type_]
        if default_to_bound:
            return type_.__bound__ or Any
        return type_
    if (origin := get_origin(type_)) and (args := get_args(type_)):
        new_args = tuple(
            _replace_type_vars(arg, generic_map, default_to_bound=default_to_bound)
            for arg in args
        )
        return _py_38_safe_origin(origin)[new_args]  # type: ignore[index]
    return type_


class BaseToolkit(BaseModel, ABC):
    """Base class for toolkits containing related tools.

    A toolkit is a collection of related tools that can be used together
    to accomplish a specific task or work with a particular system.
    """

    @abstractmethod
    def get_tools(self) -> list[BaseTool]:
        """Get all tools in the toolkit.

        Returns:
            List of tools contained in this toolkit.
        """
