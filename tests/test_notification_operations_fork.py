"""Configured operations exercise real HTTP post, edit, thread and repost effects."""

from unittest.mock import patch

from sqlmodel import Session, select

from keep.api.models.db.incident_notification import IncidentNotificationBinding
from tests.test_incident_notifications_api_fork import NotificationApiCase


class NotificationOperationsTest(NotificationApiCase):
    def configure(self):
        self.bundle["transports"][1].update(adapter_ref="mattermost-api-v2", capabilities={
            "update": True, "thread": True, "actions": False, "receipts": True})
        self.bundle["dispatch"] = {"scan_interval_seconds": 1}
        self.bundle["notification_defaults"] = {
            "events": {"incident.created": {"do": ["post"]},
                       "incident.updated": {"do": ["none"]},
                       "incident.acknowledged": {"do": ["edit", "thread"]},
                       "incident.reminder": {"do": ["repost", "thread"]}},
            "lines": {"incident.acknowledged": "Taken by {{ event.actor }}",
                      "incident.reminder": "Still firing: {{ incident.name }}"},
            "stub": {"keep_card": True, "color": "#999999", "text": "Moved to {{ target.url }}"},
            "fallbacks": {"edit": "post", "thread": "none", "repost": "post"}}
        self.apply()

    def drain(self, seconds=0):
        for offset in range(4):
            self.wire_worker(seconds + offset).run_once()

    def binding(self):
        with Session(self.engine) as session:
            return session.exec(select(IncidentNotificationBinding).where(
                IncidentNotificationBinding.destination_id == "alpha-chat")).one()

    def emit(self, event_type, seconds=5):
        from keep.api.core.incident_notifications import record_event
        from keep.api.models.db.incident import Incident
        with Session(self.engine) as session:
            incident = session.get(Incident, self.incidents()[0].id)
            record_event(session, incident, event_type, self.now(seconds), actor="operator")
            session.commit()

    def test_ack_edits_card_and_posts_one_thread_without_replacing_root_binding(self):
        self.configure()
        self.correlate(self.event())
        self.drain()
        root = self.binding().external_id
        self.change("acknowledged", 5)
        self.drain(5)
        self.assertEqual(self.binding().external_id, root)
        replies = [post for post in self.chat.posts.values() if post.get("root_id") == root]
        self.assertEqual(len(replies), 1)
        self.assertIn("Taken by", replies[0]["props"]["attachments"][0]["text"])
        self.assertEqual(len(self.chat.posts), 2)

    def test_repost_confirms_new_card_before_stubbing_old_card_and_threads_on_new_root(self):
        self.configure()
        self.correlate(self.event())
        self.drain()
        previous = self.binding().external_id
        self.emit("incident.reminder")
        self.drain(5)
        current = self.binding().external_id
        self.assertNotEqual(current, previous)
        stub = self.chat.posts[previous]["props"]["attachments"][0]
        self.assertIn("Moved to", stub["text"])
        self.assertIn("/_redirect/pl/" + current, stub["text"])
        self.assertEqual(stub["color"], "#999999")
        replies = [post for post in self.chat.posts.values() if post.get("root_id") == current]
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(self.chat.posts), 3)
        writes = self.chat.events
        created = next(i for i, event in enumerate(writes) if event["method"] == "POST"
                       and event["body"].get("props", {}).get("keep_notification_id") == self.chat.posts[current]["props"]["keep_notification_id"])
        replaced = next(i for i, event in enumerate(writes) if event["method"] == "PUT" and event["body"].get("id") == previous)
        self.assertLess(created, replaced)

    def test_unknown_repost_keeps_old_card_and_blocks_dependent_effects(self):
        self.configure()
        self.correlate(self.event())
        self.drain()
        previous = self.binding().external_id
        self.chat.status = 503
        self.emit("incident.reminder")
        self.drain(5)
        self.assertTrue(self.binding().uncertain)
        self.assertEqual(self.binding().external_id, previous)
        self.assertNotIn("Moved to", self.chat.posts[previous]["props"]["attachments"][0]["text"])
        self.assertEqual(len(self.chat.posts), 2)
        self.assertEqual([post for post in self.chat.posts.values() if post.get("root_id")], [])

    def test_repeated_worker_scan_does_not_replay_confirmed_repost_or_thread(self):
        self.configure()
        self.correlate(self.event())
        self.drain()
        self.emit("incident.reminder")
        self.drain(5)
        before = len(self.chat.events)
        self.drain(10)
        self.assertEqual(len(self.chat.events), before)
