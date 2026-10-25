"""Offline regression cases based on public HTML field shapes observed 2026-10-25."""

import copy
import json
import unittest

from scripts.update_profile_stats import (
    YOUTUBE_CHANNEL_ID,
    YOUTUBE_SOURCE,
    UNIVERSITY_SOURCE,
    collect_youtube,
    collect_university,
)


ABOUT = {
    "channelId": YOUTUBE_CHANNEL_ID,
    "canonicalChannelUrl": "http://www.youtube.com/@SECourses",
    "subscriberCountText": "53.9K subscribers",
    "videoCountText": "469 videos",
    "viewCountText": "5,400,914 views",
}


def youtube_html(about=None, channel=YOUTUBE_CHANNEL_ID, duplicate=None, assignment="var ytInitialData"):
    data = {
        "metadata": {"channelMetadataRenderer": {"title": "SECourses", "externalId": channel}},
        "contents": [{"gridChannelRenderer": {
            "channelId": "UCnQmOwdajFlx5UBlNtaAqyg",
            "subscriberCountText": {"simpleText": "216 subscribers"},
            "videoCountText": {"runs": [{"text": "18"}, {"text": " videos"}]},
        }}],
        "onResponseReceivedEndpoints": [{"showEngagementPanelEndpoint": {
            "engagementPanel": {"engagementPanelSectionListRenderer": {"content": {
                "sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": [
                    {"aboutChannelRenderer": {"metadata": {"aboutChannelViewModel": about or ABOUT}}}
                ]}}]}
            }}}
        }}],
    }
    if duplicate is not None:
        data["another"] = {"aboutChannelViewModel": duplicate}
    return "<html><script>" + assignment + " = " + json.dumps(data) + ";</script></html>"


UNIVERSITY_HTML = """
<h4>Doktor Öğretim Üyesi FURKAN GÖZÜKARA</h4>
<div class="small-box bg-success"><div class="inner"><h3>99</h3><p>BİLDİRİ</p></div></div>
<div class="small-box bg-info">
  <div class="inner"><h3>11</h3><p>MAKALE</p></div>
  <div class="icon"><i class="ion ion-stats-bars"></i></div>
  <a href="/profiles/278952/makaleler" class="small-box-footer">Detay</a>
</div>
<h3>Son 5 Makale</h3>
"""


def no_json(url):
    raise AssertionError("These collectors must not call JSON APIs")


class CollectorTests(unittest.TestCase):
    def test_youtube_selects_channel_about_not_related_channel(self):
        seen = []
        def fetch(url):
            seen.append(url)
            return youtube_html()
        result = collect_youtube(no_json, fetch)
        self.assertEqual(seen, [YOUTUBE_SOURCE])
        self.assertEqual(result["youtube_subscribers"]["value"], 53900)
        self.assertEqual(result["youtube_subscribers"]["display"], "53.9K")
        self.assertEqual(result["youtube_subscribers"]["precision"], "rounded")
        self.assertEqual(result["youtube_videos"]["value"], 469)
        self.assertEqual(result["youtube_views"]["value"], 5400914)

    def test_youtube_rejects_wrong_identity(self):
        cases = [youtube_html(channel="OTHER"), youtube_html(about={**ABOUT, "channelId": "OTHER"})]
        for page in cases:
            with self.subTest(page=page), self.assertRaises(ValueError):
                collect_youtube(no_json, lambda _: page)

    def test_youtube_fails_missing_or_inexact_fields_instead_of_guessing(self):
        cases = [("viewCountText", None), ("videoCountText", "1.2K videos"),
                 ("viewCountText", "5,40,914 views"), ("subscriberCountText", {"simpleText": "53.9K subscribers"})]
        for key, value in cases:
            about = {**ABOUT, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                collect_youtube(no_json, lambda _: youtube_html(about))

    def test_youtube_allows_identical_duplicate_and_rejects_conflict(self):
        collect_youtube(no_json, lambda _: youtube_html(duplicate=copy.deepcopy(ABOUT)))
        with self.assertRaises(ValueError):
            collect_youtube(no_json, lambda _: youtube_html(duplicate={**ABOUT, "viewCountText": "9 views"}))

    def test_youtube_parses_json_without_executing_script(self):
        result = collect_youtube(no_json, lambda _: youtube_html(assignment='window["ytInitialData"]'))
        self.assertEqual(result["youtube_views"]["value"], 5400914)
        with self.assertRaises(ValueError):
            collect_youtube(no_json, lambda _: '<script>var ytInitialData = not_json;</script>')

    def test_university_reads_labeled_summary_not_other_counts(self):
        seen = []
        def fetch(url):
            seen.append(url)
            return UNIVERSITY_HTML
        self.assertEqual(collect_university(no_json, fetch), {
            "academic_articles": {"value": 11, "source": UNIVERSITY_SOURCE}
        })
        self.assertEqual(seen, [UNIVERSITY_SOURCE])

    def test_university_rejects_wrong_person_or_profile_id(self):
        pages = [UNIVERSITY_HTML.replace("FURKAN GÖZÜKARA", "SOMEONE ELSE"),
                 UNIVERSITY_HTML.replace("278952/makaleler", "999999/makaleler")]
        for page in pages:
            with self.subTest(page=page), self.assertRaises(ValueError):
                collect_university(no_json, lambda _: page)

    def test_university_rejects_changed_or_missing_counts(self):
        pages = [UNIVERSITY_HTML.replace("<h3>11</h3>", "<h3>Unavailable</h3>"),
                 UNIVERSITY_HTML.replace("MAKALE", "PROJECT"), UNIVERSITY_HTML + UNIVERSITY_HTML]
        for page in pages:
            with self.subTest(page=page), self.assertRaises(ValueError):
                collect_university(no_json, lambda _: page)




"""Network-free failure/shape checks for the community-statistics collectors."""

import unittest

from scripts.update_profile_stats import (
    DEV_BASE_URL, DISCORD_GUILD_ID, DISCORD_INVITE_URL, HF_MODELS_URL,
    HF_WAN_URL, collect_dev, collect_discord, collect_huggingface,
)


def model(name):
    return {"id": "MonsterMMORPG/" + name, "private": False}


def article(number):
    return {"id": number, "user": {"username": "furkangozukara"},
            "published_at": "2026-10-25T00:00:00Z"}


def fetcher(routes):
    def fetch(url, return_headers=False):
        value = routes[url]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.next_url = HF_MODELS_URL + "&cursor=next-cursor"
        self.wan = {**model("Wan_GGUF"), "downloads": 69010}

    def test_huggingface_follows_cursor_and_keeps_download_window(self):
        fetch = fetcher({
            HF_MODELS_URL: ([model("one")], {"link": f'<{self.next_url}>; rel="next"'}),
            self.next_url: ([model("two")], {}), HF_WAN_URL: self.wan,
        })
        result = collect_huggingface(fetch, None)
        self.assertEqual(result["hf_models"]["value"], 2)
        self.assertEqual(result["wan_downloads"]["value"], 69010)

    def test_huggingface_does_not_accept_untrusted_or_changed_next_link(self):
        for next_url in (self.next_url.replace("https:", "http:"),
                         self.next_url.replace("huggingface.co", "example.com"),
                         self.next_url.replace("MonsterMMORPG", "different-user")):
            with self.subTest(next_url=next_url), self.assertRaises(ValueError):
                collect_huggingface(fetcher({HF_MODELS_URL: (
                    [model("one")], {"link": f'<{next_url}>; rel="next"'})}), None)

    def test_huggingface_rejects_missing_headers_and_bad_header(self):
        for response in ([model("one")], ([model("one")], None),
                         ([model("one")], {"link": "not a valid link"}),
                         ([model("one")], {"link": f'broken, <{self.next_url}>; rel="prev"'})):
            with self.subTest(response=response), self.assertRaises(ValueError):
                collect_huggingface(fetcher({HF_MODELS_URL: response}), None)

    def test_huggingface_repeated_ids_and_failed_continuation_raise(self):
        for continuation in (([model("one")], {}), OSError("page unavailable")):
            with self.subTest(continuation=continuation), self.assertRaises((ValueError, OSError)):
                collect_huggingface(fetcher({
                    HF_MODELS_URL: ([model("one")], {"link": f'<{self.next_url}>; rel="next"'}),
                    self.next_url: continuation,
                }), None)

    def test_huggingface_rejects_private_models(self):
        with self.assertRaises(ValueError):
            collect_huggingface(fetcher({HF_MODELS_URL: (
                [{"id": "MonsterMMORPG/private", "private": True}], {})}), None)

    def test_dev_continues_after_short_pages_until_empty(self):
        result = collect_dev(fetcher({
            DEV_BASE_URL + "&page=1": [article(1)],
            DEV_BASE_URL + "&page=2": [article(2)],
            DEV_BASE_URL + "&page=3": [],
        }), None)
        self.assertEqual(result["dev_posts"]["value"], 2)

    def test_dev_does_not_report_partial_count(self):
        for page_two in ([article(1)], OSError("page unavailable"), {"error": "failed"}):
            with self.subTest(page_two=page_two), self.assertRaises((ValueError, OSError)):
                collect_dev(fetcher({DEV_BASE_URL + "&page=1": [article(1)],
                                    DEV_BASE_URL + "&page=2": page_two}), None)

    def test_dev_rejects_unpublished_or_wrong_author(self):
        for entry in ({**article(1), "published_at": None},
                      {**article(1), "user": {"username": "someone_else"}}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                collect_dev(fetcher({DEV_BASE_URL + "&page=1": [entry]}), None)

    def test_discord_reads_total_and_not_online_count(self):
        data = {"guild": {"id": DISCORD_GUILD_ID}, "approximate_member_count": 12077,
                "approximate_presence_count": 1414}
        result = collect_discord(fetcher({DISCORD_INVITE_URL: data}), None)
        self.assertEqual(result["discord_members"]["value"], 12077)

    def test_discord_rejects_online_only_or_wrong_guild(self):
        for data in ({"guild": {"id": DISCORD_GUILD_ID}, "approximate_presence_count": 1414},
                     {"guild": {"id": "123"}, "approximate_member_count": 12077}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                collect_discord(fetcher({DISCORD_INVITE_URL: data}), None)




"""Regression coverage for stale data and safe README updates; no network calls."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import update_profile_stats as profile


BEFORE = "2026-10-25T12:00:00Z"
NOW = "2026-10-26T09:17:00Z"
COUNTS = {
    "youtube_subscribers": 53900,
    "youtube_videos": 469,
    "youtube_views": 5398689,
    "github_repos": 267,
    "github_followers": 1246,
    "hub_stars": 2781,
    "hub_forks": 373,
    "hf_models": 88,
    "wan_downloads": 68531,
    "discord_members": 12089,
    "dev_posts": 236,
    "academic_articles": 11,
}
SOURCES = {
    "youtube": "https://www.youtube.com/@SECourses/about",
    "github": "https://api.github.com/users/FurkanGozukara",
    "huggingface": "https://huggingface.co/MonsterMMORPG",
    "discord": "https://discord.com/api/v10/invites/software-engineering-courses-secourses-772774097734074388?with_counts=true",
    "dev": "https://dev.to/furkangozukara",
    "university": "https://apbs.mersin.edu.tr/profiles_mail/furkangozukara",
}


def fixture():
    keys = {key for group in profile.GROUP_KEYS.values() for key in group}
    assert keys == set(COUNTS), "Fixture must cover all twelve public metrics"
    metrics = {
        key: {"value": COUNTS[key], "source": SOURCES[group],
              "checked_at": BEFORE, "status": "fresh"}
        for group, group_keys in profile.GROUP_KEYS.items()
        for key in group_keys
    }
    metrics["youtube_subscribers"].update(display="53.9K", precision="rounded")
    metrics["discord_members"]["precision"] = "approximate"
    for key in ("hub_stars", "hub_forks"):
        metrics[key]["source"] = "https://api.github.com/repos/FurkanGozukara/Stable-Diffusion"
    metrics["wan_downloads"]["source"] = "https://huggingface.co/MonsterMMORPG/Wan_GGUF"
    return {"schema_version": 1, "generated_at": BEFORE, "metrics": metrics,
            "source_status": {group: "ok" for group in profile.GROUP_KEYS}}


def successful_group(state, group):
    return {
        key: {field: copy.deepcopy(value) for field, value in state["metrics"][key].items()
              if field not in {"checked_at", "status"}}
        for key in profile.GROUP_KEYS[group]
    }


class MergeResultsTests(unittest.TestCase):
    def test_partial_failure_preserves_last_success_and_does_not_mutate_input(self):
        previous = fixture()
        untouched = copy.deepcopy(previous)
        github = successful_group(previous, "github")
        github["github_repos"]["value"] = 268

        result = profile.merge_results(
            previous, {"github": github}, {"youtube": "TimeoutError"}, NOW
        )

        self.assertEqual(previous, untouched)
        self.assertEqual(result["generated_at"], NOW)
        self.assertEqual(result["metrics"]["github_repos"]["value"], 268)
        for key in profile.GROUP_KEYS["github"]:
            with self.subTest(updated=key):
                self.assertEqual(result["metrics"][key]["checked_at"], NOW)
                self.assertEqual(result["metrics"][key]["status"], "fresh")
        for group, keys in profile.GROUP_KEYS.items():
            if group == "github":
                continue
            for key in keys:
                with self.subTest(retained=key):
                    self.assertEqual(result["metrics"][key],
                                     {**previous["metrics"][key], "status": "stale"})
            self.assertEqual(result["source_status"][group], "unavailable")

    def test_failed_first_fetch_is_unavailable_without_a_fabricated_zero_or_date(self):
        result = profile.merge_results(
            {"metrics": {}}, {"github": successful_group(fixture(), "github")},
            {"youtube": "TimeoutError"}, NOW,
        )
        self.assertEqual(set(result["metrics"]), set(COUNTS))
        for group, keys in profile.GROUP_KEYS.items():
            if group == "github":
                continue
            for key in keys:
                with self.subTest(metric=key):
                    metric = result["metrics"][key]
                    self.assertIsNone(metric["value"])
                    self.assertIsNone(metric["checked_at"])
                    self.assertEqual(metric["status"], "unavailable")
                    self.assertEqual(profile.exact_value(metric), "Unavailable")


class ReadmeUpdateTests(unittest.TestCase):
    def test_only_generated_block_changes_and_repeated_update_is_stable(self):
        prefix = "# Dr. Furkan Gözükara\n\nKeep this introduction and its spacing.  \n\n"
        suffix = "\n\n## Research\nKeep [this link](https://example.com/) and every other section.\n"
        text = prefix + profile.START + "\nold-only-content\n" + profile.END + suffix
        state = fixture()

        updated = profile.update_readme(text, state)

        self.assertEqual(updated[:updated.index(profile.START)], prefix)
        self.assertEqual(updated[updated.index(profile.END) + len(profile.END):], suffix)
        self.assertEqual(updated.count(profile.START), 1)
        self.assertEqual(updated.count(profile.END), 1)
        self.assertNotIn("old-only-content", updated)
        for label in profile.LABELS.values():
            self.assertIn("| " + label + " |", updated)
        self.assertEqual(profile.update_readme(updated, state), updated)

    def test_missing_duplicate_or_reversed_markers_are_rejected(self):
        cases = {
            "both missing": "A normal README without markers",
            "start missing": "intro\n" + profile.END,
            "end missing": profile.START + "\ncontent",
            "duplicate start": profile.START + profile.START + profile.END,
            "duplicate end": profile.START + profile.END + profile.END,
            "reversed": profile.END + "\ncontent\n" + profile.START,
        }
        for name, text in cases.items():
            with self.subTest(markers=name), self.assertRaises(ValueError):
                profile.update_readme(text, fixture())

    def test_invalid_readme_causes_no_output_writes_or_temporary_files(self):
        def snapshot(root):
            return {
                str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
                for path in root.rglob("*")
            }

        for seed_assets in (False, True):
            with self.subTest(existing_assets=seed_assets), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "README.md").write_text(profile.START + "\nNo end marker\n", encoding="utf-8")
                if seed_assets:
                    assets = root / "assets"
                    assets.mkdir()
                    for name in ("profile-stats.json", "stats-dashboard.svg", "stats-dashboard-mobile.svg"):
                        (assets / name).write_bytes(("untouched " + name).encode())
                before = snapshot(root)

                with self.assertRaises(ValueError):
                    profile.write_outputs(root, fixture())

                self.assertEqual(snapshot(root), before)


