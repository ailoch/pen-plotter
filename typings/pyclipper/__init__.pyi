#  Generated via `stubgen -m pyclipper._pyclipper`

from typing import Any, Callable
from _typeshed import Incomplete

Area: Callable[..., Any]
CleanPolygon: Callable[..., Any]
CleanPolygons: Callable[..., Any]
ClosedPathsFromPolyTree: Callable[..., Any]
MinkowskiDiff: Callable[..., Any]
MinkowskiSum: Callable[..., Any]
MinkowskiSum2: Callable[..., Any]
OpenPathsFromPolyTree: Callable[..., Any]
Orientation: Callable[..., Any]
PointInPolygon: Callable[..., Any]
PolyTreeToPaths: Callable[..., Any]
ReversePath: Callable[..., Any]
ReversePaths: Callable[..., Any]
SimplifyPolygon: Callable[..., Any]
SimplifyPolygons: Callable[..., Any]
log_action: Callable[..., Any]
scale_from_clipper: Callable[..., Any]
scale_to_clipper: Callable[..., Any]

SILENT: bool

CT_INTERSECTION: int
CT_UNION: int
CT_DIFFERENCE: int
CT_XOR: int

PT_SUBJECT: int
PT_CLIP: int

PFT_EVENODD: int
PFT_NONZERO: int
PFT_POSITIVE: int
PFT_NEGATIVE: int

JT_SQUARE: int
JT_ROUND: int
JT_MITER: int

ET_CLOSEDPOLYGON: int
ET_CLOSEDLINE: int
ET_OPENBUTT: int
ET_OPENSQUARE: int
ET_OPENROUND: int

class ClipperException(Exception): ...

class PyPolyNode:
    def __init__(self, *args, **kwargs) -> None: ...

class Pyclipper:
    PreserveCollinear: Incomplete
    ReverseSolution: Incomplete
    StrictlySimple: Incomplete
    def __init__(self, *args, **kwargs) -> None: ...
    def AddPath(self, *args, **kwargs) -> Any: ...
    def AddPaths(self, *args, **kwargs) -> Any: ...
    def Clear(self, *args, **kwargs) -> Any: ...
    def Execute(self, *args, **kwargs) -> Any: ...
    def Execute2(self, *args, **kwargs) -> Any: ...
    def GetBounds(self, *args, **kwargs) -> Any: ...

class PyclipperOffset:
    ArcTolerance: Incomplete
    MiterLimit: Incomplete
    def __init__(self, *args, **kwargs) -> None: ...
    def AddPath(self, *args, **kwargs) -> Any: ...
    def AddPaths(self, *args, **kwargs) -> Any: ...
    def Clear(self, *args, **kwargs) -> Any: ...
    def Execute(self, *args, **kwargs) -> Any: ...
    def Execute2(self, *args, **kwargs) -> Any: ...
