import dataclasses
import pickle
import sys
import types
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Protocol, Union

from typing_extensions import TypeAlias

if sys.version_info >= (3, 11):
    from torch._C._dynamo import eval_frame

    DynamoFrameType: TypeAlias = eval_frame._PyInterpreterFrame
else:
    DynamoFrameType: TypeAlias = types.FrameType

import torch

# This class has a `check_fn` field for the guard,
#  and a `code` field for the code object.
CacheEntry = torch._C._dynamo.eval_frame._CacheEntry

# We use a dict to store additional data per frame.
FrameState = Dict[Any, Any]


class GuardFail(NamedTuple):
    # A string repr of the piece of failed guard code we eval-ed
    reason: str
    # A code object where we failed a guard
    orig_code: types.CodeType


class GuardFn(Protocol):
    closure_vars: Dict[str, object]
    args: List[str]
    code_parts: List[str]
    verbose_code_parts: List[str]
    global_scope: Dict[str, object]
    guard_fail_fn: Optional[Callable[[GuardFail], None]]

    # maps locals of user function to bool
    def __call__(self, f_locals: Dict[str, object]) -> bool:
        ...


@dataclasses.dataclass
class GuardedCode:
    code: types.CodeType
    check_fn: GuardFn
    interpreter_agnostic_check_fn: GuardFn
    frame: types.FrameType
    name: str
    compiled_fn: Any
    global_alias_table: Dict[str, str]
    instructions: List[Any]
    resume_fn_name: str
    resume_fn_code: types.CodeType
    unique_id: int

    def serialize(self, file):
        code_attrs = torch._dynamo.utils.attrs_code_object(self.code)
        if self.resume_fn_code:
            resume_fn_code_attrs = torch._dynamo.utils.attrs_code_object(self.resume_fn_code)
        else:
            resume_fn_code_attrs = None

        guard_py_code = self.interpreter_agnostic_check_fn.pycode
        # annoying
        unique_id = torch._dynamo.bytecode_transformation._unique_id_counter
        guarded_code_struct = (
            code_attrs,
            guard_py_code,
            self.name,
            self.compiled_fn,
            self.global_alias_table,
            self.instructions,
            self.resume_fn_name,
            resume_fn_code_attrs,
            unique_id
        )
        pickle.dump(guarded_code_struct, file)

    @staticmethod
    def deserialize(file, frame):
        try:
            serialized = file.read()
            (
                attributes,
                guard_code,
                fn_name,
                compiled_fn,
                global_alias_table,
                instructions,
                resume_fn_name,
                resume_fn_code_attrs,
                unique_id_serialized,
            ) = pickle.loads(serialized)

            if next(torch._dynamo.bytecode_transformation._unique_id_counter) > next(unique_id_serialized):
                unique_id = torch._dynamo.bytecode_transformation._unique_id_counter
            else:
                unique_id = unique_id_serialized
            torch._dynamo.bytecode_transformation._unique_id_counter = unique_id
            frame.f_globals[fn_name] = compiled_fn

            for alias, name in global_alias_table.items():
                frame.f_globals[alias] = eval(name, frame.f_globals)

            check_fn = torch._dynamo.guards.CheckFunctionManager.guard_fn_from_pycode(
                guard_code, frame.f_globals
            )
            # cg = PyCodegen()
            # torch._dynamo.bytecode_transformations.clean_and_assemble_instructions(instructions, keys, code_options)[1]
            code_obj = types.CodeType(*attributes)
            if resume_fn_code_attrs:
                resume_fn_code_obj = types.CodeType(*resume_fn_code_attrs)
                frame.f_globals[resume_fn_name] = types.FunctionType(resume_fn_code_obj, frame.f_globals, resume_fn_name)
            else:
                resume_fn_code_obj = None

            if check_fn(frame.f_locals):
                return GuardedCode(
                    code_obj,
                    check_fn,
                    check_fn,
                    frame,
                    fn_name,
                    compiled_fn,
                    global_alias_table,
                    instructions,
                    resume_fn_name,
                    resume_fn_code_obj,
                    unique_id,
                )
            else:
                return None
        except Exception as e:
            return None


class DynamoCallbackFn(Protocol):
    def __call__(
        self,
        frame: DynamoFrameType,
        cache_entry: Optional[CacheEntry],  # type: ignore[valid-type]
        frame_state: FrameState,
    ) -> Optional[GuardedCode]:
        ...


DynamoCallback = Union[DynamoCallbackFn, None, bool]


class DynamoGuardHook(Protocol):
    def __call__(
        self,
        guard_fn: GuardFn,
        code: types.CodeType,
        f_locals: Dict[str, object],
        index: int,
        last: bool,
    ) -> None:
        ...


class ProfilerStartHook(Protocol):
    def __call__(
        self,
        name: str,
        # TODO(whc) how do I annotate a _RecordFunction here?
    ) -> Any:
        ...


class ProfilerEndHook(Protocol):
    def __call__(self, record: Any) -> None:
        ...


class BytecodeHook(Protocol):
    def __call__(
        self, code: types.CodeType, new_code: types.CodeType
    ) -> Optional[types.CodeType]:
        ...
