"""Exceptions shared across api-qovrix."""


class QovrixError(Exception):
    """Base error. The message is shown to the user as-is."""


class ConfigError(QovrixError):
    """The project or a monitor file is invalid."""


class TemplateError(QovrixError):
    """A `{{ ... }}` expression could not be resolved."""


class SecretMissingError(TemplateError):
    """A secret is referenced but its environment variable is not set."""
