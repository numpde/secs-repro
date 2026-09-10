"""Identify deployment-input rejections whose explanations may reach an operator."""


class ConfigurationError(ValueError):
    """An owned configuration rule; its message must exclude credentials and input bytes."""
