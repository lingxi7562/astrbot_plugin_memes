import unittest

from backend.routing import MemeRouter, RoutingSettings


def pack(**overrides):
    value = {
        "id": "fun",
        "label": "Fun",
        "namespaces": [],
        "include_tags": [],
        "exclude_tags": [],
        "personas": [],
        "weight": 1.0,
        "enabled": True,
    }
    value.update(overrides)
    return value


class RoutingTests(unittest.TestCase):
    def test_explicit_pack_filters_namespace_and_tags(self):
        settings = RoutingSettings.safe(
            [pack(namespaces=["managed"], include_tags=["reaction"])],
            default_pack="fun",
        )
        router = MemeRouter(settings)
        candidates = [
            {"id": "managed:a", "source": "managed", "tags": ["reaction"]},
            {"id": "managed:b", "source": "managed", "tags": ["cute"]},
            {"id": "other:c", "source": "other", "tags": ["reaction"]},
        ]

        selected, info = router.route(candidates, scope="s", pack="fun")

        self.assertEqual([item["id"] for item in selected], ["managed:a"])
        self.assertEqual(info["pack"], "fun")
        self.assertFalse(info["fallback"])

    def test_persona_mapping_and_empty_pack_fallback_are_safe(self):
        settings = RoutingSettings.safe(
            [pack(id="formal", personas=["严肃"], include_tags=["formal"])],
            persona_packs={"严肃": "formal"},
        )
        router = MemeRouter(settings)
        candidates = [{"id": "managed:a", "source": "managed", "tags": ["casual"]}]

        selected, info = router.route(candidates, scope="s", persona="严肃")

        self.assertEqual(selected, candidates)
        self.assertTrue(info["fallback"])
        self.assertEqual(info["reason"], "empty_pack")

    def test_pack_persona_alias_works_without_separate_mapping(self):
        settings = RoutingSettings.safe(
            [pack(id="formal", personas=["严肃"], include_tags=["formal"])]
        )
        router = MemeRouter(settings)
        candidates = [{"id": "formal:item", "source": "formal", "tags": ["formal"]}]

        selected, info = router.route(candidates, scope="s", persona="严肃")

        self.assertEqual(selected[0]["id"], "formal:item")
        self.assertEqual(info["pack"], "formal")

    def test_sticky_assignment_is_bounded_and_stable(self):
        settings = RoutingSettings.safe(
            [pack(id="a", weight=1), pack(id="b", weight=1)], sticky_sessions=True
        )
        router = MemeRouter(settings, max_scopes=2)
        candidates = [
            {"id": "a:item", "source": "a", "tags": []},
            {"id": "b:item", "source": "b", "tags": []},
        ]
        first, first_info = router.route(candidates, scope="scope-1")
        second, second_info = router.route(candidates, scope="scope-1")
        router.route(candidates, scope="scope-2")
        router.route(candidates, scope="scope-3")

        self.assertEqual(first_info["pack"], second_info["pack"])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertLessEqual(router.status()["assigned_sessions"], 2)

    def test_disabled_and_invalid_packs_fail_closed(self):
        settings = RoutingSettings.safe(
            [pack(id="disabled", enabled=False), {"id": "bad id"}],
            default_pack="disabled",
        )
        router = MemeRouter(settings)
        candidates = [{"id": "managed:a", "tags": []}]

        selected, info = router.route(candidates, scope="s")

        self.assertEqual(selected, candidates)
        self.assertEqual(info["reason"], "no_packs")


if __name__ == "__main__":
    unittest.main()
