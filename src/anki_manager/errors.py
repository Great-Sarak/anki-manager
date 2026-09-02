class AnkiManagerError(Exception):
    """Base class for anki-manager errors."""


class LifecycleError(AnkiManagerError):
    """Raised when the lifecycle backend or underlying container fails."""


class NotReadyError(AnkiManagerError):
    """Raised when AnkiConnect did not become ready within the timeout."""


class InvalidNoteError(AnkiManagerError):
    """Raised when the fields provided to add_note do not match the model's schema."""


class NoteExistsError(AnkiManagerError):
    """Raised when add_note would create a note with a stable GUID that already exists."""


class NoteNotFoundError(AnkiManagerError):
    """Raised when update_note or find_by_guid cannot locate a note for the given GUID."""


class PermissionsHelperError(AnkiManagerError):
    """Raised when the privileged grant-deck helper invocation fails."""
