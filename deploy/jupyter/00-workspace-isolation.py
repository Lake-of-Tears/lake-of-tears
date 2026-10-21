import builtins
import os
import pathlib

# ── Lake of Tears Workspace Isolation & Scratch CWD Redirection ──────────────
# 1. When a kernel starts, identify the active workspace from the notebook path.
# 2. Redirect the kernel's Current Working Directory (CWD) to an ephemeral scratch
#    directory: /tmp/scratch/<workspace_slug>/
# 3. Guard the workspace source tree (/home/jovyan/workspaces) against direct file
#    creation or writes, preventing raw datasets from polluting notebook folders.

_WORKSPACES_ROOT = pathlib.Path("/home/jovyan/workspaces").resolve()

# Determine initial directory and workspace slug
try:
    _initial_cwd = pathlib.Path.cwd().resolve()
except Exception:
    _initial_cwd = _WORKSPACES_ROOT

_ws_slug = "default"
if _WORKSPACES_ROOT in _initial_cwd.parents or _initial_cwd == _WORKSPACES_ROOT:
    try:
        _rel = _initial_cwd.relative_to(_WORKSPACES_ROOT)
        if _rel.parts:
            _ws_slug = _rel.parts[0]
    except Exception:
        pass

# Configure ephemeral scratch directory
_scratch_dir = pathlib.Path(f"/tmp/scratch/{_ws_slug}")
try:
    _scratch_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(_scratch_dir)
except Exception as _e:
    pass

# Export standard environment variables for notebooks
os.environ["WORKSPACE_SLUG"] = _ws_slug
os.environ["LAKE_SCRATCH_DIR"] = str(_scratch_dir)
os.environ["LAKE_NOTEBOOK_DIR"] = str(_initial_cwd)

# ── Filesystem Write Guards for Workspace Source Directory ────────────────────
_orig_open = builtins.open
_orig_mkdir = os.mkdir
_orig_makedirs = os.makedirs


def _is_inside_workspace_tree(path_target) -> bool:
    try:
        p = pathlib.Path(path_target)
        if not p.is_absolute():
            p = (pathlib.Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        return _WORKSPACES_ROOT in p.parents or p == _WORKSPACES_ROOT
    except (ValueError, TypeError, OSError):
        return False


def _guarded_open(file, mode="r", *args, **kwargs):
    mode_str = str(mode)
    if any(m in mode_str for m in ("w", "a", "+", "x")):
        if _is_inside_workspace_tree(file):
            raise PermissionError(
                f"Lake of Tears Workspace Isolation: Writing data files directly to the workspace source tree "
                f"({file}) is forbidden. Datasets must be streamed to object storage (s3://datalake/...) "
                f"or written to ephemeral scratch ({_scratch_dir})."
            )
    return _orig_open(file, mode, *args, **kwargs)


def _guarded_mkdir(path, *args, **kwargs):
    if _is_inside_workspace_tree(path):
        raise PermissionError(
            f"Lake of Tears Workspace Isolation: Creating directories inside the workspace source tree "
            f"({path}) is forbidden. Ephemeral directories may be created in scratch ({_scratch_dir})."
        )
    return _orig_mkdir(path, *args, **kwargs)


def _guarded_makedirs(name, *args, **kwargs):
    if _is_inside_workspace_tree(name):
        raise PermissionError(
            f"Lake of Tears Workspace Isolation: Creating directories inside the workspace source tree "
            f"({name}) is forbidden. Ephemeral directories may be created in scratch ({_scratch_dir})."
        )
    return _orig_makedirs(name, *args, **kwargs)


builtins.open = _guarded_open
os.mkdir = _guarded_mkdir
os.makedirs = _guarded_makedirs
pathlib.Path.mkdir = lambda self, *args, **kwargs: _guarded_mkdir(self, *args, **kwargs)
