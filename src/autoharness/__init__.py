"""Compatibility import path. New integrations should use kitharness."""
from kitharness._compat import install_legacy_package

install_legacy_package(__name__)
