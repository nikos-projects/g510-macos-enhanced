"""Re-export a shared root module so the Linux copy can amend it.

Two of the root modules are portable apart from a handful of macOS-shaped
values: config.py's DEFAULTS name Safari and cmd-chords, and control.py's
iso_swap shells out to hidutil. Forking either would duplicate logic that has
tests against it and would drift.

So the Linux modules import the root one by path under a private name, pull
its contents into their own namespace, and change only what is wrong. The
functions keep referring to the root module's globals, which is what makes
amending a value like DEFAULTS in place actually take effect.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    """The root module `name`, imported under a private key."""
    key = "g510_root_" + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, os.path.join(ROOT, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def reexport(module, namespace):
    """Copy a module's public names into `namespace`."""
    for key, value in vars(module).items():
        if not key.startswith("__"):
            namespace[key] = value


def adopt(module, namespace):
    """Push this module's overrides onto the root module and become it.

    reexport() alone leaves a trap. The root module's own functions look their
    globals up in the root module, so amending a value here in place works
    (DEFAULTS), but *rebinding* one does not: setting CONFIG_PATH in the Linux
    namespace leaves root's load() still reading root's CONFIG_PATH. The
    failure is silent, and it bit a test run hard enough to write to the real
    ~/.config/g510.

    So copy every public name back onto the root module and then hand that
    module out under our own name. After this `import config` yields one
    module object with the Linux amendments already on it, which both removes
    the trap and makes the module patchable the ordinary way.
    """
    name = namespace["__name__"]
    for key, value in namespace.items():
        if not key.startswith("_"):
            setattr(module, key, value)
    sys.modules[name] = module
