import math
import unittest

from web_validation import (
    ValidationError,
    parse_library_sources,
    parse_list_query,
    safe_thumbnail_size,
    validate_config_payload,
)


class ConfigValidationTests(unittest.TestCase):
    def test_accepts_valid_partial_update(self):
        payload = {
            "match_mode": "hybrid",
            "embedding_provider_id": "embed-1",
            "embedding_fallback": False,
            "max_match_candidates": 25,
            "min_tag_score": 2.5,
            "auto_refresh": True,
            "thumbnail_size": 240,
        }

        self.assertEqual(
            validate_config_payload(payload, {"embed-1"}),
            payload,
        )

    def test_allows_empty_provider_for_automatic_selection(self):
        self.assertEqual(
            validate_config_payload({"embedding_provider_id": ""}, set()),
            {"embedding_provider_id": ""},
        )

    def test_rejects_unknown_provider_and_mode(self):
        with self.assertRaises(ValidationError):
            validate_config_payload({"embedding_provider_id": "missing"}, {"embed-1"})
        with self.assertRaises(ValidationError):
            validate_config_payload({"match_mode": "anything"}, set())

    def test_rejects_type_coercion_unknown_keys_and_bad_ranges(self):
        invalid_payloads = (
            {"embedding_fallback": 1},
            {"auto_refresh": "true"},
            {"max_match_candidates": True},
            {"max_match_candidates": 101},
            {"min_tag_score": math.inf},
            {"min_tag_score": -0.1},
            {"thumbnail_size": 49},
            {"unexpected": "value"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                validate_config_payload(payload, set())

    def test_rejects_non_object_body(self):
        for payload in (None, [], "{}"):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                validate_config_payload(payload, set())


class ListQueryValidationTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(parse_list_query({}).page, 1)
        self.assertEqual(parse_list_query({}).page_size, 50)
        self.assertEqual(parse_list_query({}).sort, "filename")

    def test_parses_supported_values(self):
        query = parse_list_query(
            {
                "page": "2",
                "page_size": "100",
                "q": "  funny cat  ",
                "tag": " cat ",
                "sort": "filename_desc",
            }
        )
        self.assertEqual(query.page, 2)
        self.assertEqual(query.page_size, 100)
        self.assertEqual(query.q, "funny cat")
        self.assertEqual(query.tag, "cat")
        self.assertEqual(query.sort, "filename_desc")

    def test_rejects_invalid_page_size_and_sort(self):
        for values in (
            {"page": "0"},
            {"page": "1.5"},
            {"page_size": "0"},
            {"page_size": "101"},
            {"sort": "random"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                parse_list_query(values)

    def test_bounds_query_text(self):
        with self.assertRaises(ValidationError):
            parse_list_query({"q": "x" * 201})
        with self.assertRaises(ValidationError):
            parse_list_query({"tag": "x" * 101})

    def test_thumbnail_size_falls_back_for_legacy_invalid_config(self):
        self.assertEqual(safe_thumbnail_size(200), 200)
        self.assertEqual(safe_thumbnail_size("200"), 200)
        self.assertEqual(safe_thumbnail_size(1000), 200)


class LibrarySourcesValidationTests(unittest.TestCase):
    def test_parses_json_and_directory_templates(self):
        sources = [
            {
                "__template_key": "json",
                "enabled": True,
                "namespace": "archive",
                "index_path": "/srv/memes/index.json",
                "data_root": "/srv/memes",
            },
            {
                "__template_key": "directory",
                "enabled": False,
                "namespace": "local-cats",
                "root": r"C:\memes\cats",
                "recursive": False,
                "tags": [" Cat ", "cat", "reaction"],
            },
        ]

        parsed = parse_library_sources(sources)

        self.assertEqual(parsed[0], sources[0])
        self.assertFalse(parsed[1]["enabled"])
        self.assertEqual(parsed[1]["tags"], ["Cat", "reaction"])

    def test_config_payload_normalises_library_sources(self):
        payload = {
            "library_sources": [
                {
                    "__template_key": "directory",
                    "namespace": "reactions",
                    "root": "/srv/reactions",
                }
            ]
        }

        validated = validate_config_payload(payload, set())

        self.assertTrue(validated["library_sources"][0]["enabled"])
        self.assertTrue(validated["library_sources"][0]["recursive"])
        self.assertEqual(validated["library_sources"][0]["tags"], [])

    def test_rejects_too_many_sources_and_non_list(self):
        source = {
            "__template_key": "directory",
            "namespace": "source",
            "root": "/srv/source",
        }
        with self.assertRaises(ValidationError):
            parse_library_sources(None)
        with self.assertRaises(ValidationError):
            parse_library_sources([source] * 33)

    def test_rejects_unsafe_or_ambiguous_sources(self):
        invalid_sources = (
            [
                {
                    "__template_key": "directory",
                    "namespace": "relative",
                    "root": "../memes",
                }
            ],
            [
                {
                    "__template_key": "directory",
                    "namespace": "managed",
                    "root": "/srv/memes",
                }
            ],
            [
                {
                    "__template_key": "directory",
                    "namespace": "one",
                    "root": "/srv/one",
                    "unexpected": True,
                }
            ],
            [
                {
                    "__template_key": "json",
                    "namespace": "index",
                    "index_path": "/srv/index.json",
                    "data_root": "/srv/../private",
                }
            ],
            [
                {
                    "__template_key": "directory",
                    "namespace": "duplicate",
                    "root": "/srv/one",
                },
                {
                    "__template_key": "directory",
                    "namespace": "DUPLICATE",
                    "root": "/srv/two",
                },
            ],
        )
        for sources in invalid_sources:
            with self.subTest(sources=sources), self.assertRaises(ValidationError):
                parse_library_sources(sources)


if __name__ == "__main__":
    unittest.main()
