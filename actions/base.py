"""ActionAdapter: the things Jobby may do in the world (spec Section 9)."""


class ActionAdapter:
    backend = "base"

    def create_event(self, title, start, end, location_or_link, description) -> str:
        """Returns the calendar event id."""
        raise NotImplementedError

    def update_event(self, event_id, **changes) -> None:
        raise NotImplementedError

    def delete_event(self, event_id) -> None:
        """Only for events Jobby created (used by --demo-reset)."""
        raise NotImplementedError

    def send_email(self, to, subject, body, reply_to_thread_id=None) -> str:
        """Returns the sent message id. Only ever called for outreach approved in Telegram."""
        raise NotImplementedError

    def call_reminder(self, summary: str, context: str | None = None) -> bool:
        """True if a call was placed."""
        raise NotImplementedError
