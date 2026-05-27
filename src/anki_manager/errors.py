class AnkiManagerError(Exception):
    """Base class for anki-manager errors."""


class LifecycleError(AnkiManagerError):
    """Raised when systemctl or the underlying container fails."""


class NotReadyError(AnkiManagerError):
    """Raised when AnkiConnect did not become ready within the timeout."""


class InvalidNoteError(AnkiManagerError):
    """Raised when the fields provided to add_note do not match the model's schema."""
