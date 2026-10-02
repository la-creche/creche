"""family.yaml schema v1, validation report and registry loader (docs/rework/contracts/01, 01b).

One file defines a family (invariant 16). This package parses that file, says
what is wrong with it in a report rather than an exception (invariant 19), and
answers what a change to it costs: a live update, or a new sandbox."""

from .classify import Diff, Direction, FieldChange, Landing, Step, SwitchMode, classify
from .grammar import (
    ALLOWED_ROOTS,
    PLATFORM_ALLOWLIST,
    PLATFORM_ROOT,
    SANDBOX_FLAVORS,
    SURVEY_TOOL,
    InstallSource,
    Kind,
    Mode,
    SandboxFlavor,
    SandboxTool,
    Verb,
    duration_s,
    memory_mb,
)
from .model import (
    DailyCall,
    FamilyFile,
    FileMount,
    ModelBlock,
    QuietBlock,
    SandboxBlock,
    ToolGrant,
)
from .parse import parse_family, parse_server
from .registry import Registry, load_registry, revision_of
from .report import FamilyState, Issue, Issues, Report, Severity
from .server import McpServerFile, ToolEntry
from .validate import CALL_SEPARATOR, HostFacts, Index, check_family

__all__ = [
    "ALLOWED_ROOTS",
    "CALL_SEPARATOR",
    "PLATFORM_ALLOWLIST",
    "PLATFORM_ROOT",
    "SANDBOX_FLAVORS",
    "SURVEY_TOOL",
    "DailyCall",
    "Diff",
    "Direction",
    "FamilyFile",
    "FamilyState",
    "FieldChange",
    "FileMount",
    "HostFacts",
    "Index",
    "InstallSource",
    "Issue",
    "Issues",
    "Kind",
    "Landing",
    "McpServerFile",
    "Mode",
    "ModelBlock",
    "QuietBlock",
    "Registry",
    "Report",
    "SandboxBlock",
    "SandboxFlavor",
    "SandboxTool",
    "Severity",
    "Step",
    "SwitchMode",
    "ToolEntry",
    "ToolGrant",
    "Verb",
    "check_family",
    "classify",
    "duration_s",
    "load_registry",
    "memory_mb",
    "parse_family",
    "parse_server",
    "revision_of",
]
