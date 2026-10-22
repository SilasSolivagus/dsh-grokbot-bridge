"""Tests for bridge state persistence, card mapping, and dedupe."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bridge.state import BridgeState, StateError

KEY_1 = "dsh-grokbot-v1:upd-001"
KEY_2 = "dsh-grokbot-v1:upd-002"

FULL_CARD = {
    "issue": 12,
    "run_uuid": "run-1",
    "last_comment_id": 34,
    "last_status": "in_progress",
}


class StateTestCase(unittest.TestCase):
    """Base class with an isolated temporary state directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bridge-state-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "state.json")

    def write_raw(self, text, path=None):
        target = path or self.path
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
        return target

    def write_state(self, data, path=None):
        return self.write_raw(json.dumps(data), path=path)

    def read_raw(self, path=None):
        with open(path or self.path, "r", encoding="utf-8") as handle:
            return handle.read()

    def entries(self, directory=None):
        return sorted(os.listdir(directory or self.dir))


class MissingFileTests(StateTestCase):
    def test_load_missing_file_returns_empty_state(self):
        state = BridgeState.load(self.path)

        self.assertEqual(state.cards, {})
        self.assertEqual(state.processed_messages, [])
        self.assertEqual(state.to_dict(), {"cards": {}, "processed_messages": []})

    def test_constructor_accepts_missing_file_as_empty(self):
        state = BridgeState(self.path)

        self.assertEqual(state.cards, {})

    def test_loading_a_missing_file_does_not_create_it(self):
        BridgeState.load(self.path)

        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.entries(), [])

    def test_path_like_object_is_accepted(self):
        state = BridgeState(Path(self.path))
        state.upsert_card("task-1", issue=1)
        state.save()

        self.assertTrue(Path(self.path).is_file())


class RoundTripTests(StateTestCase):
    def test_save_and_load_roundtrip(self):
        state = BridgeState(self.path)
        state.upsert_card("task-abc", **FULL_CARD)
        self.assertTrue(state.mark_processed(KEY_1))
        state.save()

        reloaded = BridgeState.load(self.path)

        self.assertEqual(reloaded.cards, {"task-abc": FULL_CARD})
        self.assertEqual(reloaded.processed_messages, [KEY_1])

    def test_empty_state_roundtrip(self):
        BridgeState(self.path).save()

        reloaded = BridgeState.load(self.path)

        self.assertEqual(reloaded.to_dict(), {"cards": {}, "processed_messages": []})

    def test_save_creates_missing_parent_directories(self):
        nested = os.path.join(self.dir, "a", "b", "state.json")
        state = BridgeState(nested)
        state.upsert_card("task-1", issue=7)

        state.save()

        self.assertTrue(os.path.isfile(nested))
        self.assertEqual(BridgeState.load(nested).cards, {"task-1": {"issue": 7}})

    def test_save_writes_both_top_level_keys(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=7)
        state.mark_processed(KEY_1)

        state.save()

        raw = json.loads(self.read_raw())
        self.assertEqual(set(raw), {"cards", "processed_messages"})
        self.assertEqual(raw["processed_messages"], [KEY_1])

    def test_saved_file_is_utf8_and_not_ascii_escaped(self):
        state = BridgeState(self.path)
        state.upsert_card("任务-1", issue=1)

        state.save()

        raw = self.read_raw()
        self.assertIn("任务-1", raw)
        self.assertNotIn("\\u4efb", raw)

    def test_second_save_replaces_previous_content(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=7)
        state.save()

        state.upsert_card("task-1", last_status="blocked")
        state.save()

        reloaded = BridgeState.load(self.path)
        self.assertEqual(
            reloaded.card_for_task("task-1"),
            {"issue": 7, "last_status": "blocked"},
        )

    def test_load_does_not_rewrite_the_file(self):
        raw = '{"cards":{},"processed_messages":[]}'
        self.write_raw(raw)

        BridgeState.load(self.path)

        self.assertEqual(self.read_raw(), raw)


class AtomicSaveTests(StateTestCase):
    def test_save_writes_temp_file_then_replaces_in_same_directory(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=7)
        seen = {}
        real_replace = os.replace

        def spy_replace(src, dst):
            seen["src"] = src
            seen["dst"] = dst
            seen["src_dir"] = os.path.dirname(os.path.abspath(src))
            seen["dst_dir"] = os.path.dirname(os.path.abspath(dst))
            with open(src, "r", encoding="utf-8") as handle:
                seen["payload"] = json.load(handle)
            seen["temp_existed"] = os.path.isfile(src)
            real_replace(src, dst)

        with mock.patch("bridge.state.os.replace", side_effect=spy_replace):
            state.save()

        self.assertTrue(seen["temp_existed"])
        self.assertEqual(seen["src_dir"], seen["dst_dir"])
        self.assertEqual(seen["dst_dir"], os.path.dirname(os.path.abspath(self.path)))
        self.assertNotEqual(
            os.path.abspath(seen["src"]), os.path.abspath(seen["dst"])
        )
        self.assertEqual(seen["payload"], state.to_dict())
        self.assertEqual(self.entries(), ["state.json"])

    def test_failed_replace_keeps_previous_file_and_cleans_temp_file(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=7, last_status="in_progress")
        state.save()
        original = self.read_raw()

        state.upsert_card("task-1", last_status="done")
        with mock.patch(
            "bridge.state.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(StateError) as caught:
                state.save()

        message = str(caught.exception)
        self.assertIn("save", message)
        self.assertIn("disk full", message)
        self.assertEqual(self.read_raw(), original)
        self.assertEqual(self.entries(), ["state.json"])
        self.assertEqual(
            BridgeState.load(self.path).card_for_task("task-1")["last_status"],
            "in_progress",
        )


class CorruptStateTests(StateTestCase):
    def test_invalid_json_raises_state_error(self):
        self.write_raw("{not valid json")

        with self.assertRaises(StateError) as caught:
            BridgeState.load(self.path)

        message = str(caught.exception)
        self.assertIn(self.path, message)
        self.assertIn("JSON", message)

    def test_corrupt_file_is_not_reset(self):
        corrupt = '{"cards": '
        self.write_raw(corrupt)

        with self.assertRaises(StateError):
            BridgeState.load(self.path)

        self.assertEqual(self.read_raw(), corrupt)
        with self.assertRaises(StateError):
            BridgeState.load(self.path)

    def test_empty_file_raises_state_error(self):
        self.write_raw("")

        with self.assertRaises(StateError):
            BridgeState.load(self.path)

    def test_non_utf8_file_raises_state_error(self):
        with open(self.path, "wb") as handle:
            handle.write(b"\xff\xfe\x00{")

        with self.assertRaises(StateError) as caught:
            BridgeState.load(self.path)

        self.assertIn(self.path, str(caught.exception))

    def test_json_array_is_not_valid_state(self):
        self.write_raw("[]")

        with self.assertRaises(StateError) as caught:
            BridgeState.load(self.path)

        self.assertIn("object", str(caught.exception))

    def test_missing_top_level_keys_raise_state_error(self):
        cases = [
            ("{}", ("cards", "processed_messages")),
            ('{"cards": {}}', ("processed_messages",)),
            ('{"processed_messages": []}', ("cards",)),
        ]

        for payload, missing in cases:
            with self.subTest(payload=payload):
                self.write_raw(payload)
                with self.assertRaises(StateError) as caught:
                    BridgeState.load(self.path)
                message = str(caught.exception)
                self.assertIn("missing", message)
                for key in missing:
                    self.assertIn(key, message)

    def test_unknown_top_level_key_raises_state_error(self):
        self.write_state(
            {"cards": {}, "processed_messages": [], "extra": 1}
        )

        with self.assertRaises(StateError) as caught:
            BridgeState.load(self.path)

        self.assertIn("extra", str(caught.exception))

    def test_cards_must_be_an_object(self):
        for cards in ([], "cards", 3, None):
            with self.subTest(cards=cards):
                self.write_state({"cards": cards, "processed_messages": []})
                with self.assertRaises(StateError) as caught:
                    BridgeState.load(self.path)
                self.assertIn("cards", str(caught.exception))

    def test_processed_messages_must_be_a_list(self):
        for processed in ({}, "messages", 3, None):
            with self.subTest(processed=processed):
                self.write_state({"cards": {}, "processed_messages": processed})
                with self.assertRaises(StateError) as caught:
                    BridgeState.load(self.path)
                self.assertIn("processed_messages", str(caught.exception))

    def test_card_entries_must_be_objects(self):
        for card in ([], "card", 3, None):
            with self.subTest(card=card):
                self.write_state(
                    {"cards": {"task-1": card}, "processed_messages": []}
                )
                with self.assertRaises(StateError) as caught:
                    BridgeState.load(self.path)
                self.assertIn("task-1", str(caught.exception))

    def test_unknown_card_field_raises_state_error(self):
        self.write_state(
            {
                "cards": {"task-1": {"issue": 1, "surprise": True}},
                "processed_messages": [],
            }
        )

        with self.assertRaises(StateError) as caught:
            BridgeState.load(self.path)

        self.assertIn("surprise", str(caught.exception))

    def test_bad_card_field_values_raise_state_error(self):
        cases = [
            ("issue", "12"),
            ("issue", 0),
            ("issue", -1),
            ("issue", True),
            ("issue", 1.5),
            ("run_uuid", 5),
            ("run_uuid", ""),
            ("run_uuid", "   "),
            ("run_uuid", " run-1 "),
            ("last_comment_id", "34"),
            ("last_comment_id", -1),
            ("last_comment_id", True),
            ("last_status", 5),
            ("last_status", ""),
            ("last_status", "   "),
        ]

        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.write_state(
                    {
                        "cards": {"task-1": {field: value}},
                        "processed_messages": [],
                    }
                )
                with self.assertRaises(StateError) as caught:
                    BridgeState.load(self.path)
                message = str(caught.exception)
                self.assertIn("task-1", message)
                self.assertIn(field, message)

    def test_processed_messages_items_must_be_non_empty_strings(self):
        for items in ([1], [""], ["   "], [None], ["ok", 2]):
            with self.subTest(items=items):
                self.write_state({"cards": {}, "processed_messages": items})
                with self.assertRaises(StateError) as caught:
                    BridgeState.load(self.path)
                self.assertIn("processed_messages", str(caught.exception))

    def test_duplicate_processed_message_keys_raise_state_error(self):
        self.write_state(
            {"cards": {}, "processed_messages": [KEY_1, KEY_1]}
        )

        with self.assertRaises(StateError) as caught:
            BridgeState.load(self.path)

        self.assertIn("duplicate", str(caught.exception))

    def test_task_ids_with_surrounding_whitespace_raise_state_error(self):
        self.write_state(
            {
                "cards": {" task-1 ": {"issue": 1}},
                "processed_messages": [],
            }
        )

        with self.assertRaises(StateError):
            BridgeState.load(self.path)


class MarkProcessedTests(StateTestCase):
    def test_first_mark_returns_true_and_records_key(self):
        state = BridgeState(self.path)

        self.assertTrue(state.mark_processed(KEY_1))

        self.assertEqual(state.processed_messages, [KEY_1])

    def test_repeated_mark_returns_false_without_duplicating(self):
        state = BridgeState(self.path)
        self.assertTrue(state.mark_processed(KEY_1))

        self.assertFalse(state.mark_processed(KEY_1))
        self.assertFalse(state.mark_processed(KEY_1))

        self.assertEqual(state.processed_messages, [KEY_1])

    def test_mark_processed_preserves_insertion_order(self):
        state = BridgeState(self.path)

        for key in (KEY_1, KEY_2):
            self.assertTrue(state.mark_processed(key))

        self.assertEqual(state.processed_messages, [KEY_1, KEY_2])

    def test_mark_processed_persists_across_save(self):
        state = BridgeState(self.path)
        state.mark_processed(KEY_1)
        state.mark_processed(KEY_2)
        state.save()

        reloaded = BridgeState.load(self.path)

        self.assertEqual(reloaded.processed_messages, [KEY_1, KEY_2])
        self.assertFalse(reloaded.mark_processed(KEY_1))

    def test_mark_processed_does_not_save_automatically(self):
        state = BridgeState(self.path)
        state.mark_processed(KEY_1)

        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(
            BridgeState.load(self.path).processed_messages, []
        )

    def test_values_are_trimmed(self):
        state = BridgeState(self.path)

        self.assertTrue(state.mark_processed("  {}  ".format(KEY_1)))

        self.assertEqual(state.processed_messages, [KEY_1])
        self.assertFalse(state.mark_processed(KEY_1))

    def test_invalid_keys_are_rejected(self):
        state = BridgeState(self.path)

        for key in ("", "   ", None, 123, [KEY_1]):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    state.mark_processed(key)
                self.assertIn("key", str(caught.exception))
        self.assertEqual(state.processed_messages, [])

    def test_processed_messages_property_returns_a_copy(self):
        state = BridgeState(self.path)
        state.mark_processed(KEY_1)

        messages = state.processed_messages
        messages.append(KEY_2)

        self.assertEqual(state.processed_messages, [KEY_1])


class CardTests(StateTestCase):
    def test_card_for_task_returns_none_when_missing(self):
        state = BridgeState(self.path)

        self.assertIsNone(state.card_for_task("task-unknown"))

    def test_upsert_card_creates_card(self):
        state = BridgeState(self.path)

        card = state.upsert_card("task-1", issue=12, run_uuid="run-1")

        self.assertEqual(card, {"issue": 12, "run_uuid": "run-1"})
        self.assertEqual(
            state.card_for_task("task-1"),
            {"issue": 12, "run_uuid": "run-1"},
        )

    def test_upsert_card_merges_fields(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=12, run_uuid="run-1")
        state.upsert_card("task-1", last_comment_id=0)
        state.upsert_card("task-1", last_comment_id=34, last_status="in_progress")

        self.assertEqual(state.card_for_task("task-1"), FULL_CARD)

    def test_multiple_cards_are_independent(self):
        state = BridgeState(self.path)
        state.upsert_card("task-a", issue=1)
        state.upsert_card("task-b", issue=2, last_status="blocked")

        state.upsert_card("task-a", last_status="in_progress")

        self.assertEqual(
            state.card_for_task("task-a"),
            {"issue": 1, "last_status": "in_progress"},
        )
        self.assertEqual(
            state.card_for_task("task-b"),
            {"issue": 2, "last_status": "blocked"},
        )

    def test_upsert_card_returns_a_copy(self):
        state = BridgeState(self.path)
        card = state.upsert_card("task-1", issue=1)

        card["issue"] = 99

        self.assertEqual(state.card_for_task("task-1")["issue"], 1)

    def test_card_for_task_returns_a_copy(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=1)

        card = state.card_for_task("task-1")
        card["issue"] = 99

        self.assertEqual(state.card_for_task("task-1")["issue"], 1)

    def test_cards_property_returns_a_copy(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=1)

        cards = state.cards
        cards.clear()

        self.assertEqual(state.cards, {"task-1": {"issue": 1}})

    def test_upsert_card_rejects_unknown_field(self):
        state = BridgeState(self.path)

        with self.assertRaises(ValueError) as caught:
            state.upsert_card("task-1", card="TEST-004")

        self.assertIn("card", str(caught.exception))
        self.assertEqual(state.cards, {})

    def test_upsert_card_rejects_invalid_values(self):
        cases = [
            ("issue", "12"),
            ("issue", 0),
            ("issue", -1),
            ("issue", True),
            ("issue", 1.5),
            ("run_uuid", 5),
            ("run_uuid", ""),
            ("run_uuid", "   "),
            ("last_comment_id", "34"),
            ("last_comment_id", -1),
            ("last_comment_id", True),
            ("last_status", 5),
            ("last_status", ""),
            ("last_status", "   "),
        ]

        for field, value in cases:
            with self.subTest(field=field, value=value):
                state = BridgeState(self.path)
                with self.assertRaises(ValueError) as caught:
                    state.upsert_card("task-1", **{field: value})
                self.assertIn(field, str(caught.exception))
                self.assertEqual(state.cards, {})

    def test_upsert_card_rejects_invalid_task_id(self):
        state = BridgeState(self.path)

        for task_id in ("", "   ", None, 123):
            with self.subTest(task_id=task_id):
                with self.assertRaises(ValueError) as caught:
                    state.upsert_card(task_id, issue=1)
                self.assertIn("task_id", str(caught.exception))
        self.assertEqual(state.cards, {})

    def test_card_for_task_rejects_invalid_task_id(self):
        state = BridgeState(self.path)

        for task_id in ("", "   ", None, 123):
            with self.subTest(task_id=task_id):
                with self.assertRaises(ValueError) as caught:
                    state.card_for_task(task_id)
                self.assertIn("task_id", str(caught.exception))

    def test_task_ids_are_trimmed(self):
        state = BridgeState(self.path)

        state.upsert_card("  task-1  ", issue=1)

        self.assertEqual(state.card_for_task("task-1"), {"issue": 1})
        self.assertEqual(state.cards, {"task-1": {"issue": 1}})

    def test_upsert_card_persists_across_save(self):
        state = BridgeState(self.path)
        state.upsert_card("task-1", issue=12, run_uuid="run-1")
        state.save()

        reloaded = BridgeState.load(self.path)

        self.assertEqual(
            reloaded.card_for_task("task-1"),
            {"issue": 12, "run_uuid": "run-1"},
        )


if __name__ == "__main__":
    unittest.main()
