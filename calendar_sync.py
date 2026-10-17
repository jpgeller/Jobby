"""Push events from the database to the calendar through the action layer (spec Section 3).
The calendar event id is stored on the row, so a moved interview updates the same calendar event."""
import actions
import db


def event_description(row):
    lines = [f"{row['company']} - {row['role']}"]
    if row["next_step"]:
        lines.append(f"Next step: {row['next_step']}")
    if row["location_or_link"]:
        lines.append(f"Link: {row['location_or_link']}")
    lines.append("Added by Jobby")
    return "\n".join(lines)


def plan(conn):
    """What to send: [(event_id, action, kwargs)]. Main thread only."""
    work = []
    for row in db.dirty_events(conn):
        fields = {"title": row["title"], "start": row["start"], "end": row["end"] or row["start"],
                  "location_or_link": row["location_or_link"], "description": event_description(row)}
        if row["calendar_event_id"]:
            work.append((row["id"], "update_event", {"event_id": row["calendar_event_id"], **fields}))
        else:
            work.append((row["id"], "create_event", fields))
    return work


def execute(adapter, work):
    """Send the work to the calendar. Worker-thread safe. Returns [(event_id, action, result, log_entry)]."""
    return [(event_id, action, *actions.call(adapter, action, **kwargs)) for event_id, action, kwargs in work]


def save(conn, results):
    """Record results on the main thread. Failed events stay dirty and retry next cycle."""
    done = 0
    for event_id, action, result, log_entry in results:
        actions.record(conn, log_entry)
        if not actions.succeeded(log_entry):
            continue
        fields = {"calendar_dirty": 0}
        if action == "create_event":
            fields["calendar_event_id"] = result
        db.update_event(conn, event_id, **fields)
        done += 1
    conn.commit()
    return done


def sync_calendar(conn, adapter):
    return save(conn, execute(adapter, plan(conn)))
