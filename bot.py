"""
PiPar Reddit Bot
- Monitors mentions of PiPar across Reddit
- Posts changelogs/announcements to r/pipar
- Collects and forwards user feedback to Firestore

Designed to run on GCP (Cloud Run scheduled + Compute Engine persistent).
"""

import praw
import json
import logging
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

# Optional: Firestore for feedback storage & state tracking
try:
    from google.cloud import firestore
    HAS_FIRESTORE = True
except ImportError:
    HAS_FIRESTORE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pipar-bot")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    # Reddit credentials (from env vars or Secret Manager)
    client_id: str = field(default_factory=lambda: os.environ["REDDIT_CLIENT_ID"])
    client_secret: str = field(default_factory=lambda: os.environ["REDDIT_CLIENT_SECRET"])
    username: str = field(default_factory=lambda: os.environ["REDDIT_USERNAME"])
    password: str = field(default_factory=lambda: os.environ["REDDIT_PASSWORD"])
    user_agent: str = "pipar-bot/1.0 (by u/pipar_official)"

    # Subreddit to manage
    home_subreddit: str = "pipar"

    # Subreddits to monitor for mentions (+ separates them in PRAW)
    # Add relevant ones: productivity, androidapps, artificial, etc.
    monitor_subreddits: str = (
        "android+androidapps+productivity+artificial+ChatGPT+"
        "LocalLLaMA+solopreneur+Entrepreneur+startups"
    )

    # Keywords to watch for (case-insensitive)
    mention_keywords: list = field(default_factory=lambda: [
        "pipar",
        "piparapp",
        "pipar app",
    ])

    # Firestore collection names
    fs_mentions_collection: str = "reddit_mentions"
    fs_feedback_collection: str = "reddit_feedback"
    fs_state_collection: str = "reddit_bot_state"

    # Feedback detection keywords/flairs
    feedback_flairs: list = field(default_factory=lambda: [
        "feedback", "feature request", "bug report", "suggestion",
    ])
    feedback_keywords: list = field(default_factory=lambda: [
        "feature request", "suggestion", "would be nice",
        "bug report", "issue:", "feedback:",
    ])


# ---------------------------------------------------------------------------
# Reddit client setup
# ---------------------------------------------------------------------------

def create_reddit_client(config: BotConfig) -> praw.Reddit:
    return praw.Reddit(
        client_id=config.client_id,
        client_secret=config.client_secret,
        username=config.username,
        password=config.password,
        user_agent=config.user_agent,
    )


# ---------------------------------------------------------------------------
# Module 1: Monitor mentions across Reddit
# ---------------------------------------------------------------------------

class MentionMonitor:
    """Streams comments & submissions across subreddits, looking for PiPar mentions."""

    def __init__(self, reddit: praw.Reddit, config: BotConfig):
        self.reddit = reddit
        self.config = config
        self.db = firestore.Client() if HAS_FIRESTORE else None

    def _matches(self, text: str) -> bool:
        text_lower = text.lower()
        return any(kw in text_lower for kw in self.config.mention_keywords)

    def _store_mention(self, item, item_type: str):
        mention = {
            "type": item_type,
            "subreddit": str(item.subreddit),
            "author": str(item.author) if item.author else "[deleted]",
            "body": item.body if item_type == "comment" else item.selftext,
            "title": getattr(item, "title", None),
            "permalink": f"https://reddit.com{item.permalink}",
            "created_utc": datetime.fromtimestamp(item.created_utc, tz=timezone.utc).isoformat(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "reddit_id": item.id,
        }

        if self.db:
            self.db.collection(self.config.fs_mentions_collection).document(item.id).set(mention)
            log.info(f"Stored mention {item.id} from r/{item.subreddit}")
        else:
            # Fallback: log to stdout (Cloud Logging picks this up)
            log.info(f"MENTION: {json.dumps(mention)}")

    def stream_comments(self):
        """Long-running: streams comments from monitored subreddits."""
        subreddit = self.reddit.subreddit(self.config.monitor_subreddits)
        log.info(f"Streaming comments from: {self.config.monitor_subreddits}")

        for comment in subreddit.stream.comments(skip_existing=True, pause_after=-1):
            if comment is None:
                continue
            try:
                if self._matches(comment.body):
                    self._store_mention(comment, "comment")
            except Exception as e:
                log.error(f"Error processing comment {comment.id}: {e}")

    def stream_submissions(self):
        """Long-running: streams submissions from monitored subreddits."""
        subreddit = self.reddit.subreddit(self.config.monitor_subreddits)
        log.info(f"Streaming submissions from: {self.config.monitor_subreddits}")

        for submission in subreddit.stream.submissions(skip_existing=True, pause_after=-1):
            if submission is None:
                continue
            try:
                text = f"{submission.title} {submission.selftext}"
                if self._matches(text):
                    self._store_mention(submission, "submission")
            except Exception as e:
                log.error(f"Error processing submission {submission.id}: {e}")

    def run(self):
        """Alternates between comment and submission streams."""
        subreddit = self.reddit.subreddit(self.config.monitor_subreddits)
        comment_stream = subreddit.stream.comments(skip_existing=True, pause_after=0)
        submission_stream = subreddit.stream.submissions(skip_existing=True, pause_after=0)

        log.info("Starting mention monitor (interleaved streams)...")
        while True:
            try:
                for comment in comment_stream:
                    if comment is None:
                        break
                    if self._matches(comment.body):
                        self._store_mention(comment, "comment")

                for submission in submission_stream:
                    if submission is None:
                        break
                    text = f"{submission.title} {submission.selftext}"
                    if self._matches(text):
                        self._store_mention(submission, "submission")

            except Exception as e:
                log.error(f"Stream error: {e}")
                time.sleep(30)


# ---------------------------------------------------------------------------
# Module 2: Post changelogs / announcements
# ---------------------------------------------------------------------------

class AnnouncementPoster:
    """Posts changelogs and announcements to the home subreddit.

    Designed to be called from a Cloud Run endpoint or CLI, not as a
    long-running stream.
    """

    def __init__(self, reddit: praw.Reddit, config: BotConfig):
        self.reddit = reddit
        self.config = config
        self.subreddit = reddit.subreddit(config.home_subreddit)

    def post_changelog(
        self,
        version: str,
        changes: list[str],
        title_prefix: str = "PiPar Update",
        flair: Optional[str] = "Changelog",
    ) -> str:
        """Posts a formatted changelog to the subreddit.

        Returns the submission URL.
        """
        # Build markdown body
        lines = [
            f"# {title_prefix} v{version}\n",
            f"**Released:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n",
            "## What's New\n",
        ]
        for change in changes:
            lines.append(f"- {change}")

        lines.extend([
            "\n---",
            "*PiPar — Your AI-powered personal assistant for solo entrepreneurs.*",
            "",
            "Download: [Google Play](https://play.google.com/store/apps/details?id=com.nexoy.pipar)",
            "",
            "Questions? Drop a comment below!",
        ])

        body = "\n".join(lines)
        title = f"{title_prefix} v{version}"

        submission = self.subreddit.submit(
            title=title,
            selftext=body,
            flair_id=None,  # Set flair_id if you have one configured
        )

        # Try to set flair by text (requires mod permissions)
        if flair:
            try:
                submission.mod.flair(text=flair)
            except Exception:
                log.warning("Could not set flair (bot may not be mod)")

        # Pin if the bot is a moderator
        try:
            submission.mod.sticky()
        except Exception:
            log.warning("Could not sticky post (bot may not be mod)")

        log.info(f"Posted changelog: {submission.url}")
        return submission.url

    def post_announcement(self, title: str, body: str, flair: Optional[str] = "Announcement") -> str:
        """Posts a freeform announcement."""
        submission = self.subreddit.submit(title=title, selftext=body)

        if flair:
            try:
                submission.mod.flair(text=flair)
            except Exception:
                pass
        try:
            submission.mod.sticky()
        except Exception:
            pass

        log.info(f"Posted announcement: {submission.url}")
        return submission.url


# ---------------------------------------------------------------------------
# Module 3: Collect & forward user feedback
# ---------------------------------------------------------------------------

class FeedbackCollector:
    """Monitors the home subreddit for feedback posts/comments and stores them."""

    def __init__(self, reddit: praw.Reddit, config: BotConfig):
        self.reddit = reddit
        self.config = config
        self.subreddit = reddit.subreddit(config.home_subreddit)
        self.db = firestore.Client() if HAS_FIRESTORE else None

    def _is_feedback(self, item) -> bool:
        """Check if a submission or comment looks like feedback."""
        # Check flair on submissions
        if hasattr(item, "link_flair_text") and item.link_flair_text:
            if item.link_flair_text.lower() in self.config.feedback_flairs:
                return True

        text = getattr(item, "body", "") or getattr(item, "selftext", "") or ""
        title = getattr(item, "title", "") or ""
        combined = f"{title} {text}".lower()

        return any(kw in combined for kw in self.config.feedback_keywords)

    def _store_feedback(self, item, item_type: str):
        feedback = {
            "type": item_type,
            "author": str(item.author) if item.author else "[deleted]",
            "title": getattr(item, "title", None),
            "body": item.body if item_type == "comment" else item.selftext,
            "flair": getattr(item, "link_flair_text", None),
            "permalink": f"https://reddit.com{item.permalink}",
            "created_utc": datetime.fromtimestamp(item.created_utc, tz=timezone.utc).isoformat(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "reddit_id": item.id,
            "status": "new",  # For triage: new -> reviewed -> addressed
        }

        if self.db:
            self.db.collection(self.config.fs_feedback_collection).document(item.id).set(feedback)
            log.info(f"Stored feedback {item.id} from u/{feedback['author']}")
        else:
            log.info(f"FEEDBACK: {json.dumps(feedback)}")

    def run(self):
        """Streams submissions and comments from home subreddit for feedback."""
        comment_stream = self.subreddit.stream.comments(skip_existing=True, pause_after=0)
        submission_stream = self.subreddit.stream.submissions(skip_existing=True, pause_after=0)

        log.info(f"Monitoring r/{self.config.home_subreddit} for feedback...")
        while True:
            try:
                for comment in comment_stream:
                    if comment is None:
                        break
                    if self._is_feedback(comment):
                        self._store_feedback(comment, "comment")

                for submission in submission_stream:
                    if submission is None:
                        break
                    if self._is_feedback(submission):
                        self._store_feedback(submission, "submission")

            except Exception as e:
                log.error(f"Feedback stream error: {e}")
                time.sleep(30)


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def run_monitor():
    """Run the mention monitor (long-running, for Compute Engine / container)."""
    config = BotConfig()
    reddit = create_reddit_client(config)
    log.info(f"Authenticated as u/{reddit.user.me()}")

    import threading

    monitor = MentionMonitor(reddit, config)
    collector = FeedbackCollector(reddit, config)

    # Run both in parallel
    t1 = threading.Thread(target=monitor.run, name="mention-monitor", daemon=True)
    t2 = threading.Thread(target=collector.run, name="feedback-collector", daemon=True)

    t1.start()
    t2.start()

    log.info("Bot running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down.")


def post_changelog_cli():
    """CLI tool to post a changelog. Called manually or from CI/CD."""
    import argparse

    parser = argparse.ArgumentParser(description="Post PiPar changelog to Reddit")
    parser.add_argument("--version", required=True, help="Version string, e.g. 1.2.3")
    parser.add_argument("--changes", required=True, nargs="+", help="List of changes")
    args = parser.parse_args()

    config = BotConfig()
    reddit = create_reddit_client(config)
    poster = AnnouncementPoster(reddit, config)
    url = poster.post_changelog(args.version, args.changes)
    print(f"Posted: {url}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "changelog":
        sys.argv.pop(1)  # Remove subcommand before argparse
        post_changelog_cli()
    else:
        run_monitor()
