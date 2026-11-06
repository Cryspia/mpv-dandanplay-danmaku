import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


HELPER = Path(__file__).parents[1] / "scripts" / "dandanplay" / "danmaku_helper.py"
SPEC = importlib.util.spec_from_file_location("danmaku_helper", HELPER)
dm = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = dm
SPEC.loader.exec_module(dm)


def anime(anime_id, title, start, end):
    return {
        "animeId": anime_id,
        "animeTitle": title,
        "type": "tvseries",
        "episodes": [
            {
                "episodeId": anime_id * 1000 + number,
                "episodeTitle": f"第{number}话",
            }
            for number in range(start, end + 1)
        ],
    }


class EpisodeSelectionTests(unittest.TestCase):
    def test_filtered_single_episode_requires_exact_number(self):
        filtered = anime(17431, "为美好的世界献上祝福！第三季", 3, 3)

        self.assertIsNone(dm._pick_episode(filtered, 1))
        self.assertIsNone(dm._pick_episode(filtered, 4))
        self.assertEqual(dm._pick_episode(filtered, 3)["episodeId"], 17431003)

    def test_complete_later_season_supports_relative_and_absolute_numbers(self):
        second_season = anime(18886, "葬送的芙莉莲 第二季", 29, 38)

        self.assertEqual(dm._pick_episode(second_season, 1)["episodeId"], 18886029)
        self.assertEqual(dm._pick_episode(second_season, 3)["episodeId"], 18886031)
        self.assertEqual(dm._pick_episode(second_season, 30)["episodeId"], 18886030)


class QueryLadderTests(unittest.TestCase):
    def test_filtered_season_query_does_not_stop_relaxed_search(self):
        filtered = anime(17431, "为美好的世界献上祝福！第三季", 3, 3)
        complete = anime(17431, "为美好的世界献上祝福！第三季", 1, 11)
        calls = []

        def search(query):
            calls.append(query)
            if query.endswith("3"):
                return [filtered]
            return [complete]

        with mock.patch.object(dm, "ddp_search_episodes", side_effect=search):
            episode_id = dm.resolve_match(
                "为美好的世界献上祝福！", 3, 4, kind="tv"
            )

        self.assertEqual(episode_id, 17431004)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith("3"))
        self.assertEqual(calls[1], "为美好的世界献上祝福！")

    def test_filtered_episode_three_never_becomes_episode_one(self):
        filtered = anime(17431, "为美好的世界献上祝福！第三季", 3, 3)
        complete = anime(17431, "为美好的世界献上祝福！第三季", 1, 11)

        with mock.patch.object(
            dm, "ddp_search_episodes", side_effect=[[filtered], [complete]]
        ):
            episode_id = dm.resolve_match(
                "为美好的世界献上祝福！", 3, 1, kind="tv"
            )

        self.assertEqual(episode_id, 17431001)


if __name__ == "__main__":
    unittest.main()
