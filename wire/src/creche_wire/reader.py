"""The one read of a wire text in Python (decision CH9-1).

`read` gives the value of a wire type for a JSON text, or it raises. A text
passes two checks, in this sequence:

1. `strictjson.loads` checks the text against the rules of strict JSON. It
   refuses, for example, a text over the cap, two equal keys in one object,
   a lone surrogate, `NaN` and an integer out of range.
2. The model reads the text in the strict mode of pydantic. A wire int then
   refuses "1", 1.0 and true, and a wire bool refuses 1.

Step 2 reads the bytes again, not the value of step 1. The strict mode of a
Python value refuses a JSON string for an enum or a date, and the strict mode
of a JSON text takes it. The JSON mode is the mode of the wire.
"""

from pydantic import BaseModel

from . import strictjson


def read[M: BaseModel](model: type[M], data: bytes, *, cap: int) -> M:
    """The value of `model` that the JSON text `data` holds.

    `cap` is the most bytes that a text of the caller's surface has. The
    caller gives it, because the cap is a rule of the surface, not of the type.

    Raises `strictjson.NotStrictJson` for a text that is not strict JSON, and
    `pydantic.ValidationError` for a strict text that is no value of `model`.
    """
    strictjson.loads(data, cap=cap)

    return model.model_validate_json(data, strict=True)
