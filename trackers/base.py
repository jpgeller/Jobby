"""TrackerAdapter: mirrors the applications table somewhere the user can see it (spec Section 9).
The database stays the source of truth; trackers only receive copies."""


class TrackerAdapter:
    def upsert_application(self, app: dict) -> str:
        """Create or update one row. Returns tracker_row_id."""
        raise NotImplementedError

    def delete_application(self, tracker_row_id: str) -> None:
        raise NotImplementedError

    def list_rows(self) -> list[dict]:
        raise NotImplementedError

    def sync(self, apps: list[dict]) -> dict[int, str]:
        """Mirror many applications at once. Returns {application id: tracker_row_id}.
        Backends may override this with a batched version."""
        return {app["id"]: self.upsert_application(app) for app in apps}
