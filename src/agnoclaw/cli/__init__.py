try:
    from .main import cli
except ImportError as e:
    raise ImportError(
        "CLI dependencies not installed. Install with: pip install agnoclaw[cli]"
    ) from e

__all__ = ["cli"]
