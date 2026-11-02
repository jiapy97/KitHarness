"""Compatibility for existing local integrations; new entry points use KitHarness."""
import os

LEGACY_PACKAGE = "autoharness"
ENV_PREFIX = "KITHARNESS_"
LEGACY_ENV_PREFIX = LEGACY_PACKAGE.upper() + "_"


def env_get(name, default=None):
    """Read the current setting first, including an explicitly empty value."""
    if name in os.environ:
        return os.environ[name]
    if name.startswith(ENV_PREFIX):
        return os.environ.get(LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):], default)
    return default


def export_env(environ):
    """Mirror settings for older child processes and custom notification scripts."""
    result = dict(environ)
    for name, value in list(result.items()):
        if name.startswith(ENV_PREFIX):
            result[LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):]] = value
    return result


def canonical_module(name):
    if name == LEGACY_PACKAGE or name.startswith(LEGACY_PACKAGE + "."):
        return "kitharness" + name[len(LEGACY_PACKAGE):]
    return name


def install_legacy_package(name):
    """Alias old imports without creating separate configuration or lock modules.

    The loader also supplies code for ``python -m`` entry points. Normal imports
    return the canonical module and retain its spec so reload still works.
    """
    import importlib
    import importlib.abc
    import importlib.util
    import sys

    class AliasLoader(importlib.abc.Loader):
        def __init__(self, target):
            self.target = target

        def create_module(self, spec):
            module = importlib.import_module(self.target)
            self.original_spec = module.__spec__
            return module

        def exec_module(self, module):
            module.__spec__ = self.original_spec

        def get_code(self, fullname):
            spec = importlib.util.find_spec(self.target)
            return spec.loader.get_code(self.target)

    class AliasFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if not fullname.startswith(name + "."):
                return None
            canonical = canonical_module(fullname)
            spec = importlib.util.find_spec(canonical)
            if spec is None:
                return None
            return importlib.util.spec_from_loader(
                fullname, AliasLoader(canonical), is_package=spec.submodule_search_locations is not None,
            )

    sys.meta_path.insert(0, AliasFinder())
    sys.modules[name] = importlib.import_module("kitharness")
