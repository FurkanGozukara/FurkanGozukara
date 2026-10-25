#!/usr/bin/env python3
"""Refresh public profile statistics and render the desktop/mobile dashboard.

Run with Python 3.11+: python3 scripts/update_profile_stats.py
Uses only the standard library. No service API keys are required.
An optional GITHUB_TOKEN is sent only to api.github.com, never other sources.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- PROFILE-STATS:START -->"
END = "<!-- PROFILE-STATS:END -->"
WORKFLOW_URL = "https://github.com/FurkanGozukara/FurkanGozukara/actions/workflows/update-profile-stats.yml"
ALLOWED_HOSTS = {"api.github.com", "www.youtube.com", "youtube.com", "apbs.mersin.edu.tr", "discord.com", "huggingface.co", "dev.to"}
MAX_BYTES = 12_000_000
ISTANBUL = ZoneInfo("Europe/Istanbul")


def validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError("Unexpected public statistics URL")


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def fetch_response(url):
    validate_url(url)
    headers = {
        "User-Agent": "SECourses-Profile-Stats/1.0 (+https://github.com/FurkanGozukara/FurkanGozukara)",
        "Accept": "application/json,text/html;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if urlparse(url).hostname == "api.github.com" and token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(2):
        try:
            with build_opener(SafeRedirect()).open(Request(url, headers=headers), timeout=20) as response:
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("Statistics response exceeds expected size")
                return data.decode("utf-8"), {k.lower(): v for k, v in response.headers.items()}
        except HTTPError as exc:
            if attempt == 0 and (exc.code == 429 or 500 <= exc.code < 600):
                time.sleep(2)
                continue
            raise
        except (URLError, TimeoutError):
            if attempt == 0:
                time.sleep(2)
                continue
            raise


def fetch_json(url, return_headers=False):
    body, headers = fetch_response(url)
    data = json.loads(body)
    return (data, headers) if return_headers else data


def fetch_text(url):
    return fetch_response(url)[0]


def collect_github(fetch_json, fetch_text):
    user_url = "https://api.github.com/users/FurkanGozukara"
    repo_url = "https://api.github.com/repos/FurkanGozukara/Stable-Diffusion"
    user = fetch_json(user_url)
    repo = fetch_json(repo_url)
    if user.get("login", "").lower() != "furkangozukara" or repo.get("full_name", "").lower() != "furkangozukara/stable-diffusion":
        raise ValueError("GitHub source identity mismatch")
    return {
        "github_repos": {"value": user["public_repos"], "source": user_url},
        "github_followers": {"value": user["followers"], "source": user_url},
        "hub_stars": {"value": repo["stargazers_count"], "source": repo_url},
        "hub_forks": {"value": repo["forks_count"], "source": repo_url},
    }


YOUTUBE_CHANNEL_ID = "UCKzrgTDEZjOolJAshjgvSKw"


YOUTUBE_SOURCE = (
    "https://www.youtube.com/channel/UCKzrgTDEZjOolJAshjgvSKw/about?hl=en"
)


UNIVERSITY_SOURCE = "https://apbs.mersin.edu.tr/profiles_mail/furkangozukara"


UNIVERSITY_ARTICLES = "https://apbs.mersin.edu.tr/profiles/278952/makaleler"


_INITIAL_DATA_ASSIGNMENT = re.compile(
    r"(?:\b(?:var|let|const)\s+ytInitialData|"
    r"\bwindow(?:\.ytInitialData|\[['\"]ytInitialData['\"]\])|"
    r"\bytInitialData)\s*=\s*"
)


_INTEGER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"


_SCALES = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


class _Scripts(HTMLParser):
    """Collect script text without executing JavaScript or unescaping JSON."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def _walk_json(value):
    pending = [value]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            yield value
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def _youtube_initial_data(page):
    if not isinstance(page, str):
        raise ValueError("YouTube HTML must be text")
    scripts = _Scripts()
    scripts.feed(page)
    decoder = json.JSONDecoder()
    candidates = []
    for script in scripts.scripts:
        for marker in _INITIAL_DATA_ASSIGNMENT.finditer(script):
            try:
                data, _ = decoder.raw_decode(script[marker.end():])
            except (ValueError, RecursionError):
                continue
            if isinstance(data, dict):
                candidates.append(data)
    if len(candidates) != 1:
        raise ValueError("Expected one valid YouTube ytInitialData object")
    data = candidates[0]
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("YouTube channel metadata is missing or malformed")
    metadata = metadata.get("channelMetadataRenderer")
    if not isinstance(metadata, dict) or metadata.get("externalId") != YOUTUBE_CHANNEL_ID:
        raise ValueError("YouTube page metadata does not identify the SECourses channel")
    return data


def _youtube_number(text, unit, *, abbreviated=False):
    if not isinstance(text, str):
        raise ValueError(f"YouTube {unit} field must be a string")
    text = text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    number_pattern = _INTEGER + (r"(?:\.\d+)?[KMB]?" if abbreviated else "")
    match = re.fullmatch(rf"({number_pattern})\s+{unit}s?", text, re.IGNORECASE)
    if match is None:
        raise ValueError(f"Malformed or non-exact YouTube {unit} count")
    display = match.group(1).upper()
    suffix = display[-1] if display[-1] in "KMB" else ""
    number = display[:-1] if suffix else display
    try:
        value = Decimal(number.replace(",", "")) * _SCALES[suffix]
    except InvalidOperation as exc:
        raise ValueError(f"Malformed YouTube {unit} number") from exc
    if not value.is_finite() or value != value.to_integral_value() or value < 0:
        raise ValueError(f"Non-integral YouTube {unit} count")
    result = {"value": int(value), "source": YOUTUBE_SOURCE}
    if abbreviated:
        result["display"] = display
        # Public subscriber totals above 999 have limited displayed precision.
        if suffix or value >= 1000:
            result["precision"] = "rounded"
    return result


def collect_youtube(fetch_json, fetch_text):
    """Return three channel statistics or raise ValueError on incomplete data.

    The selected channel's aboutChannelViewModel is the only statistics source;
    related-channel cards and featured-video view counts must never be used.
    fetch_json is intentionally unused: the official Data API requires a key.
    """
    data = _youtube_initial_data(fetch_text(YOUTUBE_SOURCE))
    models = [
        node["aboutChannelViewModel"]
        for node in _walk_json(data)
        if isinstance(node.get("aboutChannelViewModel"), dict)
        and node["aboutChannelViewModel"].get("channelId") == YOUTUBE_CHANNEL_ID
    ]
    if not models:
        raise ValueError("SECourses aboutChannelViewModel is missing")
    selected_fields = ("subscriberCountText", "videoCountText", "viewCountText")
    signatures = [tuple(model.get(field) for field in selected_fields) for model in models]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise ValueError("Conflicting YouTube channel statistics")
    subscribers, videos, views = signatures[0]
    return {
        "youtube_subscribers": _youtube_number(subscribers, "subscriber", abbreviated=True),
        "youtube_videos": _youtube_number(videos, "video"),
        "youtube_views": _youtube_number(views, "view"),
    }


class _Node:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def has_class(self, name):
        return name in (self.attrs.get("class") or "").split()

    def text(self):
        return "".join(child.text() if isinstance(child, _Node) else child for child in self.children)


class _HTMLTree(HTMLParser):
    _VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk_html(node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk_html(child)


def collect_university(fetch_json, fetch_text):
    """Read the MAKALE summary card, bound to Furkan Gozukara's profile ID."""
    page = fetch_text(UNIVERSITY_SOURCE)
    if not isinstance(page, str):
        raise ValueError("University HTML must be text")
    parser = _HTMLTree()
    parser.feed(page)
    if "FURKAN GÖZÜKARA" not in " ".join(parser.root.text().upper().split()):
        raise ValueError("University page does not identify Furkan Gözükara")
    observations = []
    for card in _walk_html(parser.root):
        if not card.has_class("small-box"):
            continue
        for inner in _walk_html(card):
            if not inner.has_class("inner"):
                continue
            labels = [node.text().strip().upper() for node in _walk_html(inner) if node.tag == "p"]
            if labels != ["MAKALE"]:
                continue
            links = [
                urljoin(UNIVERSITY_SOURCE, node.attrs.get("href") or "")
                for node in _walk_html(card)
                if node.tag == "a"
            ]
            if UNIVERSITY_ARTICLES not in links:
                raise ValueError("University article card belongs to an unexpected profile")
            counts = [node.text().strip() for node in _walk_html(inner) if node.tag == "h3"]
            if len(counts) != 1 or re.fullmatch(r"\d+", counts[0]) is None:
                raise ValueError("Malformed university article count")
            observations.append(int(counts[0]))
    if len(observations) != 1:
        raise ValueError("Expected exactly one university MAKALE summary card")
    return {"academic_articles": {"value": observations[0], "source": UNIVERSITY_SOURCE}}


HF_AUTHOR = "MonsterMMORPG"


HF_MODELS_URL = "https://huggingface.co/api/models?author=MonsterMMORPG&limit=100"


HF_WAN_URL = "https://huggingface.co/api/models/MonsterMMORPG/Wan_GGUF"


DEV_BASE_URL = "https://dev.to/api/articles?username=furkangozukara&per_page=1000"


DISCORD_GUILD_ID = "772774097734074388"


DISCORD_SLUG = "software-engineering-courses-secourses-772774097734074388"


DISCORD_INVITE_URL = f"https://discord.com/api/v10/invites/{DISCORD_SLUG}?with_counts=true"


MAX_PAGES = 100


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}: expected a nonnegative integer")
    return value


def _hf_next(headers):
    """Use the API's next cursor; never guess pages or follow a changed scope."""
    if not isinstance(headers, dict):
        raise ValueError("Hugging Face: response headers unavailable")
    link = headers.get("link")
    if link is None or link == "":
        return None
    if not isinstance(link, str):
        raise ValueError("Hugging Face: invalid Link header")
    next_urls = []
    for entry in re.split(r",\s*(?=<)", link):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel\s*=\s*(?:"([^"]+)"|([^;\s,]+))\s*', entry)
        if not match:
            raise ValueError("Hugging Face: malformed pagination header")
        if "next" in (match.group(2) or match.group(3)).split():
            next_urls.append(match.group(1))
    if len(next_urls) > 1:
        raise ValueError("Hugging Face: multiple next-page links")
    if not next_urls:
        return None
    url = next_urls[0]
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "huggingface.co"
            or parsed.path != "/api/models" or parsed.fragment
            or query.get("author") != [HF_AUTHOR] or query.get("limit") != ["100"]
            or set(query) != {"author", "limit", "cursor"}
            or len(query["cursor"]) != 1 or not query["cursor"][0]):
        raise ValueError("Hugging Face: unsafe or changed pagination scope")
    return url


def collect_huggingface(fetch_json, fetch_text):
    """Count public model IDs across every page; downloads cover the last 30 days."""
    url, seen_urls, model_ids = HF_MODELS_URL, set(), set()
    for _ in range(MAX_PAGES):
        if url in seen_urls:
            raise ValueError("Hugging Face: repeated pagination URL")
        seen_urls.add(url)
        response = fetch_json(url, return_headers=True)
        if not isinstance(response, tuple) or len(response) != 2:
            raise ValueError("Hugging Face: JSON and response headers required")
        models, headers = response
        if not isinstance(models, list) or len(models) > 100:
            raise ValueError("Hugging Face: unexpected model-list response")
        for model in models:
            if not isinstance(model, dict):
                raise ValueError("Hugging Face: invalid model entry")
            model_id = model.get("id")
            if (not isinstance(model_id, str)
                    or not model_id.startswith(HF_AUTHOR + "/")
                    or not model_id[len(HF_AUTHOR) + 1:] or model.get("private") is not False):
                raise ValueError("Hugging Face: unexpected author or visibility")
            if model_id in model_ids:
                raise ValueError("Hugging Face: repeated model ID; incomplete pagination")
            model_ids.add(model_id)
        url = _hf_next(headers)
        if url is None:
            break
        if not models:
            raise ValueError("Hugging Face: empty page with a continuation")
    else:
        raise ValueError("Hugging Face: model pagination limit exceeded")

    wan = fetch_json(HF_WAN_URL)
    if (not isinstance(wan, dict) or wan.get("id") != HF_AUTHOR + "/Wan_GGUF"
            or wan.get("private") is not False):
        raise ValueError("Hugging Face: unexpected Wan_GGUF model response")
    return {
        "hf_models": {"value": len(model_ids), "source": "https://huggingface.co/MonsterMMORPG"},
        "wan_downloads": {"value": _integer(wan.get("downloads"), "Wan_GGUF downloads"),
                          "source": "https://huggingface.co/MonsterMMORPG/Wan_GGUF"},
    }


def collect_dev(fetch_json, fetch_text):
    """Count published IDs until an empty page, even after a short nonempty page."""
    article_ids = set()
    for page in range(1, MAX_PAGES + 1):
        articles = fetch_json(f"{DEV_BASE_URL}&page={page}")
        if not isinstance(articles, list) or len(articles) > 1000:
            raise ValueError("DEV: unexpected published-articles response")
        if not articles:
            return {"dev_posts": {"value": len(article_ids),
                                  "source": "https://dev.to/furkangozukara"}}
        for article in articles:
            if not isinstance(article, dict):
                raise ValueError("DEV: invalid article entry")
            article_id = _integer(article.get("id"), "DEV article ID")
            user = article.get("user")
            if (not isinstance(user, dict) or user.get("username") != "furkangozukara"
                    or not isinstance(article.get("published_at"), str)
                    or not article["published_at"].strip()):
                raise ValueError("DEV: wrong author or missing publication timestamp")
            if article_id in article_ids:
                raise ValueError("DEV: repeated article ID; incomplete pagination")
            article_ids.add(article_id)
    raise ValueError("DEV: article pagination limit exceeded")


def collect_discord(fetch_json, fetch_text):
    """The public vanity invite exposes approximate total membership, not online users."""
    data = fetch_json(DISCORD_INVITE_URL)
    if not isinstance(data, dict) or not isinstance(data.get("guild"), dict):
        raise ValueError("Discord: unexpected public invite response")
    if data["guild"].get("id") != DISCORD_GUILD_ID:
        raise ValueError("Discord: public invite points to a different server")
    return {"discord_members": {
        "value": _integer(data.get("approximate_member_count"), "Discord member count"),
        "precision": "approximate",
        "source": f"https://discord.com/servers/{DISCORD_SLUG}",
    }}



GROUP_KEYS = {
    "youtube": ("youtube_subscribers", "youtube_videos", "youtube_views"),
    "github": ("github_repos", "github_followers", "hub_stars", "hub_forks"),
    "huggingface": ("hf_models", "wan_downloads"),
    "discord": ("discord_members",),
    "dev": ("dev_posts",),
    "university": ("academic_articles",),
}

LABELS = {
    "youtube_subscribers": "YouTube subscribers",
    "youtube_videos": "Published YouTube videos",
    "youtube_views": "Total YouTube channel views",
    "discord_members": "Discord members",
    "github_repos": "Public GitHub repositories",
    "github_followers": "GitHub followers",
    "hub_stars": "Stable-Diffusion hub stars",
    "hub_forks": "Stable-Diffusion hub forks",
    "hf_models": "Hugging Face model repositories",
    "wan_downloads": "Wan_GGUF downloads in the last month",
    "dev_posts": "Published DEV posts",
    "academic_articles": "University-listed academic articles",
}

DISPLAY_KEYS = ("youtube_subscribers", "youtube_views", "discord_members", "github_repos", "hub_stars", "hf_models", "wan_downloads", "dev_posts", "academic_articles")


def valid_sample(sample):
    if not isinstance(sample, dict) or isinstance(sample.get("value"), bool) or not isinstance(sample.get("value"), int):
        raise ValueError("Source did not return an integer count")
    if not 0 <= sample["value"] <= 10**15:
        raise ValueError("Source count is outside the expected range")
    validate_url(sample.get("source", ""))
    if "display" in sample and not re.fullmatch(r"[\d,.]+[KMB]?\+?", sample["display"]):
        raise ValueError("Unexpected formatted count")
    return sample


def merge_results(previous, results, errors, now):
    """A failed source never receives a new successful-check timestamp."""
    metrics = copy.deepcopy(previous.get("metrics", {}))
    for group, keys in GROUP_KEYS.items():
        if group in results:
            samples = results[group]
            if set(samples) != set(keys):
                raise ValueError("Incomplete metric set for " + group)
            for key in keys:
                sample = valid_sample(samples[key])
                metrics[key] = {**sample, "checked_at": now, "status": "fresh"}
        else:
            for key in keys:
                old = metrics.get(key, {})
                if isinstance(old.get("value"), int) and not isinstance(old.get("value"), bool):
                    metrics[key] = {**old, "status": "stale"}
                else:
                    metrics[key] = {"value": None, "checked_at": None, "status": "unavailable"}
    return {"schema_version": 1, "generated_at": now, "metrics": metrics,
            "source_status": {group: ("ok" if group in results else "unavailable") for group in GROUP_KEYS}}


def refresh(previous, collectors=None, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    collectors = collectors or {
        "youtube": collect_youtube, "github": collect_github,
        "huggingface": collect_huggingface, "discord": collect_discord,
        "dev": collect_dev, "university": collect_university,
    }
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {pool.submit(fn, fetch_json, fetch_text): group for group, fn in collectors.items()}
        for job in as_completed(jobs):
            group = jobs[job]
            try:
                samples = job.result()
                if set(samples) != set(GROUP_KEYS[group]):
                    raise ValueError("Incomplete metric set")
                for sample in samples.values():
                    valid_sample(sample)
                results[group] = samples
                print(group + ": refreshed")
            except Exception as exc:
                # Never print request headers, response bodies, or credential-bearing URLs.
                errors[group] = type(exc).__name__
                print(group + ": unavailable (" + type(exc).__name__ + "); preserving last successful values", file=sys.stderr)
    if not results:
        raise RuntimeError("No sources could be refreshed; previous dashboard was left unchanged")
    return merge_results(previous, results, errors, now)


def local_time(timestamp, short=False):
    if not timestamp:
        return "Never fetched"
    if "T" not in timestamp:
        return datetime.fromisoformat(timestamp).strftime("%d %b %Y")
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(ISTANBUL)
    return dt.strftime("%d %b %Y · %H:%M TRT" if not short else "%d %b · %H:%M TRT")


def compact(value):
    if value is None:
        return "—"
    for threshold, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if value >= threshold:
            return f"{value / threshold:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:,}"


def exact_value(metric):
    if metric.get("value") is None:
        return "Unavailable"
    if metric.get("precision") == "rounded":
        return metric.get("display", compact(metric["value"])) + " (rounded by source)"
    prefix = "≈ " if metric.get("precision") == "approximate" else ""
    return prefix + f"{metric['value']:,}"


def dashboard_cards(state):
    m = state["metrics"]
    specs = [
        ("youtube_subscribers", "YouTube subscribers", ["YouTube subscribers"], f"SECourses · {exact_value(m['youtube_videos'])} published videos", ["SECourses · " + exact_value(m['youtube_videos']) + " published videos"], "#FB7185", "play"),
        ("youtube_views", "YouTube views", ["YouTube views"], exact_value(m["youtube_views"]) + " total channel views", [exact_value(m["youtube_views"]) + " total", "channel views"], "#F5BC78", "eye"),
        ("discord_members", "Discord members", ["Discord", "members"], "Software Engineering Courses", ["Software Engineering", "Courses"], "#A5B4FC", "chat"),
        ("github_repos", "Public repositories", ["Public", "repositories"], "AI tools, experiments & courses", ["AI tools, experiments", "& courses"], "#5EEAD4", "repo"),
        ("hub_stars", "AI hub stars", ["AI hub stars"], "Stable-Diffusion knowledge base", ["Stable-Diffusion", "knowledge base"], "#F5BC78", "star"),
        ("hf_models", "Model repositories", ["Model", "repositories"], "Hugging Face · MonsterMMORPG", ["Hugging Face", "MonsterMMORPG"], "#60A5FA", "grid"),
        ("wan_downloads", "Downloads last month", ["Downloads", "last month"], "Wan_GGUF model repository", ["Wan_GGUF", "model repository"], "#5EEAD4", "download"),
        ("dev_posts", "Published DEV posts", ["Published", "DEV posts"], "Technical tutorials & articles", ["Technical tutorials", "& articles"], "#A5B4FC", "document"),
        ("academic_articles", "Academic articles", ["Academic", "articles"], "Mersin University record", ["Mersin University", "record"], "#F5BC78", "cap"),
    ]
    cards = []
    for key, label, mobile_label, sub, mobile_sub, color, icon in specs:
        metric = m[key]
        value = metric.get("value")
        display = metric.get("display") or (compact(value) if key in {"youtube_subscribers", "youtube_views", "discord_members", "wan_downloads"} else (f"{value:,}" if value is not None else "—"))
        stale = metric["status"] != "fresh"
        if stale:
            sub = "Refresh pending · " + local_time(metric.get("checked_at"), short=True)
            mobile_sub = ["Refresh pending", local_time(metric.get("checked_at"), short=True)]
        cards.append(dict(key=key, value=display, label=label, mobile_label=mobile_label,
                          sub=sub, mobile_sub=mobile_sub, color=color, icon=icon, stale=stale))
    return cards


ICONS = {
    "play": '<rect x="2" y="6" width="28" height="21" rx="6"/><path d="m13 11 9 6-9 5z"/>',
    "eye": '<path d="M1 17s6-10 15-10 15 10 15 10-6 10-15 10S1 17 1 17Z"/><circle cx="16" cy="17" r="4"/>',
    "chat": '<path d="M6 4h20a4 4 0 0 1 4 4v15a4 4 0 0 1-4 4H13l-7 5v-5a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4Z"/><path d="M9 13h14M9 19h9"/>',
    "repo": '<path d="M5 3h20a2 2 0 0 1 2 2v25H7a4 4 0 0 1-4-4V7a4 4 0 0 1 4-4Z"/><path d="M3 25h24M11 3v12l4-3 4 3V3"/>',
    "star": '<path d="m16 2 4.4 9 10 1.5-7.2 7 1.7 10L16 25l-8.9 4.5 1.7-10-7.2-7 10-1.5Z"/>',
    "grid": '<rect x="3" y="3" width="11" height="11" rx="2"/><rect x="20" y="3" width="11" height="11" rx="2"/><rect x="3" y="20" width="11" height="11" rx="2"/><rect x="20" y="20" width="11" height="11" rx="2"/>',
    "download": '<path d="M16 3v18m-7-7 7 7 7-7M4 22v8h24v-8"/>',
    "document": '<path d="M6 2h14l7 7v23H6ZM20 2v8h7M11 16h11M11 22h11M11 27h7"/>',
    "cap": '<path d="m1 12 16-8 16 8-16 8Z"/><path d="M7 15v10c6 5 14 5 20 0V15M33 12v15"/>',
}


def svg_text(x, y, text, size, color, weight=None, extra=""):
    weight_attr = f' font-weight="{weight}"' if weight else ""
    return f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}"{weight_attr}{extra}>{escape(str(text))}</text>'


def render_svg(state, mobile=False):
    width, height = (600, 1276) if mobile else (1200, 548)
    cards = dashboard_cards(state)
    description = "; ".join(LABELS[k] + ": " + exact_value(state["metrics"][k]) for k in DISPLAY_KEYS)
    stale_count = sum(c["stale"] for c in cards)
    status_color = "#F5BC78" if stale_count else "#5EEAD4"
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
             '<title id="title">SECourses automatically updated public statistics</title>',
             '<desc id="desc">' + escape(description + ". Per-source check times and availability are listed in the README.") + '</desc>',
             '<defs><linearGradient id="panel" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#112338"/><stop offset="1" stop-color="#0D192B"/></linearGradient><linearGradient id="highlight" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#2D2034"/><stop offset="1" stop-color="#112338"/></linearGradient></defs>',
             f'<rect width="{width}" height="{height}" rx="22" fill="#091422"/>',
             '<g font-family="Segoe UI, Arial, sans-serif">']
    stamp = local_time(state["generated_at"]).upper()
    if mobile:
        parts += [svg_text(24, 43, "REACH. TOOLS. RESEARCH.", 27, "#E2E8F0", 700),
                  f'<circle cx="29" cy="69" r="4" fill="{status_color}"/>',
                  svg_text(43, 76, stamp, 20, "#9DB6CC")]
    else:
        parts += [svg_text(28, 44, "REACH. TOOLS. RESEARCH.", 22, "#E2E8F0", 700),
                  f'<circle cx="952" cy="37" r="4" fill="{status_color}"/>',
                  svg_text(1174, 43, stamp, 16, "#91A9BE", extra=' text-anchor="end"')]
    for index, card in enumerate(cards):
        color = card["color"]
        if not mobile:
            x, y, w, h = 24 + (index % 3) * 392, 70 + (index // 3) * 148, 367, 131
            parts += [f'<g transform="translate({x} {y})"><rect x=".5" y=".5" width="{w}" height="{h}" rx="14" fill="url(#panel)" stroke="#20374C"/>',
                      f'<rect x="18" width="60" height="3" rx="1.5" fill="{color}"/>',
                      f'<g transform="translate(313 21) scale(.8)" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" opacity=".85">{ICONS[card["icon"]]}</g>',
                      svg_text(22, 57, card["value"], 46 if len(card["value"]) < 8 else 36, color, 750),
                      svg_text(22, 88, card["label"], 20, "#F1F5F9", 600),
                      svg_text(22, 113, card["sub"], 12 if card["stale"] else 14, "#F5BC78" if card["stale"] else "#9FB3C8"), '</g>']
        elif index == 0:
            parts += ['<g transform="translate(24 99)"><rect x=".5" y=".5" width="551" height="184" rx="18" fill="url(#highlight)" stroke="#5A3549"/>',
                      f'<rect x="24" width="88" height="4" rx="2" fill="{color}"/>',
                      f'<g transform="translate(479 25) scale(1.4)" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICONS[card["icon"]]}</g>',
                      svg_text(24, 88, card["value"], 84, color, 750),
                      svg_text(25, 130, card["label"], 30, "#F1F5F9", 600),
                      svg_text(25, 164, card["sub"], 19 if card["stale"] else 22, "#B7C8DA"), '</g>']
        else:
            x, y = 24 + ((index - 1) % 2) * 284, 304 + ((index - 1) // 2) * 226
            size = 62 if len(card["value"]) < 6 else 52
            parts += [f'<g transform="translate({x} {y})"><rect x=".5" y=".5" width="267" height="209" rx="16" fill="url(#panel)" stroke="#284258"/>',
                      f'<rect x="20" width="64" height="4" rx="2" fill="{color}"/>',
                      f'<g transform="translate(220 24)" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" opacity=".9">{ICONS[card["icon"]]}</g>',
                      svg_text(20, 76, card["value"], size, color, 750, extra=' letter-spacing="-1.3"')]
            for line, label in enumerate(card["mobile_label"]):
                parts.append(svg_text(20, 114 + line * 29, label, 26, "#F1F5F9", 600))
            for line, label in enumerate(card["mobile_sub"]):
                parts.append(svg_text(20, 171 + line * 23, label, 17 if card["stale"] else 21, "#F5BC78" if card["stale"] else "#A8BDD1"))
            parts.append('</g>')
    footer = "Automatically refreshed daily · Source check times and details below."
    if stale_count:
        footer = f"Auto-refresh daily · {stale_count} metric(s) awaiting refresh; last valid values retained."
    if mobile:
        parts += [svg_text(24, 1226, "Automatically refreshed daily", 20, "#91AAC1"),
                  svg_text(24, 1252, (f"{stale_count} metric(s) awaiting refresh · details below" if stale_count else "Source check times and details below."), 18, "#91AAC1")]
    else:
        parts.append(svg_text(28, 532, footer, 14, "#829CB4"))
    return "\n".join(parts + ['</g></svg>']) + "\n"


def readme_block(state):
    revision = hashlib.sha256((render_svg(state) + render_svg(state, mobile=True)).encode("utf-8")).hexdigest()[:12]
    description = "; ".join(LABELS[k] + ": " + exact_value(state["metrics"][k]) for k in DISPLAY_KEYS)
    lines = [f'<p align="center">\n  <picture>\n    <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/FurkanGozukara/FurkanGozukara/main/assets/stats-dashboard-mobile.svg?v={revision}" />\n    <img src="https://raw.githubusercontent.com/FurkanGozukara/FurkanGozukara/main/assets/stats-dashboard.svg?v={revision}" width="100%" alt="{escape(description, quote=True)}" />\n  </picture>\n</p>',
             f'**Automatically refreshed daily** · Last run: **{local_time(state["generated_at"])}** · [Refresh status / run now]({WORKFLOW_URL})',
             '<details>\n<summary><strong>Statistics sources, exact figures & last successful checks</strong></summary>\n',
             '| Metric | Public figure | Last successful check (Türkiye) | Status | Source |',
             '| :--- | ---: | :--- | :--- | :--- |']
    ordered_keys = ("youtube_subscribers", "youtube_videos", "youtube_views", "discord_members", "github_repos", "github_followers", "hub_stars", "hub_forks", "hf_models", "wan_downloads", "dev_posts", "academic_articles")
    for key in ordered_keys:
        metric = state["metrics"][key]
        source = f'[Public source]({metric["source"]})' if metric.get("source") else 'Unavailable'
        status = {"fresh": "Updated", "stale": "Awaiting refresh; last valid value", "unavailable": "Unavailable"}[metric["status"]]
        lines.append(f'| {LABELS[key]} | {exact_value(metric)} | {local_time(metric.get("checked_at"))} | {status} | {source} |')
    lines += ['', 'YouTube subscriber counts are rounded by YouTube. Discord membership is approximate. Wan_GGUF downloads cover that model repository’s rolling monthly window. Academic articles are those listed in the university record.', '',
              'The workflow fetches these public sources once every 24 hours, scheduled for 03:17 Türkiye time (00:17 UTC), and regenerates both dashboard layouts. A manual refresh is also available. GitHub scheduling and image caching can delay display; reloading the profile does not trigger a new upstream fetch. If a source fails, its last valid value and check time are retained. No service API keys are required.', '',
              '[Machine-readable statistics](./assets/profile-stats.json) · [Updater source](./scripts/update_profile_stats.py) · [Refresh workflow](' + WORKFLOW_URL + ')', '', '</details>']
    return "\n\n".join(lines[:3]) + "\n" + "\n".join(lines[3:])


def update_readme(text, state):
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("README must contain exactly one statistics marker pair")
    start, end = text.index(START), text.index(END)
    if end <= start:
        raise ValueError("README statistics markers are reversed")
    return text[:start + len(START)] + "\n\n" + readme_block(state) + "\n\n" + text[end:]


def write_outputs(root, state):
    readme = root / "README.md"
    # Prepare all content first: malformed markers must not leave half-written output.
    outputs = {
        readme: update_readme(readme.read_text(encoding="utf-8"), state),
        root / "assets/profile-stats.json": json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        root / "assets/stats-dashboard.svg": render_svg(state),
        root / "assets/stats-dashboard-mobile.svg": render_svg(state, mobile=True),
    }
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-only", action="store_true", help="Render the stored JSON without fetching data")
    args = parser.parse_args()
    data_path = ROOT / "assets/profile-stats.json"
    previous = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else {"metrics": {}}
    state = previous if args.render_only else refresh(previous)
    write_outputs(ROOT, state)
    fresh = sum(m["status"] == "fresh" for m in state["metrics"].values())
    print(f"Rendered both dashboards: {fresh}/{len(state['metrics'])} metrics updated")


if __name__ == "__main__":
    main()
