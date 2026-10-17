"""Loads a policy file under kernel-realism rules.  FROZEN.

Rules enforced statically (AST) so the policy can only use what the kernel could know:
  * imports limited to: math, heapq, bisect, collections, itertools, functools,
    dataclasses, typing, gpusim.api
  * no attribute names starting with '_' except on ``self`` (no peeking at simulator
    internals such as true job costs), no dunder access besides ``__init__``
  * no getattr/setattr/vars/eval/exec/open/id/hash/... (id/hash are also
    nondeterministic across runs)
  * attribute assignment only on ``self.…`` or on ``….ps.x`` / ``….client_ps.x``
    scratch namespaces (never on simulator objects)
"""

import ast
import builtins

ALLOWED_MODULES = {
    "math", "heapq", "bisect", "collections", "itertools", "functools",
    "dataclasses", "typing", "gpusim.api", "__future__",
}
FORBIDDEN_NAMES = {
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec",
    "compile", "open", "__import__", "input", "breakpoint", "id", "hash", "dir",
    "help", "exit", "quit", "memoryview", "object.__setattr__",
}
FORBIDDEN_ATTRS = {
    # str.format field syntax ("{0._cost}") would bypass the private-attribute check
    "format", "format_map",
    # frame / code introspection could reach simulator locals
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "f_back",
    "f_locals", "f_globals", "f_builtins", "f_code", "tb_frame", "tb_next", "mro",
}
SAFE_BUILTINS = [
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "range", "reversed", "round", "set", "frozenset", "sorted", "str", "sum", "tuple",
    "zip", "print", "object", "super", "staticmethod", "classmethod", "property",
    "divmod", "pow", "repr", "slice", "type",
    "Exception", "ValueError", "KeyError", "IndexError", "RuntimeError",
    "ZeroDivisionError", "AssertionError", "TypeError", "AttributeError",
    "StopIteration", "NotImplementedError", "NotImplemented", "Ellipsis",
    "True", "False", "None", "__build_class__",
]


class PolicyRuleError(Exception):
    pass


def _chain(node):
    """Names along an attribute chain, root first: a.b.c -> ['a','b','c']."""
    out = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        out.append(node.id)
    elif isinstance(node, ast.Call):
        out.append("<call>")
    else:
        out.append("<expr>")
    return out[::-1]


def check_source(src, path="policy.py"):
    tree = ast.parse(src, filename=path)
    errs = []

    def err(node, msg):
        errs.append(f"{path}:{getattr(node, 'lineno', '?')}: {msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_MODULES:
                    err(node, f"import of '{a.name}' not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in ALLOWED_MODULES or node.level:
                err(node, f"import from '{node.module}' not allowed")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES or (node.id.startswith("__") and node.id != "__name__"):
                err(node, f"use of '{node.id}' not allowed")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a in FORBIDDEN_ATTRS:
                err(node, f"attribute '{a}' not allowed")
            elif a.startswith("__"):
                if a != "__init__":
                    err(node, f"dunder attribute '{a}' not allowed")
            elif a.startswith("_"):
                if not (isinstance(node.value, ast.Name) and node.value.id == "self"):
                    err(node, f"private attribute '{a}' (simulator internals) not allowed")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            stack = list(targets)
            while stack:
                t = stack.pop()
                if isinstance(t, (ast.Tuple, ast.List)):
                    stack.extend(t.elts)
                    continue
                if isinstance(t, ast.Starred):
                    stack.append(t.value)
                    continue
                if not isinstance(t, ast.Attribute):
                    continue
                ch = _chain(t)
                parent = ch[-2] if len(ch) >= 2 else None
                if parent in ("ps", "client_ps"):
                    continue
                if ch[0] == "self" and "sched" not in ch[1:-1] and ch[1] != "sched":
                    continue
                err(t, f"assignment to '{'.'.join(ch)}' not allowed (only self.* or *.ps.* / *.client_ps.*)")
    if errs:
        raise PolicyRuleError("policy violates kernel-realism rules:\n  " + "\n  ".join(errs))
    return tree


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in ALLOWED_MODULES:
        raise ImportError(f"import of '{name}' not allowed in policies")
    return builtins.__import__(name, globals, locals, fromlist, level)


def load_policy(path, trusted=False):
    """Return a factory ``sched -> policy instance``."""
    with open(path) as f:
        src = f.read()
    if trusted:
        ns = {"__name__": "policy_trusted", "__file__": path}
        exec(compile(src, path, "exec"), ns)
    else:
        check_source(src, path)
        bi = {k: getattr(builtins, k) for k in SAFE_BUILTINS if hasattr(builtins, k)}
        bi["__import__"] = _guarded_import
        ns = {"__name__": "policy", "__builtins__": bi}
        exec(compile(src, path, "exec"), ns)
    cls = ns.get("Policy")
    if cls is None:
        raise PolicyRuleError(f"{path} defines no class Policy")
    return cls
